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


def workflow_run(name, conclusion, created="09:00", *, workflow=None, run_id=1, event="pull_request", attempt=1, started=None):
    return {"id": run_id, "workflow_id": workflow or name, "name": name, "event": event, "status": "completed", "conclusion": conclusion,
            "run_attempt": attempt, "created_at": f"2026-03-02T{created}:00Z", "run_started_at": f"2026-03-02T{started or created}:00Z"}


def check_run(name, conclusion="success", status="completed", started="09:00", completed="09:01"):
    return {"name": name, "check_suite": {"id": 1}, "status": status, "conclusion": conclusion,
            "started_at": f"2026-03-02T{started}:00Z", "completed_at": f"2026-03-02T{completed}:00Z"}


def ci_reader(monkeypatch, found, attempts=None):
    """A client whose reads answer from `found` (check runs, workflow runs and statuses, by the path's last part) and
    `attempts` ((run id, attempt) -> that attempt). `logged` holds its log lines."""
    logged = []
    gh = GitHub("t", "acme/widget", log=logged.append)
    asked = []

    def paginate(path, **params):
        asked.append((path.rsplit("/", 1)[1], params))
        value = found[path.rsplit("/", 1)[1]]
        if isinstance(value, Exception):
            raise value
        return iter(value)

    def get(path, **params):
        asked.append((path, params))
        if "/attempts/" in path:
            run_id, attempt = path.split("/actions/runs/")[1].split("/attempts/")
            return (attempts or {})[int(run_id), int(attempt)]
        return {"statuses": found["statuses"]}

    monkeypatch.setattr(gh, "paginate", paginate)
    monkeypatch.setattr(gh, "get", get)
    return gh, asked, logged


def test_ci_held_for_a_maintainers_approval_is_its_own_state(monkeypatch):
    # A first-time contributor's fork PR (#162): `tests` waits for approval and made no check runs; the CLA check ran.
    found = {"check-runs": [check_run("cla"), check_run("Stratum")], "statuses": [], "runs": [workflow_run("tests", "action_required")]}
    gh, asked, _ = ci_reader(monkeypatch, found)
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.HELD
    # Stratum's own workflow runs on pull_request_target, which GitHub never holds: the event leaves it out.
    assert ("runs", {"head_sha": "a" * 40, "event": "pull_request"}) in asked
    found["check-runs"].append(check_run("lint", "failure"))
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.FAILED  # a check that ran and failed still counts first
    found["check-runs"].pop()
    found["check-runs"].append(check_run("lint", None, "in_progress"))
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.HELD  # held for approval beats still running
    found["check-runs"] = []
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.HELD  # nothing else ran at all: held, not "no CI"


def test_a_failed_check_needs_no_workflow_runs(monkeypatch):
    # A failure is known from the checks: the workflow runs aren't read, so a 5xx there can't lose it.
    found = {"check-runs": [check_run("lint", "failure")], "statuses": [], "runs": GitHubError(502, "Bad gateway")}
    gh, asked, _ = ci_reader(monkeypatch, found)
    assert gh.ci_state("a" * 40, ignore=set()) == CI.FAILED
    assert gh.ci_state_at("a" * 40, "2026-03-02T10:00:00Z", ignore=set()) == CI.FAILED
    assert not [path for path, _ in asked if path == "runs" or "/actions/" in path]


def test_a_held_workflow_named_like_stratums_check_is_still_held(monkeypatch):
    # The project's tests in a workflow called "Stratum", or beside the Stratum job: the check's name doesn't hide them.
    found = {"check-runs": [check_run("cla")], "statuses": [], "runs": [workflow_run("Stratum", "action_required")]}
    gh, _, _ = ci_reader(monkeypatch, found)
    assert gh.ci_state("a" * 40, ignore={"Stratum", "stratum"}) == CI.HELD
    assert gh.ci_state_at("a" * 40, "2026-03-02T10:00:00Z", ignore={"Stratum", "stratum"}) == CI.HELD


def test_a_workflow_that_ran_is_read_from_its_checks_as_before(monkeypatch):
    found = {"check-runs": [check_run("cla"), check_run("tests")], "statuses": [],
             "runs": [workflow_run("tests", "success"), workflow_run("CLA", "success")]}
    gh, _, _ = ci_reader(monkeypatch, found)
    assert gh.ci_state("a" * 40, ignore=set()) == CI.PASSED
    # Approved: GitHub runs the held run again in place, as its second attempt.
    found["runs"] = [workflow_run("tests", "success", "09:00", attempt=2, started="09:30")]
    assert gh.ci_state("a" * 40, ignore=set()) == CI.PASSED


def test_a_token_that_cant_list_workflow_runs_still_gets_the_checks_and_says_so(monkeypatch):
    # Without `actions: read` on a private repo, GitHub refuses the list; the checks still decide, as before.
    found = {"check-runs": [check_run("tests")], "statuses": [], "runs": GitHubError(403, "Resource not accessible by integration")}
    gh, _, logged = ci_reader(monkeypatch, found)
    assert gh.ci_state("a" * 40, ignore=set()) == CI.PASSED
    assert logged == ["Couldn't list the workflow runs (GitHub answered 403), so CI waiting for a maintainer's approval "
                      "can't be told from CI that passed. The workflow's token needs `actions: read`."]
    for refused in (GitHubError(403, "API rate limit exceeded", rate_limited=True), GitHubError(500, "boom")):
        found["runs"] = refused
        with pytest.raises(GitHubError):
            gh.ci_state("a" * 40, ignore=set())


def test_a_rate_limit_is_told_from_a_token_without_the_right(monkeypatch):
    import requests

    monkeypatch.setattr("stratum_open.github.api.time.sleep", lambda seconds: None)

    def answer(status, headers):
        response = requests.Response()
        response.status_code, response._content = status, b'{"message": "no"}'
        response.headers.update(headers)
        return response

    for headers, limited in (({"X-RateLimit-Remaining": "0"}, True), ({"Retry-After": "1"}, True), ({}, False)):
        session = requests.Session()
        monkeypatch.setattr(session, "request", lambda method, url, **kwargs: answer(403, headers))
        gh = GitHub("t", "acme/widget", session=session)
        for read in (lambda: gh.get("/repos/acme/widget"), lambda: list(gh.paginate("/repos/acme/widget/actions/runs"))):
            with pytest.raises(GitHubError) as error:
                read()
            assert (error.value.status, error.value.rate_limited) == (403, limited)


def test_ci_held_when_the_pr_closed_is_held_in_a_replay(monkeypatch):
    closed = "2026-03-02T10:00:00Z"
    found = {"check-runs": [check_run("cla")], "statuses": [], "runs": [workflow_run("tests", "action_required", "09:00")]}
    gh, _, _ = ci_reader(monkeypatch, found)
    assert gh.ci_state_at("a" * 40, closed, ignore={"Stratum"}) == CI.HELD  # never approved
    found["runs"] = [workflow_run("tests", "action_required", "11:00")]
    assert gh.ci_state_at("a" * 40, closed, ignore={"Stratum"}) == CI.PASSED  # a run made after the PR closed wasn't there then


def test_a_run_approved_after_the_pr_closed_was_held_when_it_closed(monkeypatch):
    # #162's run: approved hours later, it kept its id and created_at and became attempt 2, which passed.
    closed = "2026-03-02T10:00:00Z"
    tests = workflow_run("tests", "success", "09:00", run_id=44, attempt=2, started="12:00")
    found = {"check-runs": [check_run("cla"), check_run("tests", started="12:00", completed="12:05")], "statuses": [], "runs": [tests]}
    attempts = {(44, 1): {**workflow_run("tests", "action_required", "09:00", run_id=44)}}
    gh, asked, _ = ci_reader(monkeypatch, found, attempts)
    assert gh.ci_state_at("a" * 40, closed, ignore={"Stratum"}) == CI.HELD
    assert [path for path, _ in asked if "/attempts/" in path] == ["/repos/acme/widget/actions/runs/44/attempts/1"]
    assert gh.ci_state("a" * 40, ignore={"Stratum"}) == CI.PASSED  # today it has run
    assert gh.ci_state_at("a" * 40, "2026-03-02T13:00:00Z", ignore={"Stratum"}) == CI.PASSED  # approved before that close
    # Approved before the PR closed, then run again after it: the attempt that stood then is the approved one.
    found["runs"] = [{**tests, "run_attempt": 3, "run_started_at": "2026-03-02T11:00:00Z"}]
    attempts[44, 2] = {**tests, "conclusion": "success", "run_started_at": "2026-03-02T09:30:00Z"}
    found["check-runs"] = [check_run("cla"), check_run("tests", started="09:30", completed="09:35")]
    assert gh.ci_state_at("a" * 40, closed, ignore={"Stratum"}) == CI.PASSED
