"""H. The GitHub App: webhook verification and parsing, the job queue, the
database, retention, and startup checks."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from stratum_open.app import AppAuth, Server, from_env
from stratum_open.findings import Finding
from stratum_open.github.feedback import collect_votes
from stratum_open.storage import SQLiteStorage

from conftest import DIFF, SQL_LINE, head_of
from contract import assert_contract, run_stratum
from fake_model import Response, finding, json_reply

HEAD = head_of(7)
SECRET = b"s3cret"


class StubAuth:
    api = "https://api.github.invalid"

    def bot_login(self):
        return "stratum[bot]"


def make_server(tmp_path, monkeypatch, storage=None, **kwargs) -> Server:
    monkeypatch.setattr("threading.Thread.start", lambda self: None)  # no workers
    return Server(StubAuth(), SECRET.decode(), storage or SQLiteStorage(str(tmp_path / "s.db")), str(tmp_path / "repos"), env={}, log=kwargs.pop("log", lambda line: None), **kwargs)


def call(server, body: bytes, *, event="pull_request", secret=SECRET, path="/webhook", signature=None, length=None, delivery="d-1"):
    sig = signature if signature is not None else "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    environ = {"PATH_INFO": path, "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)) if length is None else length,
               "wsgi.input": io.BytesIO(body), "HTTP_X_HUB_SIGNATURE_256": sig, "HTTP_X_GITHUB_EVENT": event, "HTTP_X_GITHUB_DELIVERY": delivery}
    status: list[str] = []
    response = b"".join(server(environ, lambda s, headers: status.append(s)))
    return status[0], json.loads(response)


def pr_event(number=7, action="synchronize", **extra):
    return json.dumps({"action": action, "installation": {"id": 1}, "repository": {"full_name": "acme/widget", "default_branch": "main"},
                       "pull_request": {"number": number, "draft": False, "head": {"sha": HEAD}}, **extra}).encode()


@pytest.mark.parametrize("kwargs, expected", [
    ({"signature": ""}, "401 Unauthorized"), ({"signature": "sha256=deadbeef"}, "401 Unauthorized"), ({"secret": b"wrong"}, "401 Unauthorized"),
    ({"signature": "sha1=abc"}, "401 Unauthorized"), ({}, "202 Accepted"),
])
def test_webhook_signature(tmp_path, monkeypatch, kwargs, expected):
    server = make_server(tmp_path, monkeypatch)
    assert call(server, pr_event(), **kwargs)[0] == expected
    assert server._queue.qsize() == (1 if expected.startswith("202") else 0)


def test_a_server_with_no_secret_accepts_nothing(tmp_path, monkeypatch):
    server = make_server(tmp_path, monkeypatch)
    server.secret = b""
    assert call(server, pr_event(), secret=b"")[0] == "401 Unauthorized"


@pytest.mark.parametrize("body, event, expected", [
    (b"not json", "pull_request", "400 Bad Request"), (b"[1, 2]", "pull_request", "400 Bad Request"), (b'"text"', "pull_request", "400 Bad Request"),
    (b"null", "pull_request", "400 Bad Request"), (b"{}", "pull_request", "202 Accepted"), (b"{}", "star", "202 Accepted"),
    (json.dumps({"action": "opened", "installation": {"id": 1}, "repository": {"full_name": "acme/widget"}}).encode(), "pull_request", "202 Accepted"),
    (json.dumps({"action": "opened", "installation": {"id": 1}, "repository": {"full_name": "acme/widget"}, "pull_request": None}).encode(), "pull_request", "202 Accepted"),
    (json.dumps({"action": "opened", "installation": "1", "repository": "acme/widget", "pull_request": [7]}).encode(), "pull_request", "202 Accepted"),
    (json.dumps({"action": "created", "installation": {"id": 1}, "repository": {"full_name": "acme/widget"}, "issue": {"number": 7, "pull_request": {}},
                 "comment": {"id": 9, "body": None}}).encode(), "issue_comment", "202 Accepted"),
    (json.dumps({"action": "created", "installation": {"id": 1}, "repository": {"full_name": "acme/widget"}, "issue": {"number": 7, "pull_request": {}},
                 "comment": None}).encode(), "issue_comment", "202 Accepted"),
    (json.dumps({"action": "deleted", "installation": {"id": 1}, "repositories": [None, {"name": "x"}, {"full_name": "acme/gone"}]}).encode(), "installation", "202 Accepted"),
], ids=["not-json", "list", "string", "null", "empty-object", "unknown-event", "missing-pr", "null-pr", "wrong-types", "null-comment-body", "null-comment", "odd-uninstall"])
def test_malformed_payloads_never_give_a_500(tmp_path, monkeypatch, body, event, expected):
    logs: list[str] = []
    server = make_server(tmp_path, monkeypatch, log=logs.append)
    status, data = call(server, body, event=event)
    assert status == expected, (status, data)
    if status.startswith("202"):
        assert data == {"queued": False}


def test_bad_content_length_and_oversized_bodies(tmp_path, monkeypatch):
    server = make_server(tmp_path, monkeypatch)
    assert call(server, pr_event(), length="abc")[0] == "400 Bad Request"
    assert call(server, pr_event(), length="0")[0] == "413 Payload Too Large"
    assert call(server, pr_event(), length=str(30 * 1024 * 1024))[0] == "413 Payload Too Large"


def test_replayed_and_racing_deliveries_queue_one_job(tmp_path, monkeypatch):
    server = make_server(tmp_path, monkeypatch)
    for _ in range(3):
        call(server, pr_event(action="opened"), delivery="same")
    call(server, pr_event(action="synchronize"), delivery="later")
    assert server._queue.qsize() == 1 and server._pending[("acme/widget", 7, "review")][1]["action"] == "synchronize"
    comment = json.dumps({"action": "created", "installation": {"id": 1}, "repository": {"full_name": "acme/widget"}, "issue": {"number": 7, "pull_request": {}},
                          "comment": {"id": 9, "body": "/stratum triage"}}).encode()
    for n in range(50):  # each with its own delivery id, so it's the queue's per-comment key doing the collapsing
        call(server, comment, event="issue_comment", delivery=f"comment-{n}")
    assert server._queue.qsize() == 2, "fifty copies of one comment are one job"


class BrokenStorage(SQLiteStorage):
    def record_job(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")


def test_a_worker_survives_a_job_that_fails_and_a_database_that_fails_too(tmp_path):
    logs: list[str] = []
    storage = BrokenStorage(str(tmp_path / "s.db"))
    server = Server(StubAuth(), SECRET.decode(), storage, str(tmp_path / "repos"), env={}, workers=1, log=logs.append)
    runs: list[str] = []

    def failing_run(event, payload):
        runs.append(event)
        raise RuntimeError("the review blew up with token ghp_" + "S3cretS3cretS3cretS3cretS3cretS3cret")

    server.run = failing_run  # type: ignore[method-assign]
    for n in (1, 2, 3):
        call(server, pr_event(number=n), delivery=str(n))
    deadline = time.monotonic() + 10
    while len(runs) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(runs) == 3, "the worker thread died after the first failure"
    assert any("couldn't record the failed job either" in line for line in logs)
    assert "ghp_S3cret" not in "\n".join(logs) and "[redacted]" in "\n".join(logs)


def test_retention_purges_findings_after_180_days_and_jobs_after_14(tmp_path):
    storage = SQLiteStorage(str(tmp_path / "s.db"))
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    storage.record_review("acme/widget", 7, HEAD, 50, "s", [Finding("t", "d", path="x.py")])
    storage.record_job("acme/widget", 7, "review", "published")
    with storage._tx() as db:
        db.execute("UPDATE reviews SET created_at = ?", ((now - timedelta(days=181)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        db.execute("UPDATE jobs SET created_at = ?", ((now - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    storage.record_review("acme/widget", 8, HEAD, 50, "s", [Finding("t2", "d", path="y.py")])
    deleted = storage.purge(findings_days=180, jobs_days=14, now=now)
    assert deleted == {"reviews": 1, "findings": 1, "jobs": 1}
    assert storage.latest_findings("acme/widget", 8) and not storage.latest_findings("acme/widget", 7)


def test_the_daily_purge_reaches_every_installation_and_repo_past_one_that_fails(fake_github, tmp_path, monkeypatch):
    """purge_all pages through the installations and each one's repos, and an
    installation or repo that fails is logged and skipped, never the rest."""
    monkeypatch.setattr(Server, "_purge_loop", lambda self: None)  # the test calls purge_all itself
    fake_github.per_page_cap = 2  # GitHub pages 100 at a time; 2 keeps the test small
    fake_github.installations = [{"id": 1}, {"id": 2}, {"id": 3}]  # 3 is on the second page
    repos = ["acme/blocked"] + [f"acme/r{i}" for i in range(4)]  # three pages
    fake_github.installation_repos = [{"full_name": repo, "default_branch": "main"} for repo in repos]
    fake_github.fail("POST", r"/app/installations/1/access_tokens", 403, times=0)  # a suspended installation
    fake_github.fail("POST", r"/app/installations/2/access_tokens", 404, times=0)
    fake_github.fail("GET", r"^/repos/acme/blocked/", 451, body={"message": "Repository access blocked"}, times=0)
    storage = SQLiteStorage(str(tmp_path / "s.db"))
    for repo in repos:
        storage.record_review(repo, 1, HEAD, 10, "s", [Finding("t", "d", path="x.py")])
        storage.record_job(repo, 1, "review", "published")
    old = (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with storage._tx() as db:
        db.execute("UPDATE reviews SET created_at = ?", (old,))
        db.execute("UPDATE jobs SET created_at = ?", (old,))
    logs: list[str] = []
    server = Server(AppAuth("1", _private_key(), api=fake_github.url), SECRET.decode(), storage, str(tmp_path / "repos"), env={}, workers=0,
                    log=logs.append)

    server.purge_all()

    assert {repo: rows for repo in repos for table, rows in storage.export(repo).items() if rows} == {}
    assert any(line.startswith("purge: installation 1: ") and "403" in line for line in logs)
    assert any(line.startswith("purge: installation 2: ") and "404" in line for line in logs)
    # A repo whose policy can't be read is purged with the default retention.
    assert any(line.startswith("acme/blocked: ") and "451" in line and "180 days" in line for line in logs)


def _private_key() -> str:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:  # pragma: no cover
        pytest.skip("cryptography (the `app` extra) isn't installed")
    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()


def test_uninstall_deletes_the_repos_rows_at_once(tmp_path, monkeypatch):
    server = make_server(tmp_path, monkeypatch)
    server.storage.record_review("acme/widget", 7, HEAD, 50, "s", [Finding("t", "d", path="x.py")])
    server.storage.record_vote("acme/widget", "model:t", "f" * 12, 1, "mona", -1)
    server.storage.record_review("other/repo", 1, HEAD, 50, "s", [Finding("t", "d", path="x.py")])
    body = json.dumps({"action": "deleted", "installation": {"id": 1}, "repositories": [{"full_name": "acme/widget"}]}).encode()
    assert call(server, body, event="installation")[0] == "202 Accepted"
    assert server.storage.export("acme/widget") == {t: [] for t in ("reviews", "findings", "jobs", "votes", "mutes")}
    assert server.storage.latest_findings("other/repo", 1)


class GoneAuth(StubAuth):
    """After the uninstall: GitHub gives no token, and lists no installation."""

    def token(self, installation_id):
        raise RuntimeError("404 Client Error: Not Found for url: .../app/installations/1/access_tokens")

    def installations(self):
        return []


def start_worker(server, monkeypatch):
    monkeypatch.undo()  # make_server held the workers back
    threading.Thread(target=server._work, daemon=True).start()


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert condition()


def test_a_job_queued_when_the_app_is_uninstalled_leaves_no_row(tmp_path, monkeypatch):
    server = make_server(tmp_path, monkeypatch)
    server.auth = GoneAuth()
    uninstall = json.dumps({"action": "deleted", "installation": {"id": 1}, "repositories": [{"full_name": "acme/widget"}]}).encode()
    other = json.dumps({"action": "opened", "installation": {"id": 2}, "repository": {"full_name": "other/repo"}, "pull_request": {"number": 1}}).encode()
    assert call(server, pr_event(), delivery="1")[1] == {"queued": True}
    call(server, uninstall, event="installation", delivery="2")
    assert call(server, other, delivery="3")[1] == {"queued": True}
    start_worker(server, monkeypatch)
    wait_for(lambda: server.storage.export("other/repo")["jobs"])  # one worker: acme/widget's job came first
    assert server.storage.export("acme/widget") == {t: [] for t in ("reviews", "findings", "jobs", "votes", "mutes")}


def test_a_job_running_when_the_app_is_uninstalled_has_its_rows_deleted_when_it_ends(tmp_path, monkeypatch):
    server = make_server(tmp_path, monkeypatch)
    uninstalled, started = threading.Event(), threading.Event()

    def run(event, payload):
        started.set()
        uninstalled.wait(10)
        server.storage.record_review("acme/widget", 7, HEAD, 50, "s", [Finding("t", "d", path="x.py")])
        os.makedirs(server._clone_dir("acme/widget"), exist_ok=True)
        raise RuntimeError("the installation is gone")

    server.run = run  # type: ignore[method-assign]
    call(server, pr_event(), delivery="1")
    start_worker(server, monkeypatch)
    assert started.wait(10)
    call(server, json.dumps({"action": "deleted", "installation": {"id": 1}, "repositories": [{"full_name": "acme/widget"}]}).encode(),
         event="installation", delivery="2")
    uninstalled.set()
    wait_for(lambda: not server._busy)
    assert server.storage.export("acme/widget") == {t: [] for t in ("reviews", "findings", "jobs", "votes", "mutes")}
    assert not os.path.exists(server._clone_dir("acme/widget"))
    # Installed again: its next job is kept.
    server.run = lambda event, payload: server.storage.record_job("acme/widget", 7, "review", "published")  # type: ignore[method-assign]
    call(server, pr_event(), delivery="3")
    wait_for(lambda: server.storage.export("acme/widget")["jobs"])


def test_the_daily_purge_deletes_old_job_rows_of_repos_no_longer_installed(tmp_path, monkeypatch):
    server = make_server(tmp_path, monkeypatch)
    server.auth = GoneAuth()
    server.storage.record_review("gone/repo", 1, HEAD, 50, "s", [Finding("t", "d", path="x.py")])
    server.storage.record_job("gone/repo", 1, "review", "error", "HTTPError: 404")
    server.storage.record_job("gone/repo", 2, "review", "error", "HTTPError: 404")
    with server.storage._tx() as db:
        db.execute("UPDATE jobs SET created_at = ? WHERE pr = 1", ((datetime.now(timezone.utc) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    server.purge_all()
    exported = server.storage.export("gone/repo")
    assert [job["pr"] for job in exported["jobs"]] == [2]
    assert exported["reviews"]  # their retention is the repo's own policy, which purge_all can't read here


def test_stratum_data_shows_and_deletes(tmp_path):
    db = str(tmp_path / "s.db")
    storage = SQLiteStorage(db)
    storage.record_review("acme/widget", 7, HEAD, 50, "summary", [Finding("t", "d", path="x.py")])
    storage.record_vote("acme/widget", "model:t", "f" * 12, 1, "mona", -1)
    storage.close()
    shown = run_stratum(["data", "export", "--db", db, "--repo", "acme/widget"], env={})
    assert shown.returncode == 0 and json.loads(shown.stdout)["reviews"][0]["summary"] == "summary"
    deleted = run_stratum(["data", "delete", "--db", db, "--repo", "acme/widget", "--all"], env={})
    assert deleted.returncode == 0 and "Deleted 3 row(s)" in deleted.stdout
    missing = run_stratum(["data", "export", "--db", str(tmp_path / "nope.db"), "--repo", "acme/widget"], env={})
    assert missing.returncode == 1 and "Only the GitHub App stores data" in missing.stderr and "Traceback" not in missing.stderr


def test_votes_from_users_without_write_access_are_ignored(fake_github, make_context, fake_model, tmp_path):
    fake_github.add_pull(7, diff=DIFF)
    fake_github.permissions.update({"mona": "write", "erin": "read"})
    fake_model.queue(Response.ok(json_reply([finding("app/users.py", SQL_LINE, line=5)])))
    storage = SQLiteStorage(str(tmp_path / "s.db"))
    ctx = make_context(storage=storage, feedback=True)
    from stratum_open.runner import review_pull
    review_pull(ctx, 7)
    comment = next(c for c in fake_github.inline_comments(7) if "Unknown id" in c["body"])
    comment["reactions"] = {"+1": 1, "-1": 2}
    fake_github.reactions[comment["id"]] = [{"content": "-1", "user": {"login": "mona"}}, {"content": "-1", "user": {"login": "erin"}},
                                           {"content": "+1", "user": {"login": "ghost"}}, {"content": "heart", "user": {"login": "mona"}}, {"content": "-1", "user": None}]
    assert collect_votes(ctx.gh, 7, fake_github.bot, storage) == 1
    assert storage.rule_votes("acme/widget") == {"model:unknown-id": (0, 1)}


def test_a_vote_taken_back_stops_counting(fake_github, make_context, fake_model, tmp_path):
    fake_github.add_pull(7, diff=DIFF)
    fake_github.permissions.update({"mona": "write", "hubot": "write"})
    fake_model.queue(Response.ok(json_reply([finding("app/users.py", SQL_LINE, line=5)])))
    storage = SQLiteStorage(str(tmp_path / "s.db"))
    ctx = make_context(storage=storage, feedback=True)
    from stratum_open.runner import review_pull
    review_pull(ctx, 7)
    comment = next(c for c in fake_github.inline_comments(7) if "Unknown id" in c["body"])

    def react(**by):
        comment["reactions"] = {"+1": sum(v == "+1" for v in by.values()), "-1": sum(v == "-1" for v in by.values())}
        fake_github.reactions[comment["id"]] = [{"content": v, "user": {"login": login}} for login, v in by.items()]

    react(mona="-1", hubot="-1")
    assert collect_votes(ctx.gh, 7, fake_github.bot, storage) == 2
    assert storage.rule_votes("acme/widget") == {"model:unknown-id": (0, 2)}
    react(mona="+1")  # hubot took theirs back, mona changed hers
    assert collect_votes(ctx.gh, 7, fake_github.bot, storage) == 1
    assert storage.rule_votes("acme/widget") == {"model:unknown-id": (1, 0)}

    # GitHub won't say whether mona has write access: her stored vote stays rather than being dropped.
    react(mona="+1", hubot="-1")
    fake_github.fail("GET", r".*/collaborators/mona/permission", 403, body={"message": "Resource not accessible by integration"}, times=0)
    assert collect_votes(ctx.gh, 7, fake_github.bot, storage) == 1
    assert storage.rule_votes("acme/widget") == {"model:unknown-id": (1, 1)}

    react()  # every vote taken back: the totals say so, and no reactions are read
    reads = len(fake_github.requests)
    assert collect_votes(ctx.gh, 7, fake_github.bot, storage) == 0
    assert storage.rule_votes("acme/widget") == {}
    assert not any("/reactions" in r["path"] for r in fake_github.requests[reads:])


@pytest.mark.parametrize("env, says", [
    ({}, "Missing settings: STRATUM_APP_ID, STRATUM_WEBHOOK_SECRET, STRATUM_APP_PRIVATE_KEY"),
    ({"STRATUM_APP_ID": "1", "STRATUM_WEBHOOK_SECRET": "s", "STRATUM_APP_PRIVATE_KEY_FILE": "/nope/key.pem"}, "Couldn't read STRATUM_APP_PRIVATE_KEY_FILE"),
    ({"STRATUM_APP_ID": "1", "STRATUM_WEBHOOK_SECRET": "s", "STRATUM_APP_PRIVATE_KEY": "not a key"}, "doesn't look like a PEM private key"),
    ({"STRATUM_APP_ID": "1", "STRATUM_WEBHOOK_SECRET": "s", "STRATUM_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----", "STRATUM_WORKERS": "two"}, "STRATUM_WORKERS must be a whole number"),
])
def test_bad_settings_fail_fast_with_a_readable_line(env, says, tmp_path, monkeypatch):
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    with pytest.raises(SystemExit) as stop:
        from_env({**env, "STRATUM_DB": str(tmp_path / "s.db")})
    assert says in str(stop.value)


def test_unusable_database_fails_fast(tmp_path, monkeypatch):
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    base = {"STRATUM_APP_ID": "1", "STRATUM_WEBHOOK_SECRET": "s", "STRATUM_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----"}
    newer = str(tmp_path / "newer.db")
    conn = sqlite3.connect(newer)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SystemExit) as stop:
        from_env({**base, "STRATUM_DB": newer})
    assert "schema version 99" in str(stop.value)
    with pytest.raises(SystemExit) as stop:
        from_env({**base, "STRATUM_DB": str(tmp_path / "no" / "such" / "dir" / "s.db")})
    assert "Couldn't open the database" in str(stop.value)


def test_the_whole_app_flow_over_http_against_the_fakes(fake_github, fake_model, tmp_path, monkeypatch):
    """A signed webhook reaches a real worker thread, which reviews the PR on
    the fake GitHub with the fake model and records the job."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:  # pragma: no cover
        pytest.skip("cryptography (the `app` extra) isn't installed")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode()
    fake_github.add_pull(7, diff=DIFF)
    fake_model.queue(Response.ok(json_reply([finding("app/users.py", SQL_LINE, line=5)])))
    logs: list[str] = []
    monkeypatch.setattr(Server, "checkout", lambda self, repo, token, branch, window_days: None)  # no clone of github.com in a test
    server = Server(AppAuth("1", key, api=fake_github.url), SECRET.decode(), SQLiteStorage(str(tmp_path / "s.db")), str(tmp_path / "repos"),
                    env={"STRATUM_MODEL_ENDPOINT": fake_model.url}, workers=1, log=logs.append)
    status, data = call(server, pr_event(action="opened"))
    assert (status, data) == ("202 Accepted", {"queued": True})
    deadline = time.monotonic() + 30
    # The job row is written after review_pull returns, which is after the check run; waiting for it covers every assert below.
    while not any(job["event"] == "review" for job in server.storage.export("acme/widget")["jobs"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert_contract(fake_github, number=7, head=HEAD, logs=logs)
    assert {f["title"] for f in server.storage.latest_findings("acme/widget", 7)} >= {"Unknown id"}
    assert server.storage.export("acme/widget")["jobs"][-1]["outcome"] == "published"
    assert fake_github.requests_to("POST", r"/app/installations/1/access_tokens")


def test_a_default_branch_with_no_commit_in_the_window_is_still_checked_out(tmp_path, monkeypatch):
    """`--shallow-since` selects nothing on a quiet branch and git fails; the
    clone (or the fetch into it) then takes the newest commit alone."""
    origin = tmp_path / "origin"
    (tmp_path / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Server.checkout clones from github.com: point that URL at the local repo.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin.resolve().as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/acme/widget.git")

    def git(*args, when=None):
        env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
        if when:
            env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True, env=env).stdout.strip()

    def commit(name, when):
        (origin / name).write_text("print('hi')\n")
        git("-C", str(origin), "add", name)
        git("-C", str(origin), "commit", "--quiet", "-m", f"Add {name}", when=when)

    git("init", "--quiet", "--initial-branch=main", str(origin))
    commit("first.py", "2025-01-01T12:00:00+00:00")
    commit("app.py", "2025-01-02T12:00:00+00:00")
    logs: list[str] = []
    monkeypatch.setattr(Server, "_purge_loop", lambda self: None)
    server = Server(StubAuth(), SECRET.decode(), SQLiteStorage(str(tmp_path / "s.db")), str(tmp_path / "repos"), env={}, workers=0, log=logs.append)

    path = server.checkout("acme/widget", "token", "main", 30)
    assert path and sorted(os.listdir(path)) == [".git", "app.py", "first.py"], logs
    assert git("-C", path, "rev-list", "--count", "HEAD") == "1"  # the newest commit only: the clone stays small
    commit("later.py", "2025-02-01T12:00:00+00:00")  # still outside the window: the fetch falls back too
    assert server.checkout("acme/widget", "token", "main", 30) == path, logs
    assert os.path.exists(os.path.join(path, "later.py"))
    assert git("-C", path, "rev-list", "--count", "HEAD") == "1"
    commit("new.py", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"))  # inside the window
    assert server.checkout("acme/widget", "token", "main", 30) == path, logs
    assert git("-C", path, "log", "-1", "--format=%s") == "Add new.py"
    assert not logs


def test_the_app_keeps_the_triage_queue_issue(fake_github, tmp_path, monkeypatch):
    """A PR closing rebuilds the queue as the App's bot, once the wait is over: the issue is opened, then edited in place."""
    from stratum_open.triage_queue import TITLE

    fake_github.add_pull(7, diff=DIFF, title="Add a thing")
    fake_github.add_pull(8, diff=DIFF, title="Second")
    logs: list[str] = []
    with monkeypatch.context() as patch:  # no workers or loops: the test runs each rebuild that's due
        patch.setattr("threading.Thread.start", lambda self: None)
        server = Server(AppAuth("1", _private_key(), api=fake_github.url), SECRET.decode(), SQLiteStorage(str(tmp_path / "s.db")),
                        str(tmp_path / "repos"), env={}, workers=0, log=logs.append)
    now = [0.0]
    server.clock = lambda: now[0]

    fake_github.pulls[8]["state"] = "closed"
    assert call(server, pr_event(number=8, action="closed"))[1] == {"queued": True}
    assert server.rebuild_due() == [] and fake_github.issues == []  # it waits a minute first
    now[0] = 60
    assert server.rebuild_due() == ["acme/widget"]
    [queue] = fake_github.issues
    assert queue["title"] == TITLE and queue["user"]["login"] == "stratum[bot]"
    assert "#7 Add a thing" in queue["body"] and "#8" not in queue["body"]

    fake_github.pulls[7]["state"] = "closed"
    call(server, pr_event(number=7, action="closed"), delivery="d-2")
    now[0] = 120
    assert server.rebuild_due() == ["acme/widget"]
    assert [i["number"] for i in fake_github.issues] == [queue["number"]] and "#7" not in fake_github.issues[0]["body"]
    assert fake_github.requests_to("PATCH", rf"/issues/{queue['number']}$")

    fake_github.set_policy("triage:\n  queue: false\n")  # turned off since: nothing is posted
    fake_github.add_pull(9, diff=DIFF, title="Third")
    call(server, pr_event(number=9, action="converted_to_draft"), delivery="d-3")
    now[0] = 180
    edits = len(fake_github.requests_to("PATCH", r"/issues/\d+$"))
    assert server.rebuild_due() == ["acme/widget"]
    assert len(fake_github.requests_to("PATCH", r"/issues/\d+$")) == edits and "#9" not in fake_github.issues[0]["body"]
    assert logs == []


def test_the_app_labels_a_coding_agents_pr(fake_github, fake_model, tmp_path, monkeypatch):
    class Auth:
        api = fake_github.url

        def token(self, installation_id):
            return fake_github.token

        def bot_login(self):
            return fake_github.bot

    with monkeypatch.context() as patch:  # no workers or loops: the test runs the job itself
        patch.setattr("threading.Thread.start", lambda self: None)
        server = Server(Auth(), SECRET.decode(), SQLiteStorage(str(tmp_path / "s.db")), str(tmp_path / "repos"),
                        env={"STRATUM_MODEL_ENDPOINT": fake_model.url}, workers=0, log=lambda line: None)
    monkeypatch.setattr(server, "checkout", lambda *args: None)  # no clone of github.com
    fake_github.add_pull(7, diff=DIFF, author="Copilot")
    fake_github.pr_commits[7] = [{"sha": HEAD, "commit": {"message": "Add the lookup", "author": {
        "name": "copilot-swe-agent[bot]", "email": "198982749+Copilot@users.noreply.github.com"}}}]
    fake_github.set_policy("triage:\n  enabled: true\n  agent_prs:\n    label: true\n")
    fake_model.queue(Response.ok(json_reply([])))
    server.run("pull_request", json.loads(pr_event(action="opened")))
    assert "triage: agent-written" in fake_github.labels_on(7)
    assert "**Likely written by a coding agent:** the author is `Copilot` (GitHub Copilot)" in fake_github.summary_comments(7)[0]["body"]
    assert server.storage.export("acme/widget")["jobs"][-1]["outcome"] == "published"
