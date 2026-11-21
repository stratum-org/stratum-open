"""`stratum-open replay`: a repo's closed PRs through the checks and triage,
reading GitHub and never writing to it."""

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

import pytest
import requests
import yaml
from fakes import FakeGitHub

from stratum_open import __version__, cli, replay as replay_module
from stratum_open.github.api import GitHub, GitHubError
from stratum_open.platform import CI
from stratum_open.replay import (
    RESERVE, Column, ReadOnlySession, as_dict, closing_issues, disagreements, render, render_html, render_markdown, replay,
)

CLEAN = """diff --git a/src/dates.py b/src/dates.py
--- a/src/dates.py
+++ b/src/dates.py
@@ -1,2 +1,3 @@
 def parse(text):
-    return text
+    text = text.strip()
+    return text
diff --git a/tests/test_dates.py b/tests/test_dates.py
--- a/tests/test_dates.py
+++ b/tests/test_dates.py
@@ -1,2 +1,4 @@
 def test_parse():
     assert parse("x") == "x"
+
+    assert parse(" x ") == "x"
"""
SECRET = """diff --git a/src/client.py b/src/client.py
--- a/src/client.py
+++ b/src/client.py
@@ -1 +1,2 @@
 x = 1
+API_KEY = "sk-live-0123456789abcdef0123"
"""
BARE = """diff --git a/src/dates.py b/src/dates.py
--- a/src/dates.py
+++ b/src/dates.py
@@ -1,2 +1,2 @@
 def parse(text):
-    return text
+    return text.strip()
"""
NEW_DEPENDENCY = """diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1 +1,2 @@
 pyyaml>=6
+reqeusts>=2.31
"""
MOVED_WORKFLOW = "diff --git a/docs/ci.yml b/.github/workflows/ci.yml\nsimilarity index 100%\nrename from docs/ci.yml\nrename to .github/workflows/ci.yml\n"
PWN = """on: pull_request_target
permissions: write-all
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
"""
CLOSED_AT = "2026-03-02T10:00:00Z"
POLICY = (".stratum/policy.yml", "main")
READS = {"repository", "file_text", "closed_pulls", "pull_diff", "ci_state_at"}


class ClosedPRs(FakeGitHub):
    """A repo of closed PRs. A replay may make the five reads above and no
    other call: anything else on the fake, a write above all, fails the test,
    and is kept in `refused` in case something caught the failure."""

    def __init__(self):
        super().__init__(diff=CLEAN)
        self.pulls.clear()
        self.refused: list[str] = []
        self.diffed: list[int] = []  # the PRs whose diff was read, in order
        self.ci_asked: list[tuple[str, str]] = []
        self.ci_ignored: set[str] = set()
        self.ci_of: dict[str, CI] = {}  # a commit's CI, where it isn't `ci`
        self.broken: dict[int, int | GitHubError] = {}  # a PR -> the status GitHub answers for its diff, or the error

    def __getattribute__(self, name):
        value = super().__getattribute__(name)
        if callable(value) and not name.startswith("_") and name not in READS:
            self.refused.append(name)
            raise AssertionError(f"replay called gh.{name}(), which isn't one of its reads")
        return value

    def pull_diff(self, number):
        self.diffed.append(number)
        if number in self.broken:
            broken = self.broken[number]
            raise broken if isinstance(broken, GitHubError) else GitHubError(broken, "no")
        return super().pull_diff(number)

    def ci_state_at(self, sha, when, *, ignore):
        self.ci_asked.append((sha, when))
        self.ci_ignored = ignore
        return self.ci_of.get(sha, self.ci)


def head_of(number: int) -> str:
    return f"{number:040x}"


def add(gh, number, *, merged=True, diff=CLEAN, body="Fixes #3. Strips the input before parsing it, and tests that.", **fields):
    gh.pulls[number] = {
        "number": number, "state": "closed", "draft": False, "title": f"Change {number}", "body": body, "user": {"login": "carol"},
        "head": {"sha": head_of(number), "ref": f"feature-{number}"}, "base": {"ref": "main"}, "labels": [],
        "merged_at": CLOSED_AT if merged else None, "closed_at": CLOSED_AT, "html_url": f"https://github.com/acme/widget/pull/{number}", **fields,
    }
    gh.diffs[number] = diff


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(self, method, url, **kwargs):
        raise AssertionError(f"a request left for {url}")

    monkeypatch.setattr(requests.Session, "request", refuse)


@pytest.fixture
def gh():
    gh = ClosedPRs()
    add(gh, 11)  # tested, linked, green CI
    add(gh, 12, diff=SECRET)  # a key in the code
    add(gh, 13, merged=False, diff=BARE, body="")  # no description, no issue, no tests
    return gh


@pytest.fixture
def command(gh, monkeypatch):
    """`stratum-open replay ARGS` against the fake."""
    monkeypatch.setattr("stratum_open.replay.GitHub", lambda token, repo, **kwargs: gh)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    return lambda *args: cli.main(["replay", "acme/widget", *args])


def test_each_closed_pr_gets_its_label_beside_what_became_of_it(gh):
    rows = replay(gh).rows
    assert [(row.number, row.outcome, row.key) for row in rows] == [
        (13, "closed unmerged", "low_effort"), (12, "merged", "needs_work"), (11, "merged", "ready"),
    ]
    low, work, ready = rows
    assert low.reasons == ["The description is empty or only a few words.", "No linked issue.", "Code changed with no test changes."]
    assert work.findings == {"code/secret": 1} and work.reasons[0].startswith("1 serious finding to fix")
    assert ready.evidence == ["CI passes.", "Linked to #3.", "Changes tests."] and ready.findings == {}


def test_a_closed_pr_whose_ci_was_never_approved_is_not_ready(gh):
    gh.ci_of[head_of(11)] = CI.HELD  # a first-time contributor's tests, still waiting for approval when it closed
    report = replay(gh)
    held = report.rows[-1]
    assert (held.number, held.key, held.ci) == (11, "needs_human", CI.HELD) and "CI passes." not in held.evidence
    assert held.reasons == ["CI hasn't run: a maintainer needs to approve the workflows. Comment `/stratum review` once they've run to sort it again."]
    assert as_dict(report)["pulls"][-1]["ci"] == "held" and json.loads(json.dumps(as_dict(report)))["pulls"][-1]["ci"] == "held"


def test_replay_never_writes_to_github(gh, command):
    with pytest.raises(AssertionError):
        gh.comment(11, "the fake refuses this")
    gh.refused.clear()
    assert command() == 0
    assert gh.refused == []
    assert gh.checks == gh.reviews == gh.closed == gh.issues == gh.created_labels == gh.resolved == []
    assert not any(gh.issue_comments_.values()) and not any(gh.labels.values())


def test_the_replays_session_sends_a_get_and_nothing_else(monkeypatch):
    sent = []

    def send(self, method, url, **kwargs):
        sent.append(method)
        response = requests.Response()
        response.status_code = 200
        response.headers["X-RateLimit-Remaining"] = "4321"
        response.headers["X-RateLimit-Reset"] = "1795275498"
        return response

    monkeypatch.setattr(requests.Session, "request", send)
    session = ReadOnlySession()
    session.get("https://api.github.invalid/repos/acme/widget")
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        with pytest.raises(RuntimeError, match="replay only reads"):
            session.request(method, "https://api.github.invalid/repos/acme/widget/issues/7")
    with pytest.raises(RuntimeError, match="replay only reads"):  # so the real client can't write through it either
        GitHub("t", "acme/widget", session=session).comment(7, "hello")
    assert sent == ["GET"] and session.calls == 1 and (session.remaining, session.reset) == (4321, 1795275498)


def test_the_facts_are_those_of_the_prs_final_commit(gh):
    add(gh, 21, diff=MOVED_WORKFLOW)
    gh.files[(".github/workflows/ci.yml", head_of(21))] = PWN  # the workflow's text, as that commit has it
    gh.ci_of[head_of(21)] = CI.FAILED
    moved, _, _, clean = replay(gh).rows
    assert {"workflow/pwn-request", "workflow/write-all"} <= set(moved.findings)  # read in full at the head
    assert moved.key == "needs_human" and moved.ci == CI.FAILED and moved.head == head_of(21)
    assert gh.ci_asked[0] == (head_of(21), CLOSED_AT)  # that commit's CI, as it stood when the PR closed
    assert gh.ci_ignored == {"Stratum", "stratum", "Stratum triage queue"}  # Stratum's check and the Action's jobs aren't CI
    assert (clean.additions, clean.deletions, clean.files) == (4, 1, 2)  # counted from the diff: GitHub's list has no sizes


@pytest.mark.parametrize("body, base, issues", [
    ("Fixes #12", "main", [12]),
    ("This closes: #4, and RESOLVES acme/widget#5.\nfixed https://github.com/acme/widget/issues/6", "main", [4, 5, 6]),
    ("Fixes #12 and also fixes #12", "main", [12]),
    ("See #12; the prefix #13 stays", "main", []),
    ("<!-- fixes #99, as the template suggests -->\nA tidy-up.", "main", []),
    ("Fixes #12", "release-2", []),  # GitHub links issues only from a PR into the default branch
    (None, "main", []),
])
def test_linked_issues_are_the_closing_keywords_in_the_description(body, base, issues):
    assert closing_issues({"body": body, "base": {"ref": base}}, "main") == issues


def test_a_pr_the_policy_wouldnt_review_is_listed_and_costs_no_request(gh):
    gh.files[POLICY] = "review:\n  skip_authors: [dependabot]\n"
    add(gh, 14, merged=False, draft=True)
    add(gh, 15, user={"login": "dependabot"})
    report = replay(gh)
    by_bot, draft = report.rows[:2]
    assert (by_bot.key, by_bot.note) == ("", "PRs by dependabot aren't reviewed (review.skip_authors).")
    assert draft.key == "" and draft.note.startswith("Draft PR")
    assert gh.diffed == [13, 12, 11] and len(gh.ci_asked) == 3
    assert "| not reviewed | 1 | 1 | 2 |" in render(report)


def test_todays_labels_dont_decide_whether_a_pr_was_reviewed(gh):
    gh.files[POLICY] = "review:\n  skip_labels: [wontfix]\n"
    add(gh, 14, merged=False, labels=[{"name": "wontfix"}])  # put on when it was closed, for all a replay can tell
    assert replay(gh, last=1).rows[0].key == "ready"


def test_a_track_record_is_left_out_even_where_the_policy_counts_it(gh):
    gh.files[POLICY] = "track_record:\n  affects_triage: true\n"
    rows = replay(gh).rows  # asking GitHub for the author's commits would fail the test
    assert [row.key for row in rows] == ["low_effort", "needs_work", "ready"]
    assert not any("merged PRs" in line for row in rows for line in row.reasons + row.evidence)


def test_no_package_registry_is_asked(gh, monkeypatch):
    asked = []
    monkeypatch.setattr("stratum_open.checks.dependencies.PackageLookup.info", lambda self, ecosystem, name: asked.append(name))
    add(gh, 14, diff=NEW_DEPENDENCY)
    row = replay(gh, last=1).rows[0]
    assert row.findings == {"deps/lookalike": 1} and row.key == "needs_human"  # from the name alone
    assert asked == []


def test_state_and_last_choose_the_prs(gh):
    assert [row.number for row in replay(gh, state="merged").rows] == [12, 11]
    assert [row.number for row in replay(gh, state="closed").rows] == [13]
    assert [row.number for row in replay(gh, last=2).rows] == [13, 12]
    assert gh.diffed == [12, 11, 13, 13, 12]  # a PR that isn't wanted costs no request


def test_it_stops_before_the_rate_limit_runs_out_and_keeps_its_rows(gh):
    left = iter([5000, RESERVE, RESERVE - 1])
    report = replay(gh, remaining=lambda: next(left))
    assert [row.number for row in report.rows] == [13, 12]
    assert report.stopped == f"GitHub's rate limit is nearly used up ({RESERVE - 1} requests left). It resets within the hour."
    text = render(report)
    assert "**Stopped after 2 of 50 PRs.** GitHub's rate limit is nearly used up" in text and "| #12 Change 12 | merged |" in text


def test_a_pr_github_cant_serve_is_noted_and_the_rest_go_on(gh):
    gh.broken[12] = 502
    rows = replay(gh).rows
    assert [(row.number, row.key) for row in rows] == [(13, "low_effort"), (12, ""), (11, "ready")]
    assert rows[1].note == "Couldn't read it: GitHub answered 502."


def test_when_github_refuses_part_way_the_rows_so_far_are_printed(gh, command, capsys):
    gh.broken[12] = 403
    assert command() == 2
    out, err = capsys.readouterr()
    assert "| #13 Change 13 | closed unmerged |" in out and "| #12 " not in out
    assert "**Stopped after 1 of 50 PRs.** GitHub answered 403: no." in out
    assert "Stopped after 1 of 50 PRs: GitHub answered 403: no." in err


def test_the_report_counts_labels_against_outcomes_and_says_what_it_left_out(gh):
    gh.pulls[11]["title"] = "Strip | the input, thanks @mona"
    report = replay(gh)
    report.requests = 14
    text = render(report)
    assert "The last 3 closed PRs, newest first" in text
    assert "| #12 Change 12 | merged | triage: needs work | 1 serious finding to fix: " in text and "| `code/secret` 1 |" in text
    assert "| #11 Strip \\| the input, thanks `@mona` | merged | triage: ready | CI passes. Linked to #3. Changes tests. |  |" in text
    table = text.split("## Label against outcome")[1].split("## What this rests on")[0]
    assert table.split("\n")[2:9] == [
        "| Label | merged | closed unmerged | Total |",
        "| --- | ---: | ---: | ---: |",
        "| triage: needs a maintainer | 0 | 0 | 0 |",
        "| triage: needs work | 1 | 0 | 1 |",
        "| triage: low effort | 0 | 1 | 1 |",
        "| triage: ready | 1 | 0 | 1 |",
        "| Total | 2 | 1 | 3 |",
    ]
    assert "1 of the 3 PRs replayed had at least one finding from the checks." in table
    footer = text.split("## What this rests on")[1]
    for said in ("The policy is today's: the defaults, since the repo has no `.stratum/policy.yml`.", "A job run again later doesn't count.",
                 "Stratum's own check and its Action's job aren't CI",
                 "The model's review.", "`track_record.affects_triage` does nothing here.", "`review.skip_labels` isn't applied.",
                 "`deps/missing`, `deps/young`", "an issue linked only in GitHub's sidebar isn't counted"):
        assert said in footer, said
    assert "GitHub requests: 14, about 4.7 per PR." in footer


def test_json_and_out_carry_the_same_rows(command, capsys, tmp_path):
    out = tmp_path / "replay.md"
    assert command("--last", "2", "--state", "merged", "--json", "--out", str(out)) == 0
    data = json.loads(capsys.readouterr().out)
    assert [(p["number"], p["outcome"], p["key"], p["label"]) for p in data["pulls"]] == [
        (12, "merged", "needs_work", "triage: needs work"), (11, "merged", "ready", "triage: ready"),
    ]
    assert data["pulls"][0]["findings"] == {"code/secret": 1} and data["pulls"][1]["linked_issues"] == [3]
    assert data["summary"]["labels"]["ready"] == {"merged": 1, "closed unmerged": 0} and data["summary"]["with_findings"] == 1
    assert (data["repo"], data["state"], data["asked"], data["stopped"], data["policy"]) == ("acme/widget", "merged", 2, "", "defaults")
    assert any("track record" in note for note in data["notes"])
    report = out.read_text(encoding="utf-8")
    assert "The last 2 merged PRs, newest first" in report and "| #12 Change 12 | merged | triage: needs work |" in report


def test_a_repo_with_nothing_closed_and_a_count_below_one(command, gh, capsys):
    gh.pulls.clear()
    assert command() == 0
    assert "The repo has no closed PRs." in capsys.readouterr().out
    with pytest.raises(SystemExit, match="--last must be at least 1"):
        command("--last", "0")


# ── the reports to share: --format markdown and --format html ────────────


@pytest.fixture
def disagreeing(gh):
    """The three PRs, and one marked ready that was closed without merging.
    #12 (a key in the code) was merged though marked needs work."""
    add(gh, 14, merged=False)
    return gh


def test_the_markdown_report_leads_with_the_counts_and_where_label_and_outcome_disagree(disagreeing):
    report = replay(disagreeing)
    report.requests = 20
    text = render_markdown(report)
    sections = [line for line in text.splitlines() if line.startswith("#")]
    assert sections == ["# Replay of acme/widget", "## Label against outcome", "## Where the label and the outcome disagree",
                        "## Every PR", "## What this rests on"]
    counts = text.split("## Label against outcome")[1].split("##")[0]
    assert "| triage: ready | 1 | 1 | 2 |" in counts and "| triage: needs work | 1 | 0 | 1 |" in counts and "| Total | 2 | 2 | 4 |" in counts
    # What a replay can't rebuild is said beside the numbers, not only at the end.
    assert "No model reviewed these PRs and no author's track record was counted" in counts
    odd = text.split("## Where the label and the outcome disagree")[1].split("##")[0]
    assert "Marked `triage: ready` but closed without merging, or `triage: needs work` but merged" in odd
    assert [line.split(" ")[1] for line in odd.splitlines() if line.startswith("| `#")] == ["`#14`", "`#12`"]
    every = text.split("## Every PR")[1].split("## What this rests on")[0].strip()
    assert every.startswith("<details>\n<summary>4 PRs, newest first</summary>") and every.endswith("</details>")
    assert len([line for line in every.splitlines() if line.startswith("| `#")]) == 4
    footer = text.split("## What this rests on")[1]
    assert "The model's review." in footer and "`track_record.affects_triage` does nothing here." in footer
    assert "GitHub requests: 20, about 5.0 per PR." in footer


def test_disagreements_put_the_surest_label_first(disagreeing):
    add(disagreeing, 15, merged=False)
    rows = {row.number: row for row in replay(disagreeing).rows}
    assert (rows[15].key, rows[14].key, rows[12].key, rows[11].key) == ("ready", "ready", "needs_work", "ready")
    rows[11].key = "needs_work"  # merged, as if marked needs work
    rows[15].score, rows[14].score, rows[12].score, rows[11].score = 1.0, 3.0, -2.0, -5.0
    assert [row.number for row in disagreements(list(rows.values()))] == [14, 15, 11, 12]


def test_with_no_disagreement_the_report_says_so(gh):
    text = render_markdown(replay(gh, state="closed"))
    assert "None: no PR marked `triage: ready` was closed without merging, and none marked `triage: needs work` was merged." in text


HOSTILE = [
    # (a PR's title, its Markdown cell)
    ("Fix <details> folding", "Fix &lt;details> folding"),  # an open <details> would fold the rest of the report
    ("End </td></tr></table> here", "End &lt;/td>&lt;/tr>&lt;/table> here"),
    ("Hide <!-- the rest", "Hide &lt;!-- the rest"),
    ("Make `Vec<T>` Send", "Make `Vec<T>` Send"),  # inside code, < shows as itself
    ("\\`<b>`", "\\`&lt;b>`"),  # an escaped backtick opens no code
    ("\\\\<b>", "\\\\&lt;b>"),  # an escaped backslash doesn't escape the <
    ("`` a ` <b> ``", "`` a ` <b> ``"),
    ("``a`` ` <b> ``", "``a`` ` &lt;b> ``"),  # neither the lone backtick nor the last pair has a closer of its length
    ("a | b", "a \\| b"),
    ("Thanks @mona, /stratum review", "Thanks `@mona`, `/stratum` review"),
    ("line\nbreak\r\nand\ttab", "line break and tab"),
]


@pytest.mark.parametrize("title, cell", HOSTILE)
def test_a_title_in_a_markdown_report_can_neither_break_the_table_nor_ping_anyone(gh, title, cell):
    gh.pulls[11]["title"] = title
    report = replay(gh)
    for text in (render_markdown(report), render(report)):
        line = [line for line in text.splitlines() if line.startswith(("| `#11` ", "| #11 "))][-1]
        assert line.split(" ", 2)[2].startswith(f"{cell} | merged | triage: ready |"), line


@pytest.mark.parametrize("title, cell", [
    ("Fixes #12", "Fixes `#12`"),
    ("Port acme/gadget#3 and GH-4", "Port `acme/gadget#3` and `GH-4`"),
    ("As in https://github.com/acme/widget/pull/9/files", "As in `https://github.com/acme/widget/pull/9/files`"),
    ("Keep `#5` and (#6)", "Keep `#5` and (`#6`)"),  # already code, then a number in brackets
    ("`a`#7", "`a` `#7`"),  # side by side, the backticks would make one run of two
    ("odd ` #8", "odd ` ``#8``"),  # the lone backtick would close a code span of one
    ("issue#9 and x#10", "issue#9 and x#10"),  # GitHub links neither
])
def test_the_markdown_report_to_share_puts_every_pr_and_issue_number_in_code(gh, title, cell):
    """Pasted into an issue or a discussion, a number GitHub links adds a "mentioned this" line to that PR or issue."""
    gh.pulls[11]["title"] = title
    report = replay(gh)
    line = [line for line in render_markdown(report).splitlines() if line.startswith("| `#11` ")][-1]
    assert line == f"| `#11` {cell} | merged | triage: ready | CI passes. Linked to `#3`. Changes tests. |  |"
    line = [line for line in render(report).splitlines() if line.startswith("| #11 ")][-1]  # the terminal's table is as it was
    assert line.startswith("| #11 ") and line.endswith("| CI passes. Linked to #3. Changes tests. |  |")


def test_the_html_report_is_one_page_that_loads_nothing_and_escapes_every_title(disagreeing):
    disagreeing.pulls[11]["title"] = 'Fix `<script>alert(1)</script>` & "quotes" @mona'
    disagreeing.pulls[13]["html_url"] = "javascript:alert(1)"
    page = render_html(replay(disagreeing))
    assert page.startswith("<!doctype html>") and page.rstrip().endswith("</html>") and "<title>Replay of acme/widget</title>" in page
    # Nothing to fetch: no script, no stylesheet, image or font to load.
    assert not re.search(r"<script|<link|<img|<iframe|src=|@import|url\(", page, re.IGNORECASE)
    assert "<code>&lt;script&gt;alert(1)&lt;/script&gt;</code> &amp; &quot;quotes&quot; @mona" in page
    assert re.findall(r'href="([^"]*)"', page) == [f"https://github.com/acme/widget/pull/{n}" for n in (14, 12, 14, 12, 11)]
    assert "<td>#13 Change 13</td>" in page  # its URL isn't a web page's, so it isn't a link
    for said in ("<h2>Label against outcome</h2>", "No model reviewed these PRs", "<h2>Where the label and the outcome disagree</h2>",
                 "<h2>Every PR (4, newest first)</h2>", "<h3>Left out, rather than guessed</h3>", "<li>The model&#x27;s review."):
        assert said in page, said
    assert '<span class="dot" style="background:#0e8a16"></span>triage: ready</td><td>1</td><td>1</td><td>2</td>' in page


def test_a_column_added_to_columns_shows_in_every_format(gh, monkeypatch):
    """A new fact about each PR takes one entry in COLUMNS, not a change to each format."""
    monkeypatch.setattr(replay_module, "COLUMNS", [*replay_module.COLUMNS, Column("Agent", lambda policy, row: f"agent of #{row.number}")])
    report = replay(gh)
    assert "| PR | Outcome | Label | Why | Checks' findings | Agent |" in render(report)
    assert "| CI passes. Linked to #3. Changes tests. |  | agent of #11 |" in render(report)
    assert "| --- | --- | --- | --- | --- | --- |" in render_markdown(report) and "| agent of `#12` |" in render_markdown(report)
    assert "<th>Agent</th>" in render_html(report) and "<td>agent of #13</td>" in render_html(report)


def test_format_chooses_what_is_printed_and_written(command, capsys, tmp_path):
    page = tmp_path / "replay.html"
    assert command("--format", "html", "--out", str(page)) == 0
    assert capsys.readouterr().out == ""  # an HTML page goes only to the file
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")
    report = tmp_path / "replay.md"
    assert command("--format", "markdown", "--out", str(report)) == 0
    printed = capsys.readouterr().out
    assert printed == report.read_text(encoding="utf-8") + "\n" and "## Where the label and the outcome disagree" in printed
    assert command("--format", "html") == 0 and capsys.readouterr().out.startswith("<!doctype html>")
    assert command("--format", "html", "--json") == 0 and json.loads(capsys.readouterr().out)["repo"] == "acme/widget"


# ── rate limits ──────────────────────────────────────────────────────────


def in_minutes(minutes: float) -> tuple[int, str]:
    """A reset time `minutes` from now, and its time of day as the report gives it."""
    reset = int(time.time() + minutes * 60)
    return reset, time.strftime("%H:%M", time.gmtime(reset))


def test_a_rate_limit_stops_the_replay_with_how_far_it_got_and_when_the_limit_resets(gh):
    gh.broken[12] = GitHubError(403, "API rate limit exceeded for user ID 1", rate_limited=True)
    reset, clock = in_minutes(23.5)
    report = replay(gh, remaining=lambda: 0 if 12 in gh.diffed else 4000, resets=lambda: reset)
    assert [row.number for row in report.rows] == [13]
    assert report.stopped == f"GitHub's rate limit is used up. It resets at {clock} UTC, in 24 minutes."
    assert render_markdown(report).startswith(f"# Replay of acme/widget\n\n**Stopped after 1 of 50 PRs.** {report.stopped}\n")
    assert '<p class="stopped"><strong>Stopped after 1 of 50 PRs.</strong> GitHub&#x27;s rate limit is used up.' in render_html(report)


def test_github_saying_slow_down_is_told_apart_from_the_hours_limit(gh):
    gh.broken[12] = GitHubError(403, "You have exceeded a secondary rate limit", rate_limited=True)
    report = replay(gh, remaining=lambda: 4000)  # requests are left this hour
    assert report.stopped == ("GitHub's secondary rate limit stopped it: too many requests in a short time. "
                              "Try again in a few minutes.")
    gh.broken[12] = GitHubError(429, "Too many requests")
    assert replay(gh).stopped == "GitHub's rate limit is used up. It resets within the hour."


def test_a_rate_limit_before_the_first_pr_is_a_report_too(gh):
    def limited():
        raise GitHubError(403, "API rate limit exceeded for user ID 1", rate_limited=True)

    gh.repository = limited
    reset, clock = in_minutes(1)
    report = replay(gh, remaining=lambda: 0, resets=lambda: reset)
    assert report.rows == [] and report.stopped == f"GitHub's rate limit is used up. It resets at {clock} UTC, in 1 minute."
    assert render_markdown(report) == f"# Replay of acme/widget\n\n**Stopped after 0 of 50 PRs.** {report.stopped}\n"

    def missing():
        raise GitHubError(404, "Not Found")

    gh.repository = missing  # any other refusal is the command's to explain
    with pytest.raises(GitHubError):
        replay(gh)


def test_stopping_before_the_limit_runs_out_says_when_it_resets(gh):
    reset, clock = in_minutes(59.2)
    left = iter([5000, RESERVE - 1])
    report = replay(gh, remaining=lambda: next(left), resets=lambda: reset)
    assert report.stopped == f"GitHub's rate limit is nearly used up ({RESERVE - 1} requests left). It resets at {clock} UTC, in 60 minutes."


# ── examples/try-stratum.yml ─────────────────────────────────────────────

TRY = Path(__file__).parent.parent / "examples" / "try-stratum.yml"


def try_workflow() -> dict:
    return yaml.safe_load(TRY.read_text(encoding="utf-8"))


def try_steps() -> list[dict]:
    return [step for job in try_workflow()["jobs"].values() for step in job["steps"]]


def test_the_try_workflow_runs_by_hand_reads_only_and_pins_its_actions():
    flow = try_workflow()
    assert list(flow.get("on", flow.get(True))) == ["workflow_dispatch"]  # YAML 1.1 reads a bare `on` as true
    assert set(flow["permissions"].values()) == {"read"} and all("permissions" not in job for job in flow["jobs"].values())
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]) for step in try_steps() if "uses" in step)
    assert "secrets." not in TRY.read_text(encoding="utf-8")
    runs = [step["run"] for step in try_steps() if "run" in step]
    assert len(runs) == 2 and not any(re.search(r"\bgh\b|\bcurl\b|--post", run) for run in runs)  # it posts nothing


def pinned_install() -> tuple[str | None, str | None]:
    """What the example installs: (a commit, a released version), one of them None."""
    [install] = [step["run"] for step in try_steps() if "pip install" in step.get("run", "")]
    spec = shlex.split(install)[-1]
    commit = re.fullmatch(r"stratum-open @ git\+https://github\.com/stratum-org/stratum-open@([0-9a-f]{40})", spec)
    release = re.fullmatch(r"stratum-open==(\d+(?:\.\d+)+)", spec)
    assert commit or release, f"{spec} is neither a commit nor a released version"
    return (commit.group(1) if commit else None), (release.group(1) if release else None)


def test_the_try_workflow_installs_a_commit_or_a_release_never_a_branch():
    """Whoever copies the example runs what it installs, with a token that
    can read their repo, so what it installs must not change under them.
    0.3.0, the latest release, has no `replay --format`, so until the next
    release it's a commit. The release that bumps the version fails this
    test until the example installs that release."""
    commit, release = pinned_install()
    if __version__ == "0.3.0":
        assert commit is not None
    else:
        assert release == __version__


def test_the_commit_the_try_workflow_installs_is_on_main_and_has_replay_format():
    commit, _ = pinned_install()
    if commit is None:
        pytest.skip("the example installs a release")
    root = Path(__file__).parent.parent

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8")

    try:
        known = git("cat-file", "-e", f"{commit}^{{commit}}").returncode == 0
    except OSError:
        pytest.skip("no git here")
    if not known and git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        pytest.skip(f"this shallow clone may not have {commit[:7]}")
    assert known, f"{commit} isn't a commit of this repo"
    assert git("merge-base", "--is-ancestor", commit, "HEAD").returncode == 0, f"{commit} isn't on main"
    assert 'add_argument("--format", choices=["markdown", "html"]' in git("show", f"{commit}:stratum_open/cli.py").stdout


def test_the_try_workflow_writes_the_markdown_report_to_the_job_summary(command, tmp_path, monkeypatch):
    [step] = [step for step in try_steps() if "stratum-open replay" in step.get("run", "")]
    assert step["env"] == {"GITHUB_TOKEN": "${{ github.token }}", "LAST": "${{ inputs.last }}"}
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    flow = try_workflow()
    last = flow.get("on", flow.get(True))["workflow_dispatch"]["inputs"]["last"]["default"]
    for name, value in {"GITHUB_REPOSITORY": "acme/widget", "GITHUB_STEP_SUMMARY": str(summary), "LAST": str(last)}.items():
        monkeypatch.setenv(name, value)
    program, *args = [os.path.expandvars(word) for word in shlex.split(step["run"])]
    assert program == "stratum-open" and cli.main(args) == 0
    report = summary.read_text(encoding="utf-8")
    assert report.startswith("# Replay of acme/widget") and "## Where the label and the outcome disagree" in report
    assert "The last 3 closed PRs" in report and "50 were asked for; the repo has no more." in report
