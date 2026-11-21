"""Whether a coding agent wrote a PR, from what agents leave on the PRs they
open: the account that opened it, the addresses on its commits, the footer in
its description, its branch's name, and, weakest, a change to the files that
steer agents.

These are an agent's own signatures, listed in `agent_prs.yml` with a public
source for each, not a guess at how the code reads. A person who used an
editor's completions leaves none of them. Everything comes from the PR's
metadata and its commits, which the Action can read on a PR from a fork too.

The result is `likely` when a strong or medium signal fires, `possible` when
only weak ones do, and `no` otherwise. Only `likely` can change triage, and
only where `triage.agent_prs` says so; `possible` is a line in the summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from importlib import resources

import yaml

from stratum_open.checks.agents import instruction_file
from stratum_open.policy import Policy, matches_any

DATA = "agent_prs.yml"
LIKELY, POSSIBLE, NO = "likely", "possible", "no"
STRONG, MEDIUM, WEAK = "strong", "medium", "weak"
MAX_COMMITS = 100  # one page of the PR's commits: one API call, whatever its size
# The address in a `Co-authored-by: Name <address>` trailer.
_TRAILER = re.compile(r"^[ \t]*co-authored-by:[^\n<]*<([^<>\s]+)>[ \t]*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Agent:
    name: str
    accounts: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Signal:
    kind: str  # author | commits | description | branch | instructions
    strength: str  # strong | medium | weak
    text: str  # what fired, as the summary says it


@dataclass
class AgentResult:
    verdict: str = NO  # likely | possible | no
    signals: list[Signal] = field(default_factory=list)
    commits_read: bool = True  # False: the commits couldn't be read, so they're not known either way

    @property
    def likely(self) -> bool:
        return self.verdict == LIKELY

    def line(self) -> str:
        """The summary's line; "" when nothing fired."""
        if not self.signals:
            return ""
        lead = "Likely written by a coding agent" if self.likely else "Possibly written by a coding agent"
        line = f"**{lead}:** " + "; ".join(signal.text for signal in self.signals) + "."
        if not self.likely:
            line += " These are weak signals, so they don't change the triage."
        return line


def wanted(policy: Policy) -> bool:
    """Whether the policy asks for the check: any of `triage.agent_prs` or
    `review.agent_prs_logic_pass`. Off, a review neither reads the PR's
    commits nor says anything about agents."""
    rules = policy.triage.agent_prs
    return rules.label or rules.needs_maintainer or rules.require_tests or policy.review.agent_prs_logic_pass


@cache
def known() -> tuple[Agent, ...]:
    """The agents in `agent_prs.yml`, shipped with the package."""
    return parse(resources.files("stratum_open").joinpath(DATA).read_text(encoding="utf-8"))


def parse(text: str) -> tuple[Agent, ...]:
    data = yaml.safe_load(text) or {}
    agents = []
    for entry in data.get("agents") or ():
        values = {key: tuple(str(v) for v in entry.get(key) or ()) for key in ("accounts", "emails", "markers", "branches", "sources")}
        agents.append(Agent(name=str(entry["name"]), **values))
    return tuple(agents)


def detect(pull: dict, paths: list[str], commits: list[dict] | None, agents: tuple[Agent, ...] | None = None) -> AgentResult:
    """`pull` in GitHub's shape (its user, body and head branch), the paths it
    changes, and its commits as `pulls/{n}/commits` lists them; None when
    they couldn't be read."""
    agents = known() if agents is None else agents
    signals: list[Signal] = []

    def found(kind: str, strength: str, text: str) -> None:
        signals.append(Signal(kind, strength, text))

    login = ((pull.get("user") or {}).get("login") or "").lower()
    by_author = [a for a in agents if login and login in _lower(a.accounts)]
    if by_author:
        found("author", STRONG, f"the author is `{(pull.get('user') or {}).get('login')}` ({by_author[0].name})")

    if commits:
        read = commits[:MAX_COMMITS]
        by_commit = [{a.name for a in agents if not _addresses(c).isdisjoint(_lower(a.emails))} for c in read]
        names = [a.name for a in agents if any(a.name in agents_on for agents_on in by_commit)]
        if names:
            hits = sum(1 for agents_on in by_commit if agents_on)
            found("commits", STRONG, f"{_commits(hits, len(read), int(pull.get('commits') or 0))} by or co-authored by {_and(names)}")

    body = (pull.get("body") or "").lower()
    by_marker = [(a, m) for a in agents for m in a.markers if m.lower() in body]
    if by_marker:
        agent, marker = by_marker[0]
        shown = marker.removeprefix("<!--").removesuffix("-->").strip()  # a comment in the summary would be one there too
        found("description", MEDIUM, f"the description carries {agent.name}'s footer (`{_code(shown)}`)")

    branch = ((pull.get("head") or {}).get("ref") or "")
    by_branch = [a for a in agents if branch and matches_any(branch.lower(), _lower(a.branches))]
    if by_branch:
        found("branch", WEAK, f"its branch `{_code(branch)}` is named the way {by_branch[0].name} names its own")

    steering = [p for p in paths if instruction_file(p)]
    if steering:
        found("instructions", WEAK, f"it changes a file that steers coding agents (`{_code(steering[0])}`)")

    verdict = LIKELY if any(s.strength != WEAK for s in signals) else POSSIBLE if signals else NO
    return AgentResult(verdict, signals, commits_read=commits is not None)


def _commits(hits: int, read: int, total: int) -> str:
    """'its commit is', 'all 3 commits are', '2 of 5 commits are', with 'the
    first' when the PR has more commits than were read."""
    if total > read:
        return f"{hits} of the first {read} commits {'is' if hits == 1 else 'are'}"
    if read == 1:
        return "its commit is"
    return f"all {read} commits are" if hits == read else f"{hits} of {read} commits {'is' if hits == 1 else 'are'}"


def _addresses(commit: dict) -> set[str]:
    """The commit's author's address and its co-authors', lower case."""
    inner = commit.get("commit") or {}
    found = {((inner.get("author") or {}).get("email") or "").lower()}
    found |= {address.lower() for address in _TRAILER.findall(inner.get("message") or "")}
    return found - {""}


@cache
def _lower(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.lower() for value in values)


def _code(text: str) -> str:
    """Text a contributor chose (a branch, a path) for one code span: one line,
    no backticks, at most 80 characters."""
    text = " ".join(text.replace("`", "").split())
    return text if len(text) <= 80 else text[:79] + "…"


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
