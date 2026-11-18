"""`stratum-open replay`: a repo's closed PRs through the checks and triage,
reading GitHub and never writing to it."""

import json

import pytest
import requests
from fakes import FakeGitHub

from stratum_open import cli
from stratum_open.github.api import GitHub, GitHubError
from stratum_open.platform import CI
from stratum_open.replay import RESERVE, ReadOnlySession, as_dict, closing_issues, render, replay

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
        self.broken: dict[int, int] = {}  # a PR -> the status GitHub answers for its diff

    def __getattribute__(self, name):
        value = super().__getattribute__(name)
        if callable(value) and not name.startswith("_") and name not in READS:
            self.refused.append(name)
            raise AssertionError(f"replay called gh.{name}(), which isn't one of its reads")
        return value

    def pull_diff(self, number):
        self.diffed.append(number)
        if number in self.broken:
            raise GitHubError(self.broken[number], "no")
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
        "merged_at": CLOSED_AT if merged else None, "closed_at": CLOSED_AT, **fields,
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
        return response

    monkeypatch.setattr(requests.Session, "request", send)
    session = ReadOnlySession()
    session.get("https://api.github.invalid/repos/acme/widget")
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        with pytest.raises(RuntimeError, match="replay only reads"):
            session.request(method, "https://api.github.invalid/repos/acme/widget/issues/7")
    with pytest.raises(RuntimeError, match="replay only reads"):  # so the real client can't write through it either
        GitHub("t", "acme/widget", session=session).comment(7, "hello")
    assert sent == ["GET"] and session.calls == 1 and session.remaining == 4321


def test_the_facts_are_those_of_the_prs_final_commit(gh):
    add(gh, 21, diff=MOVED_WORKFLOW)
    gh.files[(".github/workflows/ci.yml", head_of(21))] = PWN  # the workflow's text, as that commit has it
    gh.ci_of[head_of(21)] = CI.FAILED
    moved, _, _, clean = replay(gh).rows
    assert {"workflow/pwn-request", "workflow/write-all"} <= set(moved.findings)  # read in full at the head
    assert moved.key == "needs_human" and moved.ci == CI.FAILED and moved.head == head_of(21)
    assert gh.ci_asked[0] == (head_of(21), CLOSED_AT)  # that commit's CI, as it stood when the PR closed
    assert gh.ci_ignored == {"Stratum", "stratum"}  # Stratum's check and the Action's job aren't CI
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
