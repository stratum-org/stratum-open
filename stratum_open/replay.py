"""`stratum-open replay owner/repo`: what the checks and triage would have
said on a repo's closed PRs, beside what became of each one.

It only reads. A maintainer sees the labels their own last PRs would have
got before installing anything, and the rows are the data for measuring
triage against what maintainers did: merged, or closed without merging.

Each PR is judged as it was at its last push, as far as GitHub still knows:

- the diff, the paths, the size and the files the checks read in full are
  those of the PR's final commit;
- CI is that commit's check runs, statuses and workflow runs as they stood
  when the PR was closed (`GitHub.ci_state_at`), without Stratum's own;
- linked issues are the closing keywords in the PR's description.

What can't be rebuilt afterwards is left out, never guessed, and the report
says so (`notes`): the model's review, the author's track record, the labels
the PR had then, what a package registry said that day. A track record as of
a day could be built from a clone (`build_records` takes a `ref` and a `now`),
but a replay has no clone, and it would take a walk of the history per PR and
today's commit emails to match the login.

Requests: two for the run (the repo, its policy) and one per 100 PRs listed;
then per PR one for the diff, three for CI (check runs, statuses, and, unless
a check failed, the workflow runs, for one held for approval, plus one per
workflow run started again after the PR closed), and one per file the checks
read in full (workflows, manifests, Python files).
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import requests

from stratum_open import __version__
from stratum_open.diff import parse_diff
from stratum_open.github.api import GitHub, GitHubError
from stratum_open.platform import CI
from stratum_open.policy import POLICY_PATH, Policy
from stratum_open.review import demention
from stratum_open.runner import Context, check_pull, load_context_policy, skip_reason
from stratum_open.triage import KEYS, PRFacts, label_for, triage

MERGED, UNMERGED = "merged", "closed unmerged"
OUTCOMES = (MERGED, UNMERGED)
# A replay stops when fewer requests than this are left this hour. A PR rarely
# costs more than 65 (the diff, CI, 60 files read in full), so one that's begun can finish.
RESERVE = 100
# The Action's job in examples/stratum.yml. A live review leaves its own job out of CI by the name
# the runner gives it; afterwards only the example's name is known.
ACTION_JOB = "stratum"
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# GitHub's closing keywords: "Fixes #12", "closes: owner/repo#12", "Resolved https://github.com/owner/repo/issues/12".
_CLOSES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?[ \t]+(?:https://github\.com/[\w.-]+/[\w.-]+/issues/|(?:[\w.-]+/[\w.-]+)?#)(\d+)\b",
    re.IGNORECASE,
)


class ReadOnlySession(requests.Session):
    """The replay's line to GitHub. It sends a GET and refuses anything else,
    so nothing under a replay can write to GitHub. It also counts the
    requests and keeps the last rate-limit figure GitHub sent."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.remaining: int | None = None  # requests left this hour, once GitHub has said

    def request(self, method, url, *args, **kwargs):
        if str(method).upper() != "GET":
            raise RuntimeError(f"replay only reads from GitHub: it won't send a {method} to {url}")
        self.calls += 1
        response = super().request(method, url, *args, **kwargs)
        left = response.headers.get("X-RateLimit-Remaining", "")
        if left.isdigit() and response.headers.get("X-RateLimit-Resource", "core") == "core":
            self.remaining = int(left)
        return response


@dataclass
class Row:
    number: int
    title: str
    author: str
    outcome: str  # merged | closed unmerged
    closed_at: str
    head: str  # the PR's final commit
    key: str = ""  # the triage key; "" when the PR wasn't replayed, and `note` says why
    reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    score: float = 0.0
    findings: dict[str, int] = field(default_factory=dict)  # the checks' findings: rule -> how many
    ci: CI | None = None
    linked_issues: list[int] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    files: int = 0
    note: str = ""


@dataclass
class Report:
    repo: str
    policy: Policy
    asked: int
    state: str  # all | merged | closed
    rows: list[Row] = field(default_factory=list)
    stopped: str = ""  # why it ended before `asked` PRs, when GitHub made it
    requests: int | None = None


def replay(gh: GitHub, *, last: int = 50, state: str = "all", remaining: Callable[[], int | None] = lambda: None,
           log: Callable[[str], None] = lambda line: None) -> Report:
    """The last `last` closed PRs of `gh.repo`, newest first, each with the
    label it would have got. `remaining()` is the requests left this hour
    (None when unknown): below `RESERVE` the replay stops and keeps its rows."""
    default_branch = gh.repository().get("default_branch") or "main"
    policy = load_context_policy(gh, default_branch)
    for error in policy.errors:
        log(f"policy: {error} Using the defaults.")
    ctx = Context(gh=gh, policy=policy, model=None, bot="-", log=log)
    report = Report(gh.repo, policy, last, state)
    try:
        for pull in gh.closed_pulls():
            if len(report.rows) >= last:
                break
            left = remaining()
            if left is not None and left < RESERVE:
                report.stopped = f"GitHub's rate limit is nearly used up ({left} requests left). It resets within the hour."
                break
            if state != "all" and (state == "merged") != bool(pull.get("merged_at")):
                continue
            row = _row(ctx, pull, default_branch)
            report.rows.append(row)
            log(f"#{row.number} {_label(policy, row.key)} ({row.outcome})")
    except GitHubError as exc:  # GitHub or the network gave out part-way: the rows so far are kept
        report.stopped = f"{str(exc).rstrip('.')}."
    except requests.RequestException as exc:
        report.stopped = f"Couldn't reach GitHub ({type(exc).__name__})."
    return report


def _row(ctx: Context, pull: dict, default_branch: str) -> Row:
    gh, policy = ctx.gh, ctx.policy
    row = Row(
        number=pull["number"], title=pull.get("title") or "", author=(pull.get("user") or {}).get("login", ""),
        outcome=MERGED if pull.get("merged_at") else UNMERGED, closed_at=pull.get("closed_at") or "", head=pull["head"]["sha"],
    )
    # The labels it had then aren't known, so `review.skip_labels` is left out.
    row.note = skip_reason({**pull, "labels": []}, None, policy) or ""
    if row.note:
        return row
    try:
        files = parse_diff(gh.pull_diff(row.number))
        engine = check_pull(ctx, pull, files)
        facts = PRFacts.from_pull(pull, [f.path for f in files])
        # GitHub's list of PRs has no sizes; the final commit's diff does.
        facts.additions, facts.deletions = sum(f.additions for f in files), sum(f.deletions for f in files)
        facts.ci = gh.ci_state_at(row.head, row.closed_at, ignore={policy.review.check_name, ACTION_JOB})
        facts.linked_issues = closing_issues(pull, default_branch)
    except GitHubError as exc:
        if exc.status in (401, 403, 429):  # the token is refused or out of requests: the next PR would fare no better
            raise
        row.note = f"Couldn't read it: GitHub answered {exc.status}."
        return row
    # No record: see the module's docstring.
    result = triage(facts, engine.findings, engine.policy, record=None)
    row.key, row.reasons, row.evidence, row.score = result.key, result.reasons, result.evidence, round(result.score, 2)
    by_rule = Counter(f.rule for f in engine.findings)
    row.findings = dict(sorted(by_rule.items(), key=lambda item: (-item[1], item[0])))
    row.ci, row.linked_issues = facts.ci, facts.linked_issues
    row.additions, row.deletions, row.files = facts.additions, facts.deletions, len(files)
    return row


def closing_issues(pull: dict, default_branch: str) -> list[int]:
    """The issues a PR's description says it closes, by GitHub's closing
    keywords. GitHub honours them only on a PR into the default branch, so
    this does too. A live review asks GitHub, which also knows the issues
    linked by hand in the sidebar; those can change after a PR is closed,
    while the description is the PR's own."""
    if ((pull.get("base") or {}).get("ref") or default_branch) != default_branch:
        return []
    body = _HTML_COMMENT.sub("", pull.get("body") or "")  # a PR template's own "fixes #123" isn't the author's
    return list(dict.fromkeys(int(number) for number in _CLOSES.findall(body)))[:10]


# ── the report ───────────────────────────────────────────────────────────


def notes(policy: Policy) -> list[str]:
    """The report's footer: where each fact comes from, and what a replay
    leaves out."""
    if policy.source == "file":
        rules = f"`{POLICY_PATH}` on the default branch"
    elif policy.errors:
        rules = f"the defaults, since `{POLICY_PATH}` can't be used"
    else:
        rules = f"the defaults, since the repo has no `{POLICY_PATH}`"
    return [
        "From each PR's last push:",
        "- The diff, the paths and the size are those of the PR's final commit, and the files the checks read in full are read at that commit.",
        "- CI is that commit's check runs and statuses as they stood when the PR was closed. A job run again later doesn't count. "
        "GitHub keeps check runs for about 400 days, so an older PR can show no CI.",
        "- CI whose workflows were still waiting for a maintainer's approval when the PR closed (a first-time contributor's PR) "
        "counts as held, even if they were approved afterwards.",
        f"- Stratum's own check and its Action's job aren't CI, as in a live review. The job is known by the example workflow's name "
        f"for it, `{ACTION_JOB}`; under another name it counts as CI here.",
        "",
        "From today:",
        f"- The policy is today's: {rules}.",
        "- The title and the description are as they read now. Linked issues are the closing keywords in the description (`Fixes #12`); "
        "an issue linked only in GitHub's sidebar isn't counted.",
        f"- The checks are those of stratum-open {__version__}.",
        "",
        "Left out, rather than guessed:",
        "- The model's review. A label here rests on the checks and the PR's facts alone. With a model, a critical finding "
        f"or a description that doesn't match the code makes a PR `{label_for(policy, 'needs_work')}`.",
        "- The author's track record as of that day, so `track_record.affects_triage` does nothing here.",
        "- The labels the PR had then, so `review.skip_labels` isn't applied.",
        "- Package registries: whether a new dependency existed, or was days old, on that day (`deps/missing`, `deps/young`).",
        "- What needs the repo's history or an earlier review: `boundaries.new_edges`, `/stratum ignore` and muted rules.",
    ]


def summary(report: Report) -> dict:
    counts = Counter((row.key, row.outcome) for row in report.rows)
    return {
        "labels": {key: {outcome: counts[key, outcome] for outcome in OUTCOMES} for key in KEYS},
        "not_reviewed": {outcome: counts["", outcome] for outcome in OUTCOMES},
        "replayed": sum(1 for row in report.rows if row.key),
        "with_findings": sum(1 for row in report.rows if row.findings),
    }


def render(report: Report) -> str:
    """The report as Markdown: a row per PR, the table of label against
    outcome, and the footer."""
    policy, rows = report.policy, report.rows
    one, many = {"all": ("closed PR", "closed PRs"), "merged": ("merged PR", "merged PRs"),
                 "closed": ("PR closed without merging", "PRs closed without merging")}[report.state]
    lines = [f"# Replay of {report.repo}", ""]
    if report.stopped:
        lines += [f"**Stopped after {len(rows)} of {report.asked} PRs.** {report.stopped}", ""]
    if not rows:
        return "\n".join(lines + ([] if report.stopped else [f"The repo has no {many}.", ""]))
    lines += [
        f"The last {len(rows)} {one if len(rows) == 1 else many}, newest first: what Stratum Open's checks and triage would have said "
        "on each, beside what became of it. No model was run and nothing was posted."
        + (f" {report.asked} were asked for; the repo has no more." if len(rows) < report.asked and not report.stopped else ""),
        "",
        "| PR | Outcome | Label | Why | Checks' findings |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        why = row.note or " ".join(row.reasons or row.evidence)
        found = ", ".join(f"`{rule}` {count}" for rule, count in row.findings.items())
        lines.append(f"| #{row.number} {_cell(row.title, 80)} | {row.outcome} | {_label(policy, row.key)} | {_cell(why, 240)} | {found} |")

    totals = summary(report)
    lines += ["", "## Label against outcome", "", f"| Label | {MERGED} | {UNMERGED} | Total |", "| --- | ---: | ---: | ---: |"]
    table = [(_label(policy, key), totals["labels"][key]) for key in KEYS]
    if any(totals["not_reviewed"].values()):
        table.append((_label(policy, ""), totals["not_reviewed"]))
    table.append(("Total", {outcome: sum(1 for row in rows if row.outcome == outcome) for outcome in OUTCOMES}))
    for name, counts in table:
        lines.append(f"| {name} | {counts[MERGED]} | {counts[UNMERGED]} | {counts[MERGED] + counts[UNMERGED]} |")
    lines += ["", f"{totals['with_findings']} of the {totals['replayed']} PRs replayed had at least one finding from the checks.", ""]

    lines += ["## What this rests on", "", *notes(policy), ""]
    if report.requests is not None:
        lines += [f"GitHub requests: {report.requests}, about {report.requests / len(rows):.1f} per PR.", ""]
    return "\n".join(lines)


def as_dict(report: Report) -> dict:
    """The report's data, for `--json`."""
    return {
        "repo": report.repo, "state": report.state, "asked": report.asked, "version": __version__, "policy": report.policy.source,
        "stopped": report.stopped, "requests": report.requests,
        "pulls": [{**asdict(row), "label": label_for(report.policy, row.key) if row.key else ""} for row in report.rows],
        "summary": summary(report),
        "notes": [line.removeprefix("- ") for line in notes(report.policy) if line.startswith("- ")],
    }


def _label(policy: Policy, key: str) -> str:
    return label_for(policy, key) if key else "not reviewed"


def _cell(text: str, limit: int) -> str:
    """Text for one table cell: one line of at most `limit` characters, with
    nothing that ends the cell, leaves code open or pings anyone."""
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
        text += "`" if text.count("`") % 2 else ""  # the cut fell inside a code span
    return demention(text).replace("|", "\\|")


# ── the command ──────────────────────────────────────────────────────────


def run(repo: str, *, token: str, api: str, last: int, state: str, as_json: bool, out: str | None) -> int:
    """`stratum-open replay`: the report on stdout (JSON with `--json`), and
    as Markdown in `out` when given. 2 when it stopped early; the rows it had
    are still printed."""
    if last < 1:
        sys.exit("--last must be at least 1.")
    session = ReadOnlySession()
    gh = GitHub(token, repo, api=api, session=session)
    report = replay(gh, last=last, state=state, remaining=lambda: session.remaining, log=lambda line: print(line, file=sys.stderr))
    report.requests = session.calls
    text = render(report)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    print(json.dumps(as_dict(report), indent=2) if as_json else text)
    if report.stopped:
        print(f"Stopped after {len(report.rows)} of {last} PRs: {report.stopped}", file=sys.stderr)
    return 2 if report.stopped else 0
