import pytest

from stratum_open.github.api import GitHub, GitHubError, _login
from stratum_open.platform import CI


def test_graphql_bot_logins_match_rest_ones_only_for_real_bots():
    assert _login({"login": "github-actions", "__typename": "Bot"}) == "github-actions[bot]"
    assert _login({"login": "stratum-open", "__typename": "User"}) == "stratum-open"  # a person, not the App
    assert _login(None) == ""


def test_a_label_with_a_slash_stays_one_path_segment(monkeypatch):
    gh = GitHub("t", "acme/widget")
    urls = []
    monkeypatch.setattr(gh, "request", lambda method, url, **kw: urls.append(url))
    gh.remove_label(7, "triage/needs work")
    gh.ensure_label("triage/ready", "ffffff", "")
    assert urls[0].endswith("/issues/7/labels/triage%2Fneeds%20work")
    assert urls[1].endswith("/labels/triage%2Fready")


def test_review_threads_carry_rest_style_logins(monkeypatch):
    gh = GitHub("t", "acme/widget")
    page = {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"id": "T1", "isResolved": False, "path": "a.py", "comments": {"nodes": [
            {"databaseId": 5, "body": "x <!-- stratum:finding 0123456789ab model:x -->", "author": {"login": "github-actions", "__typename": "Bot"}},
        ]}}],
    }}}}
    monkeypatch.setattr(gh, "graphql", lambda query, **variables: page)
    [thread] = gh.review_threads(4)
    assert thread["comments"][0]["author"] == "github-actions[bot]"

    from stratum_open.github.publish import thread_fingerprint

    assert thread_fingerprint(thread, "github-actions[bot]") == "0123456789ab"


def test_file_text_leaves_out_a_file_over_max_bytes_without_downloading_it(monkeypatch):
    import base64

    gh = GitHub("t", "acme/widget")
    small = {"type": "file", "size": 6, "encoding": "base64", "content": base64.b64encode(b"x = 1\n").decode()}
    # GitHub sends a file over 1 MB with no content, only its size and where to download it.
    big = {"type": "file", "size": 2_000_000, "encoding": "none", "content": "", "download_url": "https://raw.example/big.py"}
    monkeypatch.setattr(gh, "request", lambda method, url, **kw: big if url.endswith("big.py") else small)
    downloads = []
    monkeypatch.setattr(gh.session, "get", lambda url, **kw: downloads.append(url))
    assert gh.file_text("app/big.py", "a" * 40, max_bytes=1_000_000) is None
    assert downloads == []
    assert gh.file_text("app/small.py", "a" * 40, max_bytes=1_000_000) == "x = 1\n"
    assert gh.file_text("app/small.py", "a" * 40, max_bytes=5) is None
    assert gh.file_text("app/small.py", "a" * 40) == "x = 1\n"


def test_a_too_big_diff_keeps_a_move_as_a_move(monkeypatch):
    from stratum_open.diff import parse_diff
    from stratum_open.github.api import GitHubError

    gh = GitHub("t", "acme/widget")

    def refuse(*args, **kwargs):
        raise GitHubError(406, "diff too large")

    monkeypatch.setattr(gh, "request", refuse)
    monkeypatch.setattr(gh, "paginate", lambda path, **params: iter([
        {"filename": ".github/workflows/ci.yml", "previous_filename": "docs/ci.yml", "status": "renamed", "changes": 0},
        {"filename": "app/logo.png", "previous_filename": "logo.png", "status": "renamed", "changes": 2},  # edited, no patch
    ]))
    move, edited = parse_diff(gh.pull_diff(7))
    assert (move.status, move.old_path, move.path, move.binary) == ("renamed", "docs/ci.yml", ".github/workflows/ci.yml", False)
    assert edited.binary


def test_ci_is_read_as_it_stood_at_a_time(monkeypatch):
    gh = GitHub("t", "acme/widget")
    closed = "2026-03-02T10:00:00Z"
    asked = []
    found = {"check-runs": [], "statuses": [], "runs": []}

    def paginate(path, **params):
        asked.append((path.rsplit("/", 1)[1], params))
        return iter(found[path.rsplit("/", 1)[1]])

    def run(name, started, completed, conclusion, suite=1):
        return {"name": name, "check_suite": {"id": suite}, "status": "completed", "conclusion": conclusion,
                "started_at": f"2026-03-02T{started}:00Z", "completed_at": f"2026-03-02T{completed}:00Z"}

    def ci():
        return gh.ci_state_at("a" * 40, closed, ignore={"Stratum"})

    monkeypatch.setattr(gh, "paginate", paginate)
    assert ci() == CI.NONE  # no CI by then
    found["check-runs"] = [run("test", "09:00", "09:05", "failure"), run("test", "09:10", "09:15", "success")]
    assert ci() == CI.PASSED  # run again before the PR closed: the second run is the one that stood
    assert asked[-3] == ("check-runs", {"filter": "all"})  # GitHub's default hides the earlier runs
    found["check-runs"].append(run("test", "11:00", "11:05", "failure"))
    assert ci() == CI.PASSED  # run again after it closed: not what was known then
    found["check-runs"].append(run("test", "09:00", "09:05", "failure", suite=2))
    assert ci() == CI.FAILED  # another workflow's job of the same name is its own check
    found["check-runs"] = [run("test", "09:00", "09:05", "success"), run("docs", "09:50", "10:20", "failure")]
    assert ci() == CI.PENDING  # still running when the PR closed, however it ended
    found["check-runs"] = [run("test", "09:00", "09:05", "success"), run("Stratum", "09:00", "09:05", "failure")]
    assert ci() == CI.PASSED  # Stratum's own check isn't CI
    found["statuses"] = [  # newest first, as GitHub lists them
        {"context": "docs", "state": "error", "created_at": "2026-03-02T10:30:00Z"},
        {"context": "docs", "state": "success", "created_at": "2026-03-02T09:30:00Z"},
        {"context": "docs", "state": "pending", "created_at": "2026-03-02T09:00:00Z"},
    ]
    assert ci() == CI.PASSED  # each context's last status by then
    found["statuses"][1]["state"] = "failure"
    assert ci() == CI.FAILED


def workflow_run(name, conclusion, created="09:00", *, workflow=None, run_id=1, event="pull_request"):
    return {"id": run_id, "workflow_id": workflow or name, "name": name, "event": event, "status": "completed",
            "conclusion": conclusion, "created_at": f"2026-03-02T{created}:00Z", "updated_at": f"2026-03-02T{created}:00Z"}


def check_run(name, conclusion="success", status="completed"):
    return {"name": name, "check_suite": {"id": 1}, "status": status, "conclusion": conclusion,
            "started_at": "2026-03-02T09:00:00Z", "completed_at": "2026-03-02T09:01:00Z"}


def ci_reader(monkeypatch, found):
    """A client whose reads answer from `found`: check runs, workflow runs and statuses by the path's last part."""
    gh = GitHub("t", "acme/widget")
    asked = []

    def paginate(path, **params):
        asked.append((path.rsplit("/", 1)[1], params))
        value = found[path.rsplit("/", 1)[1]]
        if isinstance(value, Exception):
            raise value
        return iter(value)

    monkeypatch.setattr(gh, "paginate", paginate)
    monkeypatch.setattr(gh, "get", lambda path, **params: {"statuses": found["statuses"]})
    return gh, asked


def test_ci_held_for_a_maintainers_approval_is_its_own_state(monkeypatch):
    # A first-time contributor's fork PR (#162): `tests` waits for approval and made no check runs; the CLA check ran.
    found = {"check-runs": [check_run("cla"), check_run("Stratum")], "statuses": [], "runs": [workflow_run("tests", "action_required")]}
    gh, asked = ci_reader(monkeypatch, found)
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.HELD
    assert ("runs", {"head_sha": "a" * 40, "event": "pull_request"}) in asked
    found["check-runs"].append(check_run("lint", "failure"))
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.FAILED  # a check that ran and failed still counts first
    found["check-runs"].pop()
    found["check-runs"].append(check_run("lint", None, "in_progress"))
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.HELD  # held for approval beats still running
    found["check-runs"] = []
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.HELD  # nothing else ran at all: held, not "no CI"
    found["runs"] = [workflow_run("Stratum", "action_required")]
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.NONE  # Stratum's own workflow isn't CI


def test_a_workflow_that_ran_is_read_from_its_checks_as_before(monkeypatch):
    found = {"check-runs": [check_run("cla"), check_run("tests")], "statuses": [],
             "runs": [workflow_run("tests", "success"), workflow_run("CLA", "success", event="pull_request")]}
    gh, _ = ci_reader(monkeypatch, found)
    assert gh.ci_state("a" * 40, ignore=set()) == CI.PASSED
    # Approved and run again: the workflow's last run is the one that counts.
    found["runs"] = [workflow_run("tests", "action_required", "09:00", workflow=5, run_id=1), workflow_run("tests", "success", "09:30", workflow=5, run_id=2)]
    assert gh.ci_state("a" * 40, ignore=set()) == CI.PASSED


def test_a_token_that_cant_list_workflow_runs_still_gets_the_checks(monkeypatch):
    # Without `actions: read` on a private repo, GitHub refuses the list; the checks still decide, as before.
    found = {"check-runs": [check_run("tests")], "statuses": [], "runs": GitHubError(403, "Resource not accessible by integration")}
    gh, _ = ci_reader(monkeypatch, found)
    assert gh.ci_state("a" * 40, ignore=set()) == CI.PASSED
    found["runs"] = GitHubError(500, "boom")
    with pytest.raises(GitHubError):
        gh.ci_state("a" * 40, ignore=set())


def test_ci_held_when_the_pr_closed_is_held_in_a_replay(monkeypatch):
    closed = "2026-03-02T10:00:00Z"
    found = {"check-runs": [check_run("cla")], "statuses": [], "runs": [workflow_run("tests", "action_required", "09:00")]}
    gh, _ = ci_reader(monkeypatch, found)
    assert gh.ci_state_at("a" * 40, closed, ignore={"Stratum"}) == CI.HELD  # never approved
    found["runs"] = [workflow_run("tests", "action_required", "11:00")]
    assert gh.ci_state_at("a" * 40, closed, ignore={"Stratum"}) == CI.PASSED  # a run made after the PR closed wasn't there then
    found["runs"] = [workflow_run("tests", "action_required", "09:00", workflow=5, run_id=1),
                     workflow_run("tests", "success", "09:30", workflow=5, run_id=2)]
    assert gh.ci_state_at("a" * 40, closed, ignore={"Stratum"}) == CI.PASSED  # run again before it closed
