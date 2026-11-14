"""Sorting PRs: one label per PR, the reasons for it, and a score that
ranks the queue so maintainers start with the PRs that are cheapest to review
and best supported by evidence.

It judges what's in the PR: tests, CI, a linked issue, whether the change
matches its description, its size and, only if the policy says so, the
author's track record. It never guesses whether AI wrote it; detectors for
that aren't reliable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from stratum_open.findings import Finding
from stratum_open.policy import Policy
from stratum_open.review import demention
from stratum_open.track_record import AuthorRecord
from stratum_open.util import count

KEYS = ("needs_human", "needs_work", "low_effort", "ready")
COLORS = {"ready": "0e8a16", "needs_work": "d93f0b", "low_effort": "fbca04", "needs_human": "5319e7"}
DESCRIPTIONS = {
    "ready": "Stratum: evidence looks good; ready for a maintainer's review",
    "needs_work": "Stratum: the author has something to fix first",
    "low_effort": "Stratum: little evidence the change was checked",
    "needs_human": "Stratum: touches something a maintainer must look at",
}
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$")
_DOC_PATH = re.compile(r"(\.(md|rst|txt|adoc)$)|(^|/)docs?/|(^|/)(LICENSE|CHANGELOG|AUTHORS)[^/]*$", re.IGNORECASE)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_CHECKBOX = re.compile(r"^\s*[-*]\s*\[[ xX]\].*$", re.MULTILINE)
MIN_DESCRIPTION_CHARS = 40


@dataclass
class PRFacts:
    number: int
    title: str
    body: str
    author: str
    draft: bool
    additions: int
    deletions: int
    paths: list[str]
    ci_ok: bool | None = None  # None = pending or no CI
    # None = the lookup failed, so it isn't known whether the PR links one: never read as "no linked issue".
    linked_issues: list[int] | None = field(default_factory=list)

    @property
    def unlinked(self) -> bool:
        """The lookup worked, and the PR links no issue."""
        return self.linked_issues == []

    @classmethod
    def from_pull(cls, pull: dict, paths: list[str]) -> PRFacts:
        return cls(
            number=pull["number"], title=pull.get("title") or "", body=pull.get("body") or "",
            author=(pull.get("user") or {}).get("login", ""), draft=bool(pull.get("draft")),
            additions=pull.get("additions", 0), deletions=pull.get("deletions", 0), paths=paths,
        )


@dataclass
class Triage:
    key: str  # one of KEYS
    reasons: list[str]
    evidence: list[str]  # what speaks for the PR
    score: float  # higher = review sooner

    def as_dict(self) -> dict:
        return {"key": self.key, "reasons": self.reasons, "evidence": self.evidence, "score": round(self.score, 2)}


def label_for(policy: Policy, key: str) -> str:
    return getattr(policy.triage.labels, key)


def is_test(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def is_doc(path: str) -> bool:
    return bool(_DOC_PATH.search(path))


def description_length(body: str) -> int:
    """Characters of real description, without a PR template's comments and
    checklists."""
    return len(_CHECKBOX.sub("", _HTML_COMMENT.sub("", body or "")).strip())


def triage(
    facts: PRFacts,
    findings: list[Finding],
    policy: Policy,
    *,
    matches_description: str = "",
    mismatch: str = "",
    record: AuthorRecord | None = None,
) -> Triage:
    needs_human = _needs_human_reasons(findings)

    needs_work = []
    if facts.ci_ok is False:
        needs_work.append("CI is failing.")
    blocking = [f for f in findings if f.severity == "critical" or (f.source == "check" and f.blocking and not f.needs_human)]
    if blocking:
        needs_work.append(f"{count(len(blocking), 'serious finding')} to fix: " + "; ".join(sorted({f.title for f in blocking}))[:200] + ".")
    if policy.triage.require_linked_issue and facts.unlinked:
        needs_work.append("No linked issue, and this project asks for one.")
    if matches_description == "no":
        needs_work.append("The code doesn't do what the description says" + (f": {mismatch}" if mismatch else "."))

    code_paths = [p for p in facts.paths if not is_doc(p) and not is_test(p)]
    tests_changed = any(is_test(p) for p in facts.paths)
    described = description_length(facts.body) >= MIN_DESCRIPTION_CHARS
    struck = _struck_share(record) if policy.track_record.affects_triage else None

    low_effort = []
    if not described:
        low_effort.append("The description is empty or only a few words.")
    if facts.unlinked:
        low_effort.append("No linked issue.")
    if code_paths and not tests_changed:
        low_effort.append("Code changed with no test changes.")
    if struck is not None and struck[1] >= 3 and struck[0] / struck[1] >= 0.5:
        low_effort.append(f"{struck[0]} of the author's last {struck[1]} merged PRs were later reverted or fixed.")

    evidence = []
    if facts.ci_ok:
        evidence.append("CI passes.")
    if facts.linked_issues:
        evidence.append("Linked to " + ", ".join(f"#{n}" for n in facts.linked_issues[:3]) + ".")
    if tests_changed:
        evidence.append("Changes tests.")
    if matches_description == "yes":
        evidence.append("The change matches its description.")
    if struck is not None and struck[1] >= 3 and struck[0] == 0:
        evidence.append(f"None of the author's last {struck[1]} merged PRs needed a revert or fix.")

    if needs_human:
        key, reasons = "needs_human", needs_human
    elif needs_work:
        key, reasons = "needs_work", needs_work
    elif policy.triage.label_low_effort and len(low_effort) >= 3:
        key, reasons = "low_effort", low_effort
    else:
        key, reasons = "ready", []
    return Triage(key, reasons, evidence, _score(facts, evidence, needs_work, low_effort))


def _needs_human_reasons(findings: list[Finding]) -> list[str]:
    reasons, seen = [], set()
    for finding in findings:
        if finding.needs_human and (finding.rule, finding.path) not in seen:
            seen.add((finding.rule, finding.path))
            where = f" (`{finding.path}`)" if finding.path else ""
            reasons.append(f"{finding.title}{where}.")
    return reasons[:8]


def _struck_share(record: AuthorRecord | None) -> tuple[int, int] | None:
    if record is None:
        return None
    recent = record.merged[-10:]
    numbers = {pr.number for pr in recent}
    return len({s.pr.number for s in record.strikes if s.pr.number in numbers}), len(recent)


def _score(facts: PRFacts, evidence: list[str], needs_work: list[str], low_effort: list[str]) -> float:
    """Evidence for, minus the cost of reviewing: small, tested, linked PRs
    with passing CI come first."""
    size = facts.additions + facts.deletions
    cost = math.log2(1 + size) / 2 + len(facts.paths) / 20
    return len(evidence) - 1.5 * len(needs_work) - 0.5 * len(low_effort) - cost


def render_queue(rows: list[tuple[PRFacts, Triage | None]], policy: Policy) -> str:
    """The triage queue as one Markdown issue body."""
    lines = [
        "Open PRs sorted by Stratum. Within each group, the PRs that are cheapest to review and best supported come first.",
        "This list is rebuilt on a schedule; edits to it are overwritten.",
        "",
    ]
    order = {"ready": 0, "needs_human": 1, "needs_work": 2, "low_effort": 3}
    reviewed = [(f, t) for f, t in rows if t is not None]
    for key in sorted(order, key=order.get):
        group = sorted((pair for pair in reviewed if pair[1].key == key), key=lambda pair: -pair[1].score)
        if not group:
            continue
        lines += [f"### {label_for(policy, key)} ({len(group)})", ""]
        for facts, result in group:
            why = "; ".join(result.reasons or result.evidence)[:180]
            # Titles and finding titles (model text) alike: nobody named in them is pinged.
            lines.append(demention(f"- #{facts.number} {facts.title[:80]} · +{facts.additions}/−{facts.deletions}" + (f" · {why}" if why else "")))
        lines.append("")
    waiting = [f for f, t in rows if t is None]
    if waiting:
        lines += [f"### Not reviewed yet ({len(waiting)})", ""] + [demention(f"- #{f.number} {f.title[:80]}") for f in waiting] + [""]
    return "\n".join(lines)
