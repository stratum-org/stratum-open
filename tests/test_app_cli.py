import hashlib
import hmac
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest
import requests
import yaml

from stratum_open import cli
from stratum_open.app import Server
from stratum_open.findings import Finding
from stratum_open.storage import SQLiteStorage


class Auth:
    api = "https://api.github.invalid"

    def bot_login(self):
        return "stratum-test[bot]"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr("threading.Thread.start", lambda self: None)  # no workers in tests
    storage = SQLiteStorage(str(tmp_path / "s.db"))
    return Server(Auth(), "s3cret", storage, str(tmp_path / "repos"), env={}, log=lambda line: None)


def call(server, body: bytes, *, event="pull_request", secret=b"s3cret", path="/webhook"):
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    environ = {
        "PATH_INFO": path, "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
        "HTTP_X_HUB_SIGNATURE_256": signature, "HTTP_X_GITHUB_EVENT": event,
    }
    status = []
    response = b"".join(server(environ, lambda s, headers: status.append(s)))
    return status[0], json.loads(response)


def pr_event(number=7, action="synchronize", draft=False):
    return json.dumps({
        "action": action, "installation": {"id": 1}, "repository": {"full_name": "acme/widget", "default_branch": "main"},
        "pull_request": {"number": number, "draft": draft},
    }).encode()


def test_webhook_signature_is_required(server):
    assert call(server, pr_event(), secret=b"wrong")[0] == "401 Unauthorized"
    assert call(server, pr_event())[0] == "202 Accepted"
    assert call(server, b"", path="/healthz")[0] == "200 OK"


def test_a_newer_push_replaces_the_queued_review(server):
    call(server, pr_event(action="opened"))
    call(server, pr_event(action="synchronize"))
    assert server._queue.qsize() == 1
    assert server._pending[("acme/widget", 7, "review")][1]["action"] == "synchronize"
    # Drafts are queued: whether to skip them is the repo's own policy (review.skip_drafts),
    # which the job reads; see test_the_job_skips_what_the_repos_policy_says.
    assert call(server, pr_event(number=8, draft=True))[1] == {"queued": True}
    assert call(server, pr_event(number=9, action="labeled"))[1] == {"queued": False}


@pytest.mark.parametrize("body, queued", [
    ("/stratum review", True),
    ("thanks!\r\n/Stratum ignore code/sql\r\n", True),
    ("@stratum review", False),
    ("looks good", False),
    ("Fixes https://github.com/stratum-org/stratum-open/issues/53", False),
    ("/stratum-org/stratum-open has the docs", False),
    ("From the model. Reply `/stratum ignore` to hide it on this PR.", False),  # Stratum's own footer
    ("email me at hi@stratum.dev", False),
])
def test_only_comments_with_a_command_are_queued(server, body, queued):
    comment = json.dumps({"action": "created", "installation": {"id": 1}, "repository": {"full_name": "acme/widget"},
                          "issue": {"number": 7, "pull_request": {}}, "comment": {"id": 9, "body": body}}).encode()
    assert call(server, comment, event="issue_comment")[1] == {"queued": queued}


def test_a_bots_comment_is_never_queued(server):
    # handle() drops it too, but only after a worker has cloned the repo for it.
    comment = json.dumps({"action": "created", "installation": {"id": 1}, "repository": {"full_name": "acme/widget"},
                          "issue": {"number": 7, "pull_request": {}},
                          "comment": {"id": 9, "body": "/stratum review", "user": {"login": "stratum-test[bot]", "type": "Bot"}}}).encode()
    assert call(server, comment, event="issue_comment")[1] == {"queued": False}


def test_a_command_on_an_issue_that_isnt_a_pr_is_never_queued(server):
    # handle() drops it too, but only after a worker has cloned the repo for it.
    def comment(issue):
        return json.dumps({"action": "created", "installation": {"id": 1}, "repository": {"full_name": "acme/widget"},
                           "issue": issue, "comment": {"id": 9, "body": "/stratum review"}}).encode()
    assert call(server, comment({"number": 7}), event="issue_comment")[1] == {"queued": False}
    assert call(server, comment({"number": 7, "pull_request": {"url": "x"}}), event="issue_comment")[1] == {"queued": True}
    inline = json.dumps({"action": "created", "installation": {"id": 1}, "repository": {"full_name": "acme/widget"},
                         "pull_request": {"number": 8}, "comment": {"id": 10, "body": "/stratum ignore"}}).encode()
    assert call(server, inline, event="pull_request_review_comment")[1] == {"queued": True}


def test_uninstalling_deletes_the_repos_rows_and_clone(server, tmp_path):
    server.storage.record_review("acme/widget", 7, "a" * 40, 50, "s", [Finding("t", "d", path="x.py")])
    clone = server._clone_dir("acme/widget")
    os.makedirs(clone)
    body = json.dumps({"action": "deleted", "installation": {"id": 1}, "repositories": [{"full_name": "acme/widget"}]}).encode()
    call(server, body, event="installation")
    assert server.storage.latest_findings("acme/widget", 7) == []
    assert not os.path.exists(clone)


# ── CLI ──────────────────────────────────────────────────────────────────


def test_the_command_is_stratum_open_not_hosted_stratums_stratum():
    """Hosted Stratum's client installs `stratum`; this one must not take that name."""
    scripts = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts == {"stratum-open": "stratum_open.cli:main"}


def test_policy_check(tmp_path, capsys):
    good = tmp_path / "good.yml"
    good.write_text("review:\n  max_inline_comments: 4\n")
    bad = tmp_path / "bad.yml"
    bad.write_text("review:\n  model: cloud\n")
    assert cli.main(["policy", "check", str(good)]) == 0
    assert cli.main(["policy", "check", str(bad)]) == 1
    assert "review.model must be one of: local, hosted." in capsys.readouterr().out
    repeated = tmp_path / "repeated.yml"
    repeated.write_text("review:\n  max_chunks: 5\nreview:\n  rules: [Use parameters.]\n")
    assert cli.main(["policy", "check", str(repeated)]) == 0
    assert "warning: review appears twice (lines 1 and 3); only the last one is read." in capsys.readouterr().out


def test_review_local_runs_the_checks(tmp_path, capsys, monkeypatch):
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text('x = 1\nAPI_KEY = "sk-live-0123456789abcdef0123"\n')
    git("commit", "-q", "-am", "Add key")
    # The user's own diff settings don't change what's reviewed.
    git("config", "diff.external", "echo")
    git("config", "diff.noprefix", "true")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    code = cli.main(["review", "--local", "main", "--no-model", "--json", str(tmp_path / "out.json")])
    out = capsys.readouterr().out
    assert "app.py:2" in out and "code/secret" in out
    assert code == 1  # a critical finding
    assert json.loads((tmp_path / "out.json").read_text())[0]["rule"] == "code/secret"



def test_review_local_skips_a_moved_binary_file(tmp_path, capsys, monkeypatch):
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    # A binary file a test reads, with text in it that would look like a secret if it were read as code.
    (repo / "tests" / "blob.dat").write_bytes(b"\x00\x01\x02" + b'API_KEY = "sk-live-0123456789abcdef0123"\n')
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    git("mv", "tests/blob.dat", "blob.dat")
    git("commit", "-q", "-m", "Move the blob")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    code = cli.main(["review", "--local", "main", "--no-model"])
    out = capsys.readouterr().out
    assert "code/secret" not in out and "checks/moved-unread" not in out
    assert code == 0


def test_review_local_reads_the_code_before_forty_edited_tests(tmp_path, capsys, monkeypatch):
    # #81: the local review read the first 40 files in diff order, and `tests/` sorts before `web/`.
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "web").mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    for i in range(40):
        (repo / "tests" / f"test_t{i:02}.py").write_text("".join(f"t{j} = {j}\n" for j in range(5)))
    (repo / "web" / "load.py").write_text("import pickle\n\ndef load(data):\n    return pickle.loads(data)\n")
    git("add", ".")
    git("commit", "-q", "-m", "Add tests and a loader")
    sent = []
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: True)
    monkeypatch.setattr("stratum_open.checks.bandit.run_bandit", lambda sources: sent.append(set(sources)) or [])
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    cli.main(["review", "--local", "main", "--no-model"])
    capsys.readouterr()
    assert sent == [{"web/load.py"}]


def test_review_local_reads_every_moved_file_it_has_room_for(tmp_path, capsys, monkeypatch):
    # #120's review: the GitHub limit of 20 moved files is about API calls, which `git show` doesn't make.
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "app").mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    for i in range(30):
        (repo / "tests" / f"h{i:02}.js").write_text("function f(req) { eval(req.query.x); }\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    for i in range(30):
        git("mv", f"tests/h{i:02}.js", f"app/h{i:02}.js")
    git("commit", "-q", "-m", "Move the helpers")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    cli.main(["review", "--local", "main", "--no-model", "--json", str(tmp_path / "out.json")])
    capsys.readouterr()
    findings = json.loads((tmp_path / "out.json").read_text())
    assert sorted(f["path"] for f in findings if f["rule"] == "code/eval") == [f"app/h{i:02}.js" for i in range(30)]
    assert "checks/moved-unread" not in {f["rule"] for f in findings}


@pytest.fixture
def committed(tmp_path, monkeypatch):
    """A repo with one commit, no git config but its own, and the CLI run from it."""
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    return repo, git


def test_review_staged_reads_only_what_is_staged(committed, capsys, monkeypatch):
    repo, git = committed
    (repo / "sub").mkdir()
    (repo / "sub" / "keys.py").write_text('API_KEY = "sk-live-0123456789abcdef0123"\n')
    staged = "import pickle\n\n\ndef load(data):\n    return pickle.loads(data)\n"
    (repo / "load.py").write_text(staged)
    git("add", ".")
    # Not staged: a secret in a committed file, one in an edit to a staged file, and one in a file git doesn't know.
    (repo / "app.py").write_text('x = 1\nTOKEN = "sk-live-abcdef0123456789abcd"\n')
    (repo / "load.py").write_text(staged + 'SECRET = "sk-live-9876543210fedcba9876"\n')
    (repo / "notes.py").write_text('KEY = "sk-live-0123456789abcdef9999"\n')
    # #62's settings, and the one only `--cached` reads (c/ and i/ in place of a/ and b/), change nothing.
    for name, value in (("diff.external", "echo"), ("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"), ("diff.relative", "true")):
        git("config", name, value)
    sent = []
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: True)
    monkeypatch.setattr("stratum_open.checks.bandit.run_bandit", lambda sources: sent.append(dict(sources)) or [])
    monkeypatch.chdir(repo / "sub")
    assert cli.main(["review", "--staged", "--no-model"]) == 1  # critical: the default --fail-on
    assert capsys.readouterr().out == "sub/keys.py:1: critical code/secret Possible secret in the code\n"
    assert sent[0]["load.py"] == staged  # the index's copy, not the working tree's


@pytest.mark.parametrize("fail_on, code", [(None, 0), ("low", 1), ("medium", 1), ("high", 1), ("critical", 0)])
def test_fail_on_sets_the_exit_code_by_severity(committed, capsys, monkeypatch, fail_on, code):
    repo, git = committed
    (repo / "run.py").write_text("def run(text):\n    return eval(text)\n")  # one high finding
    git("add", ".")
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)  # it would add a medium one
    assert cli.main(["review", "--staged", "--no-model", *(["--fail-on", fail_on] if fail_on else [])]) == code
    assert capsys.readouterr().out == "run.py:2: high code/eval `eval()` runs a string as code\n"


def test_review_staged_says_once_when_bandit_and_ruff_didnt_run(committed, capsys, monkeypatch):
    repo, git = committed
    (repo / ".stratum").mkdir()
    (repo / ".stratum" / "policy.yml").write_text("checks:\n  ruff: true\n")
    git("add", ".")
    git("commit", "-q", "-m", "Turn Ruff on")  # the policy is read from HEAD
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    monkeypatch.setattr("stratum_open.checks.ruff.available", lambda: False)
    (repo / "README.md").write_text("Docs.\n")
    git("add", ".")
    assert cli.main(["review", "--staged", "--no-model", "--fail-on", "low"]) == 0
    assert "didn't run" not in capsys.readouterr().err  # no Python file staged: neither would have read anything
    (repo / "app.py").write_text("x = 2\n")
    git("add", ".")
    assert cli.main(["review", "--staged", "--no-model", "--fail-on", "low"]) == 0  # the other checks ran, and that's no failure
    err = capsys.readouterr().err
    assert err.count("didn't run") == 1
    assert ("Bandit and Ruff didn't run on the staged Python files: not installed here. "
            "In the pre-commit hook, add `bandit` and `ruff` to its additional_dependencies.") in err


def test_review_staged_on_the_first_commit(tmp_path, capsys, monkeypatch):
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
    (tmp_path / "app.py").write_text('API_KEY = "sk-live-0123456789abcdef0123"\n')
    subprocess.run(["git", "add", "."], check=True)
    assert cli.main(["review", "--staged", "--no-model"]) == 1  # no HEAD: no policy, no commit message, no baseline
    assert "app.py:1: critical code/secret" in capsys.readouterr().out


def test_the_pre_commit_hook_runs_the_staged_checks(committed, capsys, monkeypatch):
    [hook] = yaml.safe_load((Path(__file__).parents[1] / ".pre-commit-hooks.yaml").read_text(encoding="utf-8"))
    assert (hook["id"], hook["language"], hook["pass_filenames"]) == ("stratum-open-checks", "python", False)
    command, *args = hook["entry"].split()
    assert command == "stratum-open"  # the console script, see test_the_command_is_stratum_open_not_hosted_stratums_stratum
    repo, git = committed
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    (repo / "clean.py").write_text("x = 3\n")
    git("add", ".")
    assert cli.main(args) == 0
    (repo / "run.py").write_text("def run(text):\n    return eval(text)\n")
    git("add", ".")
    assert cli.main(args) == 1  # high
    assert "run.py:2: high code/eval" in capsys.readouterr().out


@pytest.mark.parametrize("args", [["--local"], ["--local", "main"], ["acme/widget#12"]])
def test_review_staged_takes_neither_a_base_nor_a_pr(args):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["review", "--staged", *args])
    assert str(exit_.value) == "--staged reviews what's staged against HEAD, so it takes neither --local nor a PR."


class _Network:
    """requests.Session.get for the registry lookups: `hang` waits, as for a
    host that never answers or a DNS server that doesn't, until the test ends;
    otherwise each call answers with `reply` (a status code) or raises it."""

    def __init__(self, reply=None, hang=False):
        self.reply, self.hang, self.asked, self.done = reply, hang, [], threading.Event()

    def get(self, url, **kwargs):  # set on the class, but bound to this object: no session argument
        self.asked.append(url)
        if self.hang:
            self.done.wait(30)
        if isinstance(self.reply, Exception):
            raise self.reply
        return type("Response", (), {"status_code": self.reply, "json": lambda self: {}})()


@pytest.mark.parametrize("network, asked", [
    (_Network(hang=True), 1),  # unreachable: the first lookup takes the whole budget, and the rest aren't asked
    (_Network(requests.ConnectionError("refused")), 3),  # refused, or no DNS: each fails at once
])
def test_review_staged_gives_the_registry_lookups_a_few_seconds_in_all(committed, capsys, monkeypatch, network, asked):
    repo, git = committed
    (repo / "requirements.txt").write_text("flask==3.0.0\nhttpx==0.27.0\nrich==13.7.1\n")
    git("add", ".")
    monkeypatch.setattr(cli, "STAGED_LOOKUP_SECONDS", 0.5)
    monkeypatch.setattr("requests.Session.get", network.get)
    started = time.monotonic()
    try:
        assert cli.main(["review", "--staged", "--no-model", "--fail-on", "high"]) == 0
    finally:
        network.done.set()
    assert time.monotonic() - started < 5
    assert len(network.asked) == asked
    out, err = capsys.readouterr()
    assert "requirements.txt:1: low deps/new New dependency: `flask`" in out
    assert ("3 new dependencies couldn't be looked up at the registry (no network, or past the 0.5 s a staged review "
            "gives the lookups). The other checks ran.") in err


def test_review_staged_keeps_what_the_registry_answers(committed, capsys, monkeypatch):
    repo, git = committed
    (repo / "requirements.txt").write_text("flaskk==3.0.0\n")
    git("add", ".")
    monkeypatch.setattr("requests.Session.get", _Network(404).get)
    assert cli.main(["review", "--staged", "--no-model", "--fail-on", "high"]) == 1
    out, err = capsys.readouterr()
    assert "requirements.txt:1: high deps/missing `flaskk` isn't on PyPI" in out and "looked up" not in err


def test_review_staged_skips_the_name_checks_when_the_working_tree_isnt_whats_staged(committed, capsys, monkeypatch):
    repo, git = committed  # its app.py would make `app` ambiguous
    (repo / "pkg").mkdir()
    (repo / ".stratum").mkdir()
    (repo / ".stratum" / "policy.yml").write_text("checks:\n  unknown_names: true\n")
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "db.py").write_text("def connect():\n    pass\n")
    git("add", ".")
    git("commit", "-q", "-m", "Add the db module")
    (repo / "pkg" / "users.py").write_text("from pkg.db import close\n")
    git("add", ".")
    # `close` exists only in an edit that isn't staged: read from the working tree, the commit's import would look fine.
    (repo / "pkg" / "db.py").write_text("def connect():\n    pass\n\n\ndef close():\n    pass\n")
    assert cli.main(["review", "--staged", "--no-model"]) == 0
    out, err = capsys.readouterr()
    assert "has no `close`" not in out
    assert ("checks.unknown_names didn't run: they read the other modules from the working tree, where `pkg/db.py` differs "
            "from what's staged. Stage or stash it (new files too), and run it again.") in err
    git("checkout", "--", "pkg/db.py")  # as pre-commit stashes it
    assert cli.main(["review", "--staged", "--no-model"]) == 0
    out, err = capsys.readouterr()
    assert "pkg/users.py:1" in out and "`pkg/db.py` has no `close`" in out and "checks.unknown_names didn't run" not in err


def test_data_commands(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    storage = SQLiteStorage(db)
    storage.record_review("acme/widget", 7, "a" * 40, 50, "s", [Finding("t", "d", path="x.py")])
    storage.record_vote("acme/widget", "model:t", "f" * 12, 1, "mona", -1)
    storage.close()
    assert cli.main(["data", "delete", "--db", db, "--repo", "acme/widget", "--pr", "7"]) == 0
    assert cli.main(["data", "delete", "--db", db, "--feedback"]) == 0
    out = capsys.readouterr().out
    assert "for acme/widget#7" in out and "Deleted 1 vote and mute row(s)." in out
    with pytest.raises(SystemExit):
        cli.main(["data", "delete", "--db", str(tmp_path / "missing.db"), "--feedback"])


def test_data_on_a_file_that_isnt_a_database_or_is_newer(tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_text("not a database " * 100)
    newer = tmp_path / "newer.db"
    db = sqlite3.connect(newer)
    db.execute("PRAGMA user_version = 999")
    db.close()
    for db in (junk, newer):
        with pytest.raises(SystemExit) as exit_:
            cli.main(["data", "export", "--db", str(db), "--repo", "acme/widget"])
        assert str(exit_.value).startswith(f"Couldn't open the database at {db}: ")


@pytest.mark.parametrize("days, message", [("0", "must be between 1 and 36500"), ("999999", "must be between 1 and 36500"),
                                           ("abc", "must be a whole number")])
def test_purge_days_out_of_range_is_a_usage_error(days, message, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["data", "purge", "--days", days])
    assert exit_.value.code == 2 and f"--days: {message}" in capsys.readouterr().err


def test_help_prints_where_stdout_isnt_utf8(monkeypatch):
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    with pytest.raises(SystemExit) as exit_:
        cli.main(["data", "--help"])
    assert exit_.value.code == 0


def test_review_local_from_a_subfolder_with_diff_relative(tmp_path, capsys, monkeypatch):
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / "sub" / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    (repo / "sub" / "app.py").write_text('x = 1\nAPI_KEY = "sk-live-0123456789abcdef0123"\n')
    git("commit", "-q", "-am", "Add key")
    git("config", "diff.relative", "true")
    monkeypatch.chdir(repo / "sub")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    assert cli.main(["review", "--local", "main", "--no-model"]) == 1
    assert "sub/app.py:2" in capsys.readouterr().out  # the path from the repo root, not from sub/


def test_review_local_looks_names_up_in_the_modules_as_the_branch_leaves_them(tmp_path, capsys, monkeypatch):
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / ".stratum").mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / ".stratum" / "policy.yml").write_text("checks:\n  unknown_names: true\n")
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "db.py").write_text("def connect():\n    pass\n")
    (repo / "app" / "users.py").write_text("import sqlite3\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    (repo / "app" / "db.py").write_text("def connect():\n    pass\n\n\ndef close():\n    pass\n")
    (repo / "app" / "users.py").write_text("import sqlite3\nfrom app.db import close, connect, disconnect\n")
    git("commit", "-q", "-am", "Close the connection")
    monkeypatch.chdir(repo / "app")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    assert cli.main(["review", "--local", "main", "--no-model"]) == 0
    out = capsys.readouterr().out
    assert "app/users.py:2" in out and "`app/db.py` has no `disconnect`" in out
    assert "`close`" not in out  # the branch adds it


def test_review_local_finds_a_name_the_branch_removes_and_another_file_still_uses(tmp_path, capsys, monkeypatch):
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / ".stratum").mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / ".stratum" / "policy.yml").write_text("checks:\n  removed_names: true\n")
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "db.py").write_text("import sqlite3\n\n\ndef connect():\n    pass\n\n\ndef close():\n    pass\n")
    (repo / "app" / "users.py").write_text("from app.db import close, connect\n")
    (repo / "app" / "jobs.py").write_text("from app.db import close\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    # The branch renames both, and fixes one of the two files that import `close`.
    (repo / "app" / "db.py").write_text("import sqlite3\n\n\ndef open_connection():\n    pass\n\n\ndef shut():\n    pass\n")
    (repo / "app" / "jobs.py").write_text("from app.db import shut\n")
    git("commit", "-q", "-am", "Rename the connection helpers")
    monkeypatch.chdir(repo / "app")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    assert cli.main(["review", "--local", "main", "--no-model"]) == 0
    out = capsys.readouterr().out
    # The working tree is the branch already, so what it removed is read from the diff undone.
    assert "app/db.py:4" in out and "`app/db.py` no longer has `connect`, which is still used" in out
    assert "app/db.py:8" in out and "`app/db.py` no longer has `close`, which is still used" in out
    assert "`app/users.py:1` still imports it" in out and "app/jobs.py" not in out


def test_a_redelivered_webhook_is_ignored(server):
    def deliver(delivery):
        body = pr_event()
        signature = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        environ = {"PATH_INFO": "/webhook", "REQUEST_METHOD": "POST", "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
                   "HTTP_X_HUB_SIGNATURE_256": signature, "HTTP_X_GITHUB_EVENT": "pull_request", "HTTP_X_GITHUB_DELIVERY": delivery}
        return json.loads(b"".join(server(environ, lambda status, headers: None)))

    assert deliver("d-1") == {"queued": True}
    assert deliver("d-1") == {"queued": False, "duplicate": True}
    assert deliver("d-2")["queued"] is True


def test_the_job_skips_what_the_repos_policy_says(server, monkeypatch):
    from stratum_open.policy import parse_policy
    from stratum_open.runner import Context

    reviewed = []
    policy = parse_policy("review:\n  skip_labels: [wip]\n")
    monkeypatch.setattr(server, "context", lambda installation, repo, branch: Context(gh=None, policy=policy, model=None, bot="b[bot]"))
    monkeypatch.setattr("stratum_open.app.review_pull", lambda ctx, number: reviewed.append(number))
    payload = json.loads(pr_event(number=11))
    payload["pull_request"]["labels"] = [{"name": "wip"}]
    server.run("pull_request", payload)
    assert reviewed == []
    assert server.storage.export("acme/widget")["jobs"][-1]["outcome"] == "skipped"


def test_triage_post_with_a_personal_token_edits_its_own_queue_issue(monkeypatch, capsys):
    from fakes import FakeGitHub

    import stratum_open.github.api as api
    from stratum_open.github.api import GitHubError

    class AsAlice(FakeGitHub):
        """GitHub, with the CLI's token belonging to alice (GITHUB_TOKEN or `gh auth token`)."""

        user = {"login": "alice"}

        def repository(self):
            return {"default_branch": "main"}

        def file_text(self, path, ref):
            return None

        def get(self, path, **params):
            assert path == "/user"
            if self.user is None:
                raise GitHubError(403, "Resource not accessible by integration")
            return self.user

        def create_issue(self, title, body):
            issue = super().create_issue(title, body)
            issue["user"] = dict(self.user or {"login": "github-actions[bot]"})  # GitHub makes the token's user the author
            return issue

    gh = AsAlice(diff="")
    monkeypatch.setattr(api, "GitHub", lambda token, repo, api=None: gh)
    monkeypatch.setattr(cli, "_token", lambda: "t")
    monkeypatch.delenv("STRATUM_BOT_LOGIN", raising=False)
    for _ in range(3):
        assert cli.main(["triage", "acme/widget", "--post"]) == 0
    assert [(i["title"], i["user"]["login"]) for i in gh.issues] == [("Stratum triage queue", "alice")]

    # The Action's token can't read /user; its issues are github-actions[bot]'s, as before.
    gh.issues.clear()
    gh.user = None
    for _ in range(2):
        assert cli.main(["triage", "acme/widget", "--post"]) == 0
    assert [(i["title"], i["user"]["login"]) for i in gh.issues] == [("Stratum triage queue", "github-actions[bot]")]
    capsys.readouterr()


def test_triage_post_respects_triage_queue_off(monkeypatch, capsys):
    from fakes import FakeGitHub

    import stratum_open.github.api as api

    gh = FakeGitHub(diff="", files={(".stratum/policy.yml", "main"): "triage:\n  queue: false\n"})
    monkeypatch.setattr(api, "GitHub", lambda token, repo, api=None: gh)
    monkeypatch.setattr(cli, "_token", lambda: "t")
    monkeypatch.setenv("STRATUM_BOT_LOGIN", "github-actions[bot]")
    assert cli.main(["triage", "acme/widget", "--post"]) == 1
    assert gh.issues == [] and "triage.queue is off" in capsys.readouterr().err
    assert cli.main(["triage", "acme/widget"]) == 0  # printing the queue still works
    assert "Open PRs sorted by Stratum" in capsys.readouterr().out and gh.issues == []


# ── the triage queue ─────────────────────────────────────────────────────


@pytest.fixture
def jobs(server, monkeypatch):
    """Jobs that need no GitHub: a review or a command only records itself, and so does a queue rebuild.
    `now` is the server's clock, and `rebuilt` lists (when, repo) for each rebuild."""
    from stratum_open.policy import parse_policy
    from stratum_open.runner import Context, Outcome

    state = {"policy": parse_policy(""), "now": 0.0, "rebuilt": [], "done": "review"}
    server.clock = lambda: state["now"]
    monkeypatch.setattr(server, "context", lambda installation, repo, branch: Context(gh=None, policy=state["policy"], model=None, bot="b[bot]"))
    monkeypatch.setattr("stratum_open.app.collect_votes", lambda gh, number, bot, storage: 0)
    monkeypatch.setattr("stratum_open.app.review_pull", lambda ctx, number: Outcome(number, "published"))
    monkeypatch.setattr("stratum_open.app.handle", lambda ctx, event, payload: state["done"])
    monkeypatch.setattr(server, "rebuild_queue", lambda installation, repo, branch: state["rebuilt"].append((state["now"], repo)))
    return state


def test_several_jobs_for_one_repo_within_a_minute_lead_to_one_queue_rebuild(server, jobs):
    for number, at in zip(range(7, 12), (0, 10, 25, 40, 55)):
        jobs["now"] = at
        server.run("pull_request", json.loads(pr_event(number=number)))
    jobs["now"] = 114
    assert server.rebuild_due() == []  # a minute after the last job, not the first
    jobs["now"] = 115
    assert server.rebuild_due() == ["acme/widget"]
    jobs["now"] = 1000
    assert server.rebuild_due() == []
    assert jobs["rebuilt"] == [(115, "acme/widget")]


def test_the_queue_waits_for_the_repos_queued_jobs_but_not_past_ten_minutes(server, jobs):
    server.run("pull_request", json.loads(pr_event(number=7)))
    call(server, pr_event(number=8))  # its review is queued, so the rebuild after #7 waits for it
    call(server, pr_event(number=9).replace(b"acme/widget", b"acme/other"))  # another repo's job doesn't hold it
    jobs["now"] = 599
    assert server.rebuild_due() == []
    jobs["now"] = 600
    assert server.rebuild_due() == ["acme/widget"]


def test_a_repo_thats_never_quiet_gets_a_handful_of_rebuilds(server, jobs):
    for now in range(0, 1200, 5):  # the rebuild loop looks every 5 seconds; a review ends every 30 for 15 minutes
        jobs["now"] = now
        if now % 30 == 0 and now <= 900:
            server.run("pull_request", json.loads(pr_event(number=now + 1)))
        server.rebuild_due()
    assert jobs["rebuilt"] == [(600, "acme/widget"), (960, "acme/widget")]  # 31 reviews


def test_a_repo_with_the_queue_off_waits_for_no_rebuild(server, jobs):
    from stratum_open.policy import parse_policy

    jobs["policy"] = parse_policy("triage:\n  queue: false\n")
    server.run("pull_request", json.loads(pr_event()))
    assert server._rebuilds == {}


@pytest.mark.parametrize("action", ["closed", "converted_to_draft"])
def test_a_pr_leaving_the_queue_rebuilds_it_without_a_job(server, jobs, action):
    assert call(server, pr_event(action=action))[1] == {"queued": True}
    assert server._queue.qsize() == 0 and server._pending == {}  # no review, no clone, no model
    jobs["now"] = 60
    assert server.rebuild_due() == ["acme/widget"]


@pytest.mark.parametrize("done, rebuilt", [("review", True), ("ignore", True), ("explain", False), ("refused", False), (None, False)])
def test_a_command_rebuilds_the_queue_only_when_it_can_have_changed_a_label(server, jobs, done, rebuilt):
    jobs["done"] = done
    payload = {"action": "created", "installation": {"id": 1}, "repository": {"full_name": "acme/widget", "default_branch": "main"},
               "issue": {"number": 7, "pull_request": {}}, "comment": {"id": 9, "body": "/stratum review"}}
    server.run("issue_comment", payload)
    assert ("acme/widget" in server._rebuilds) is rebuilt


def test_uninstalling_drops_the_repos_queue_rebuild(server, jobs):
    server.run("pull_request", json.loads(pr_event()))
    call(server, json.dumps({"action": "deleted", "installation": {"id": 1}, "repositories": [{"full_name": "acme/widget"}]}).encode(),
         event="installation")
    server.run("pull_request", json.loads(pr_event()))  # a job that was running when the App was uninstalled
    jobs["now"] = 1000
    assert server.rebuild_due() == [] and jobs["rebuilt"] == []
