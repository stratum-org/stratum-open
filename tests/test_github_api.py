from stratum_open.github.api import GitHub, _login


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
    found = {"check-runs": [], "statuses": []}

    def paginate(path, **params):
        asked.append((path.rsplit("/", 1)[1], params))
        return iter(found[path.rsplit("/", 1)[1]])

    def run(name, started, completed, conclusion, suite=1):
        return {"name": name, "check_suite": {"id": suite}, "status": "completed", "conclusion": conclusion,
                "started_at": f"2026-03-02T{started}:00Z", "completed_at": f"2026-03-02T{completed}:00Z"}

    def ci():
        return gh.ci_state_at("a" * 40, closed, ignore={"Stratum"})

    monkeypatch.setattr(gh, "paginate", paginate)
    assert ci() is None  # no CI by then
    found["check-runs"] = [run("test", "09:00", "09:05", "failure"), run("test", "09:10", "09:15", "success")]
    assert ci() is True  # run again before the PR closed: the second run is the one that stood
    assert asked[-2] == ("check-runs", {"filter": "all"})  # GitHub's default hides the earlier runs
    found["check-runs"].append(run("test", "11:00", "11:05", "failure"))
    assert ci() is True  # run again after it closed: not what was known then
    found["check-runs"].append(run("test", "09:00", "09:05", "failure", suite=2))
    assert ci() is False  # another workflow's job of the same name is its own check
    found["check-runs"] = [run("test", "09:00", "09:05", "success"), run("docs", "09:50", "10:20", "failure")]
    assert ci() is None  # still running when the PR closed, however it ended
    found["check-runs"] = [run("test", "09:00", "09:05", "success"), run("Stratum", "09:00", "09:05", "failure")]
    assert ci() is True  # Stratum's own check isn't CI
    found["statuses"] = [  # newest first, as GitHub lists them
        {"context": "docs", "state": "error", "created_at": "2026-03-02T10:30:00Z"},
        {"context": "docs", "state": "success", "created_at": "2026-03-02T09:30:00Z"},
        {"context": "docs", "state": "pending", "created_at": "2026-03-02T09:00:00Z"},
    ]
    assert ci() is True  # each context's last status by then
    found["statuses"][1]["state"] = "failure"
    assert ci() is False


def test_check_annotations_are_sent_in_batches_of_fifty(monkeypatch):
    gh = GitHub("t", "acme/widget")
    requests = []

    def send(method, path, *, json):
        requests.append((method, json["output"]["annotations"]))
        return {"id": 42}

    monkeypatch.setattr(gh, "request", send)
    annotations = [{"path": f"file_{n}.py"} for n in range(120)]
    gh.check_run("a" * 40, "review", "success", "Review", "Summary", "Details", annotations)
    assert [(method, len(batch)) for method, batch in requests] == [("POST", 50), ("PATCH", 50), ("PATCH", 20)]
    assert [annotation for _, batch in requests for annotation in batch] == annotations


def test_review_threads_reads_every_cursor(monkeypatch):
    gh = GitHub("t", "acme/widget")
    cursors = []

    def graphql(query, **variables):
        cursor = variables["cursor"]
        cursors.append(cursor)
        start, end = (0, 100) if cursor is None else (100, 150)
        nodes = [{"id": f"T{n}", "comments": {"nodes": [{"databaseId": n, "body": "example"}]}}
                 for n in range(start, end)]
        return {"repository": {"pullRequest": {"reviewThreads": {
            "nodes": nodes, "pageInfo": {"hasNextPage": end == 100, "endCursor": "next" if end == 100 else None},
        }}}}

    monkeypatch.setattr(gh, "graphql", graphql)
    assert [thread["id"] for thread in gh.review_threads(7)] == [f"T{n}" for n in range(150)]
    assert cursors == [None, "next"]


def test_comment_is_not_retried_after_a_server_500(monkeypatch):
    import pytest
    import requests
    from stratum_open.github.api import GitHubError

    gh = GitHub("t", "acme/widget")
    methods = []

    def send(method, url, **kwargs):
        methods.append(method)
        response = requests.Response()
        response.status_code = 500
        response._content = b'{"message": "server error"}'
        return response

    monkeypatch.setattr(gh.session, "request", send)
    monkeypatch.setattr("stratum_open.github.api.time.sleep", lambda delay: None)
    with pytest.raises(GitHubError) as error:
        gh.comment(7, "hello")
    assert error.value.status == 500
    assert methods == ["POST"]
