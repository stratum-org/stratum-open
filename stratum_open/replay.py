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

The report comes as the table for the terminal (`render`), as Markdown to
paste into an issue, a discussion or a job summary (`render_markdown`), or as
one HTML page (`render_html`). All three draw the per-PR table from `COLUMNS`.
"""

from __future__ import annotations

import html
import json
import math
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple

import requests

from stratum_open import __version__
from stratum_open.diff import parse_diff
from stratum_open.github.api import GitHub, GitHubError
from stratum_open.platform import CI
from stratum_open.policy import POLICY_PATH, Policy
from stratum_open.review import demention
from stratum_open.runner import QUEUE_JOB, Context, check_pull, load_context_policy, skip_reason
from stratum_open.triage import COLORS, KEYS, PRFacts, label_for, triage
from stratum_open.util import count

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
    requests and keeps the last rate-limit figures GitHub sent."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.remaining: int | None = None  # requests left this hour, once GitHub has said
        self.reset: int | None = None  # when the count starts again, in seconds since 1970 (UTC)

    def request(self, method, url, *args, **kwargs):
        if str(method).upper() != "GET":
            raise RuntimeError(f"replay only reads from GitHub: it won't send a {method} to {url}")
        self.calls += 1
        response = super().request(method, url, *args, **kwargs)
        left, reset = response.headers.get("X-RateLimit-Remaining", ""), response.headers.get("X-RateLimit-Reset", "")
        if left.isdigit() and response.headers.get("X-RateLimit-Resource", "core") == "core":
            self.remaining = int(left)
            self.reset = int(reset) if reset.isdigit() else self.reset
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
    url: str = ""  # the PR's page on GitHub


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
           resets: Callable[[], int | None] = lambda: None, log: Callable[[str], None] = lambda line: None) -> Report:
    """The last `last` closed PRs of `gh.repo`, newest first, each with the
    label it would have got. `remaining()` is the requests left this hour
    (None when unknown): below `RESERVE` the replay stops and keeps its rows.
    `resets()` is when that count starts again (seconds since 1970, or None),
    which the report gives when GitHub's rate limit stops it."""
    try:
        default_branch = gh.repository().get("default_branch") or "main"
        policy = load_context_policy(gh, default_branch)
    except GitHubError as exc:
        if not _rate_limited(exc):
            raise  # no such repo, or the token can't see it: the command says which
        return Report(gh.repo, Policy(), last, state, stopped=_stopped_by_limit(remaining(), resets()))
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
                report.stopped = f"GitHub's rate limit is nearly used up ({left} requests left). {_resets(resets())}"
                break
            if state != "all" and (state == "merged") != bool(pull.get("merged_at")):
                continue
            row = _row(ctx, pull, default_branch)
            report.rows.append(row)
            log(f"#{row.number} {_label(policy, row.key)} ({row.outcome})")
    except GitHubError as exc:  # GitHub or the network gave out part-way: the rows so far are kept
        report.stopped = _stopped_by_limit(remaining(), resets()) if _rate_limited(exc) else f"{str(exc).rstrip('.')}."
    except requests.RequestException as exc:
        report.stopped = f"Couldn't reach GitHub ({type(exc).__name__})."
    return report


def _rate_limited(exc: GitHubError) -> bool:
    return exc.rate_limited or exc.status == 429


def _stopped_by_limit(left: int | None, reset: int | None) -> str:
    """Why GitHub's rate limit stopped a replay, and when it can go on."""
    if left:  # requests are left this hour: it was the limit on requests in a short time
        return "GitHub's secondary rate limit stopped it: too many requests in a short time. Try again in a few minutes."
    return f"GitHub's rate limit is used up. {_resets(reset)}"


def _resets(reset: int | None, now: float | None = None) -> str:
    if reset is None:
        return "It resets within the hour."
    minutes = max(1, math.ceil((reset - (time.time() if now is None else now)) / 60))
    return f"It resets at {time.strftime('%H:%M', time.gmtime(reset))} UTC, in {count(minutes, 'minute')}."


def _row(ctx: Context, pull: dict, default_branch: str) -> Row:
    gh, policy = ctx.gh, ctx.policy
    row = Row(
        number=pull["number"], title=pull.get("title") or "", author=(pull.get("user") or {}).get("login", ""),
        outcome=MERGED if pull.get("merged_at") else UNMERGED, closed_at=pull.get("closed_at") or "", head=pull["head"]["sha"],
        url=pull.get("html_url") or "",
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
        facts.ci = gh.ci_state_at(row.head, row.closed_at, ignore={policy.review.check_name, ACTION_JOB, QUEUE_JOB})
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
        f"- Stratum's own check and its Action's job aren't CI, as in a live review, nor is the job that rebuilds the triage queue. "
        f"The jobs are known by the example workflow's names for them, `{ACTION_JOB}` and `{QUEUE_JOB}`; under other names they count as CI here.",
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


class Column(NamedTuple):
    """A column of the per-PR table, after the PR itself. Every format draws
    its table from `COLUMNS`, so a column added there shows in all three."""

    heading: str
    text: Callable[[Policy, Row], str]  # plain text, with code in backticks: each format escapes it its own way
    limit: int = 240  # the most a Markdown cell keeps


COLUMNS = [
    Column("Outcome", lambda policy, row: row.outcome),
    Column("Label", lambda policy, row: _label(policy, row.key)),
    Column("Why", lambda policy, row: row.note or " ".join(row.reasons or row.evidence)),
    Column("Checks' findings", lambda policy, row: ", ".join(f"`{rule}` {n}" for rule, n in row.findings.items()), limit=2000),
]
CAVEAT = ("No model reviewed these PRs and no author's track record was counted, so each label rests on the checks and the PR's "
          "facts alone. The end of the report says what else a replay can't rebuild.")


def disagreements(rows: list[Row]) -> list[Row]:
    """The PRs whose label and outcome disagree, the surest label first:
    marked ready but closed without merging, then marked needs work but
    merged."""
    ready = [row for row in rows if row.key == "ready" and row.outcome == UNMERGED]
    work = [row for row in rows if row.key == "needs_work" and row.outcome == MERGED]
    return sorted(ready, key=lambda row: -row.score) + sorted(work, key=lambda row: row.score)


def render(report: Report) -> str:
    """The report for the terminal, as Markdown: a row per PR, the table of
    label against outcome, and the footer."""
    lines = _head(report)
    if not report.rows:
        return "\n".join(lines)
    lines += [_intro(report), "", *_markdown_table(report.policy, report.rows, share=False), ""]
    lines += ["## Label against outcome", "", *_markdown_counts(report), "", _with_findings(report), ""]
    lines += ["## What this rests on", "", *notes(report.policy), "", *_request_count(report)]
    return "\n".join(lines)


def render_markdown(report: Report) -> str:
    """The report to share, in an issue, a discussion or a job summary: the
    counts, the PRs whose label and outcome disagree, every PR (folded), and
    what it rests on. No title in it can ping anyone or break a table, and
    no PR or issue number in it is linked (see `_markdown_table`)."""
    policy, rows = report.policy, report.rows
    lines = _head(report)
    if not rows:
        return "\n".join(lines)
    odd = disagreements(rows)
    lines += [_intro(report), "", "## Label against outcome", "", *_markdown_counts(report), "", _with_findings(report), "", CAVEAT, ""]
    lines += ["## Where the label and the outcome disagree", "", _disagreeing(policy, odd), ""]
    lines += [*_markdown_table(policy, odd, share=True), ""] if odd else []
    lines += ["## Every PR", "", "<details>", f"<summary>{count(len(rows), 'PR')}, newest first</summary>", "",
              *_markdown_table(policy, rows, share=True), "", "</details>", ""]
    lines += ["## What this rests on", "", *notes(policy), "", *_request_count(report)]
    return "\n".join(lines)


def render_html(report: Report) -> str:
    """The report as one HTML page that loads nothing: its style is inline,
    and it has no script and no image. All text in it is escaped."""
    policy, rows = report.policy, report.rows
    title = f"Replay of {report.repo}"
    body = [f"<h1>{_html(title)}</h1>"]
    if report.stopped:
        body.append(f'<p class="stopped"><strong>Stopped after {len(rows)} of {report.asked} PRs.</strong> {_html(report.stopped)}</p>')
    elif not rows:
        body.append(f"<p>The repo has no {_kinds(report.state)[1]}.</p>")
    if rows:
        odd = disagreements(rows)
        body += [f"<p>{_html(_intro(report))}</p>", "<h2>Label against outcome</h2>", _html_counts(report),
                 f"<p>{_html(_with_findings(report))}</p>", f'<p class="caveat">{_html(CAVEAT)}</p>',
                 "<h2>Where the label and the outcome disagree</h2>", f"<p>{_html(_disagreeing(policy, odd))}</p>"]
        body += [_html_table(policy, odd)] if odd else []
        body += [f"<h2>Every PR ({len(rows)}, newest first)</h2>", _html_table(policy, rows), "<h2>What this rests on</h2>", *_html_notes(policy)]
        body += [f'<p class="muted">{_html(line)}</p>' for line in _request_count(report) if line]
    return "\n".join([
        "<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">', f"<title>{_html(title)}</title>",
        f"<style>{_STYLE}</style>", "</head>", "<body>", "<main>", *body, "</main>", "</body>", "</html>", "",
    ])


def as_dict(report: Report) -> dict:
    """The report's data, for `--json`."""
    return {
        "repo": report.repo, "state": report.state, "asked": report.asked, "version": __version__, "policy": report.policy.source,
        "stopped": report.stopped, "requests": report.requests,
        "pulls": [{**asdict(row), "label": label_for(report.policy, row.key) if row.key else ""} for row in report.rows],
        "summary": summary(report),
        "notes": [line.removeprefix("- ") for line in notes(report.policy) if line.startswith("- ")],
    }


# What every format says, before it's written as Markdown or HTML.


def _kinds(state: str) -> tuple[str, str]:
    return {"all": ("closed PR", "closed PRs"), "merged": ("merged PR", "merged PRs"),
            "closed": ("PR closed without merging", "PRs closed without merging")}[state]


def _head(report: Report) -> list[str]:
    lines = [f"# Replay of {report.repo}", ""]
    if report.stopped:
        return lines + [f"**Stopped after {len(report.rows)} of {report.asked} PRs.** {report.stopped}", ""]
    return lines + ([] if report.rows else [f"The repo has no {_kinds(report.state)[1]}.", ""])


def _intro(report: Report) -> str:
    rows = report.rows
    one, many = _kinds(report.state)
    return (f"The last {len(rows)} {one if len(rows) == 1 else many}, newest first: what Stratum Open's checks and triage would have said "
            "on each, beside what became of it. No model was run and nothing was posted."
            + (f" {report.asked} were asked for; the repo has no more." if len(rows) < report.asked and not report.stopped else ""))


def _counts(report: Report) -> list[tuple[str | None, str, dict[str, int]]]:
    """The table of label against outcome, a row each: the label's key (""
    for the PRs not reviewed, None for the total), its name, and the PRs by
    outcome."""
    totals = summary(report)
    table = [(key, _label(report.policy, key), totals["labels"][key]) for key in KEYS]
    if any(totals["not_reviewed"].values()):
        table.append(("", _label(report.policy, ""), totals["not_reviewed"]))
    table.append((None, "Total", {outcome: sum(1 for row in report.rows if row.outcome == outcome) for outcome in OUTCOMES}))
    return table


def _with_findings(report: Report) -> str:
    totals = summary(report)
    return f"{totals['with_findings']} of the {totals['replayed']} PRs replayed had at least one finding from the checks."


def _disagreeing(policy: Policy, odd: list[Row]) -> str:
    ready, work = label_for(policy, "ready"), label_for(policy, "needs_work")
    if not odd:
        return f"None: no PR marked `{ready}` was closed without merging, and none marked `{work}` was merged."
    return (f"Marked `{ready}` but closed without merging, or `{work}` but merged: the PRs to look at first, "
            "the surest label first.")


def _request_count(report: Report) -> list[str]:
    if report.requests is None:
        return []
    return [f"GitHub requests: {report.requests}, about {report.requests / len(report.rows):.1f} per PR.", ""]


def _label(policy: Policy, key: str) -> str:
    return label_for(policy, key) if key else "not reviewed"


def _link(url: str) -> bool:
    """A PR's page as GitHub gave it, fit to link to."""
    return bool(re.fullmatch(r"https?://[^\s()<>\"'`\\]+", url))


# Markdown


def _markdown_table(policy: Policy, rows: list[Row], *, share: bool) -> list[str]:
    """The per-PR table. To `share`, every PR and issue number is in code,
    the PR's own too: pasted into an issue or a discussion, a number GitHub
    links adds a "mentioned this" line to that PR or issue, which for a
    50-PR report is 50 lines or more on other people's PRs."""
    lines = ["| PR | " + " | ".join(column.heading for column in COLUMNS) + " |", "| --- |" + " --- |" * len(COLUMNS)]
    for row in rows:
        cells = [f"{f'`#{row.number}`' if share else f'#{row.number}'} {_cell(row.title, 80, references=not share)}",
                 *(_cell(column.text(policy, row), column.limit, references=not share) for column in COLUMNS)]
        lines.append(f"| {' | '.join(cells)} |")
    return lines


def _markdown_counts(report: Report) -> list[str]:
    lines = [f"| Label | {MERGED} | {UNMERGED} | Total |", "| --- | ---: | ---: | ---: |"]
    for _, name, counts in _counts(report):
        lines.append(f"| {_cell(name, 80)} | {counts[MERGED]} | {counts[UNMERGED]} | {counts[MERGED] + counts[UNMERGED]} |")
    return lines


def _cell(text: str, limit: int, *, references: bool = True) -> str:
    """Text for one table cell: one line of at most `limit` characters, with
    nothing that ends the cell or the table, leaves code open or pings anyone,
    and, without `references`, no issue or PR number GitHub would link."""
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
        text += "`" if text.count("`") % 2 else ""  # the cut fell inside a code span
    return _plain(demention(text), references=references).replace("|", "\\|")


_TICKS = re.compile(r"`+")
# What GitHub links to an issue or a PR, outside code: #12, owner/repo#12, GH-12, and their URLs.
_REFERENCE = re.compile(
    r"https?://[^\s`]+?/(?:issues|pull|discussions)/\d+[^\s`]*|(?<![\w./-])(?:[\w.-]+/[\w.-]+)?#\d+\b|\b(?i:gh)-\d+\b"
)


def _plain(text: str, *, references: bool = True) -> str:
    """`text` with each `<` outside code written `&lt;`, so GitHub renders no
    HTML from a PR's title: a `<details>` would fold the rest of the report,
    and a `</table>` would end the table. Without `references`, each issue or
    PR number outside code goes in code too, where GitHub doesn't link it.
    Code spans are found as GitHub's Markdown finds them, and are kept as
    they are: a `<` shows as itself there."""
    pieces = list(_pieces(text))
    # Backticks for the new code spans, as many as no unclosed run in the text has, so none of those can close one.
    loose = {len(run) for kind, piece in pieces if kind == "text" for run in _TICKS.findall(piece)}
    ticks = "`" * next(n for n in range(1, len(loose) + 2) if n not in loose)
    out: list[str] = []
    for index, (kind, piece) in enumerate(pieces):
        if kind == "text":
            piece = piece.replace("<", "&lt;")
            if not references:
                line = f"{out[-1][-1:] if out else ''}{piece}{pieces[index + 1][1][:1] if index + 1 < len(pieces) else ''}"
                shift = 1 if out else 0  # where `piece` starts in `line`

                def code(match: re.Match, line=line, shift=shift) -> str:
                    # Next to another backtick, the new ones would make a longer run: a space keeps them apart.
                    before = " " if line[match.start() + shift - 1:match.start() + shift] == "`" else ""
                    after = " " if line[match.end() + shift:match.end() + shift + 1] == "`" else ""
                    return f"{before}{ticks}{match.group()}{ticks}{after}"

                piece = _REFERENCE.sub(code, piece)
        out.append(piece)
    return "".join(out)


def _pieces(text: str):
    """`text` as GitHub's Markdown reads it, in order: ("code", a code span),
    ("escape", a backslash and the character it escapes) and ("text", the
    rest). A run of backticks with no closing run of its length is text."""
    start = at = 0
    while at < len(text):
        if text[at] == "\\":
            kind, end = "escape", at + 2
        elif text[at] == "`":
            run = _TICKS.match(text, at).group()
            close = next((tick for tick in _TICKS.finditer(text, at + len(run)) if tick.group() == run), None)
            if close is None:
                at += len(run)
                continue
            kind, end = "code", close.end()
        else:
            at += 1
            continue
        if start < at:
            yield "text", text[start:at]
        yield kind, text[at:end]
        start = at = end
    if start < len(text):
        yield "text", text[start:]


# HTML


def _html(text: str) -> str:
    """Text for the page: escaped, with code in backticks set as code."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(text))


def _html_table(policy: Policy, rows: list[Row]) -> str:
    heads = "".join(f"<th>{_html(column.heading)}</th>" for column in COLUMNS)
    lines = ['<div class="scroll"><table class="prs">', f"<thead><tr><th>PR</th>{heads}</tr></thead>", "<tbody>"]
    for row in rows:
        pr = f'<a href="{html.escape(row.url)}">#{row.number}</a>' if _link(row.url) else f"#{row.number}"
        cells = "".join(f"<td>{_html(column.text(policy, row))}</td>" for column in COLUMNS)
        lines.append(f"<tr><td>{pr} {_html(row.title)}</td>{cells}</tr>")
    return "\n".join(lines + ["</tbody>", "</table></div>"])


def _html_counts(report: Report) -> str:
    lines = ['<table class="counts">', f"<thead><tr><th>Label</th><th>{MERGED}</th><th>{UNMERGED}</th><th>Total</th></tr></thead>", "<tbody>"]
    for key, name, counts in _counts(report):
        dot = f'<span class="dot" style="background:#{COLORS[key]}"></span>' if key in COLORS else ""
        numbers = "".join(f"<td>{n}</td>" for n in (counts[MERGED], counts[UNMERGED], counts[MERGED] + counts[UNMERGED]))
        lines.append(f'<tr{" class=total" if key is None else ""}><td>{dot}{_html(name)}</td>{numbers}</tr>')
    return "\n".join(lines + ["</tbody>", "</table>"])


def _html_notes(policy: Policy) -> list[str]:
    """`notes`, whose lines are a heading ending in a colon, then its points."""
    out: list[str] = []
    for line in notes(policy):
        if line.startswith("- "):
            out.append(f"<li>{_html(line[2:])}</li>")
        elif line:
            out += ["</ul>"] if out else []
            out += [f"<h3>{_html(line.rstrip(':'))}</h3>", "<ul>"]
    return out + ["</ul>"]


_STYLE = """
:root { color-scheme: light dark; --text: #1f2328; --muted: #59636e; --page: #ffffff; --line: #d1d9e0; --shade: #f6f8fa;
  --link: #0969da; --warn: #9a6700; --warn-shade: #fff8c5; }
@media (prefers-color-scheme: dark) {
  :root { --text: #f0f6fc; --muted: #9198a1; --page: #0d1117; --line: #3d444d; --shade: #151b23;
    --link: #4493f8; --warn: #d29922; --warn-shade: #272115; }
}
body { margin: 0; background: var(--page); color: var(--text);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 32px 16px 48px; }
h1 { font-size: 1.75em; margin: 0 0 12px; }
h2 { font-size: 1.25em; margin: 36px 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
h3 { font-size: 1em; margin: 20px 0 4px; }
p, ul { margin: 8px 0; }
li { margin: 2px 0; }
a { color: var(--link); }
code { font: 0.85em ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; background: var(--shade);
  padding: 0.1em 0.35em; border-radius: 4px; }
.muted { color: var(--muted); }
.caveat { color: var(--muted); max-width: 52em; }
.stopped { border: 1px solid var(--warn); background: var(--warn-shade); padding: 8px 12px; border-radius: 6px; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; font-size: 14px; }
th, td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: var(--shade); }
table.prs { width: 100%; }
table.prs td:first-child { min-width: 16em; }
table.counts td + td, table.counts th + th { text-align: right; font-variant-numeric: tabular-nums; }
tr.total td { font-weight: 600; }
.dot { display: inline-block; width: 0.7em; height: 0.7em; border-radius: 50%; margin-right: 0.5em; }
"""


# ── the command ──────────────────────────────────────────────────────────


FORMATS: dict[str | None, Callable[[Report], str]] = {None: render, "markdown": render_markdown, "html": render_html}


def run(repo: str, *, token: str, api: str, last: int, state: str, as_json: bool, out: str | None,
        report_format: str | None = None) -> int:
    """`stratum-open replay`: the report on stdout (JSON with `--json`), and
    in `out` when given, in `report_format` (None: the terminal's). An HTML
    page written to `out` isn't printed too. 2 when it stopped early; the
    rows it had are still in the report."""
    if last < 1:
        sys.exit("--last must be at least 1.")
    session = ReadOnlySession()
    gh = GitHub(token, repo, api=api, session=session)
    report = replay(gh, last=last, state=state, remaining=lambda: session.remaining, resets=lambda: session.reset,
                    log=lambda line: print(line, file=sys.stderr))
    report.requests = session.calls
    text = FORMATS[report_format](report)
    if out:
        Path(out).write_text(text, encoding="utf-8")
    if as_json:
        print(json.dumps(as_dict(report), indent=2))
    elif not (out and report_format == "html"):
        print(text)
    if report.stopped:
        print(f"Stopped after {len(report.rows)} of {last} PRs: {report.stopped}", file=sys.stderr)
    return 2 if report.stopped else 0
