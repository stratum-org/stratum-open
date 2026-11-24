"""`stratum-open mcp`, driven through the MCP SDK's own in-process client:
the tools it lists, the checks on a repo with a planted secret, the model's
review with the fake model, and the core install without the SDK."""

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fakes import FakeModel

from stratum_open.mcp_server import TOOLS, Deadline, ReadOnly
from stratum_open.model import Reply

needs_sdk = pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason='needs the MCP SDK: pip install -e ".[mcp]"')

SECRET = 'API_KEY = "sk-live-0123456789abcdef0123"'
FIRST = "    return items[0]"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout of `feature`, one commit past `main`, that adds a secret and a function to app.py."""
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    path = tmp_path / "repo"
    path.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True, env=env).stdout

    git("init", "-q", "-b", "main")
    (path / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    (path / "app.py").write_text(f"x = 1\n{SECRET}\n\n\ndef first(items):\n{FIRST}\n")
    git("commit", "-q", "-am", "Add a key and first()")
    monkeypatch.chdir(path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    return SimpleNamespace(path=path, git=git)


def session(work):
    """`await work(client)`, with a client connected to a new server in this process."""
    import anyio
    from mcp import Client

    from stratum_open.mcp_server import build

    async def main():
        async with Client(build()) as client:
            return await work(client)

    return anyio.run(main)


def call(name, arguments=None, **kwargs):
    return session(lambda client: client.call_tool(name, arguments or {}, **kwargs))


def model_answer(*findings, summary="Adds a key and a helper."):
    return json.dumps({"summary": summary, "matches_description": "yes", "findings": list(findings)})


EMPTY_LIST = {"file": "app.py", "code": FIRST.strip(), "line": 6, "severity": "high", "category": "bug", "title": "Empty list",
              "detail": "first() raises IndexError on an empty list.", "fix": "Return None for an empty list."}


@needs_sdk
def test_it_lists_the_five_tools_and_each_only_reads():
    tools = session(lambda client: client.list_tools()).tools
    assert [tool.name for tool in tools] == list(TOOLS)
    assert all(tool.annotations.read_only_hint and not tool.annotations.destructive_hint for tool in tools)
    by_name = {tool.name: tool for tool in tools}
    assert by_name["check_local"].input_schema["properties"]["base"]["default"] is None  # the remote's default branch
    assert set(by_name["review_local"].input_schema["properties"]) == {"base", "timeout"}
    assert by_name["triage_pr"].input_schema["required"] == ["repo", "number"]
    # The JSON's shape is in the listing, for a client to check it against.
    finding = by_name["check_local"].output_schema["$defs"]["FindingJSON"]["properties"]
    assert {"rule", "severity", "path", "line", "message", "fingerprint", "code"} <= set(finding)


@needs_sdk
def test_check_local_finds_the_planted_secret(repo):
    result = call("check_local", {"base": "main"})
    assert not result.is_error, result.content
    data = result.structured_content
    assert json.loads(result.content[0].text) == data  # the same JSON for a client that reads only text
    [found] = data["findings"]
    assert (found["rule"], found["severity"], found["source"], found["path"], found["line"]) == ("code/secret", "critical", "check", "app.py", 2)
    assert found["code"] == SECRET and re.fullmatch(r"[0-9a-f]{12}", found["fingerprint"])
    assert "hard-coded API key" in found["message"] and found["suggestion"]
    assert (data["total"], data["omitted"], data["files"], data["conclusion"]) == (1, 0, 1, "failure")
    assert data["model"] is None and data["uncommitted"] == [] and data["notes"] == []
    assert data["head"] == repo.git("rev-parse", "HEAD").strip()


@needs_sdk
def test_check_local_names_the_changes_it_didnt_read(repo):
    (repo.path / "app.py").write_text("x = 2\n")
    (repo.path / "new.py").write_text("y = 1\n")
    data = call("check_local").structured_content
    assert [f["rule"] for f in data["findings"]] == ["code/secret"]  # HEAD, as committed
    assert sorted(data["uncommitted"]) == ["app.py", "new.py"]
    assert "changes that aren't committed, which this didn't read" in data["notes"][1]


@needs_sdk
def test_check_local_with_nothing_changed_or_a_bad_base(repo):
    repo.git("checkout", "-q", "main")
    data = call("check_local").structured_content
    assert data["findings"] == [] and data["notes"] == ["Compared with main: no base was given, and origin/HEAD isn't set (`git remote set-head origin --auto` sets it).", "No changes between main and HEAD."]
    result = call("check_local", {"base": "nosuch"})
    assert result.is_error and "git couldn't compare nosuch with HEAD" in result.content[0].text
    result = call("check_local", {"base": "--output=written.txt"})  # an option, not a branch: git would write a file
    assert result.is_error and "Not a branch or commit" in result.content[0].text
    assert not (repo.path / "written.txt").exists()


@needs_sdk
def test_without_a_base_it_compares_with_the_remotes_default_branch(repo):
    data = call("check_local").structured_content
    assert data["base"] == "main" and data["notes"][0] == "Compared with main: no base was given, and origin/HEAD isn't set (`git remote set-head origin --auto` sets it)."
    # origin/HEAD names trunk, which has no local branch yet, then one.
    main = repo.git("rev-parse", "main").strip()
    repo.git("update-ref", "refs/remotes/origin/trunk", main)
    repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    data = call("check_local").structured_content
    assert data["base"] == "origin/trunk" and [f["rule"] for f in data["findings"]] == ["code/secret"]
    assert data["notes"][0] == "Compared with origin/trunk, the remote's default branch: no base was given, and there's no local trunk."
    repo.git("branch", "trunk", main)
    data = call("check_local").structured_content
    assert data["base"] == "trunk" and data["notes"][0] == "Compared with trunk, the remote's default branch: no base was given."


@needs_sdk
def test_the_checks_that_build_the_code_dont_run(repo, monkeypatch):
    """Building runs the code (build scripts, macros, cgo): a read-only tool doesn't, and says so."""
    (repo.path / ".stratum").mkdir()
    (repo.path / ".stratum" / "policy.yml").write_text("checks:\n  go_vet: true\n")
    repo.git("add", ".")
    repo.git("commit", "-q", "-m", "Turn on go vet")
    repo.git("branch", "-f", "main")
    (repo.path / "go.mod").write_text("module example.com/m\n\ngo 1.22\n")
    (repo.path / "main.go").write_text('package main\n\nimport "fmt"\n\nfunc main() {\n\tfmt.Printf("%d\\n", "x")\n}\n')
    repo.git("add", ".")
    repo.git("commit", "-q", "-m", "Add a Go program")
    built = []
    monkeypatch.setattr("stratum_open.checks.go_vet.available", lambda: True)
    monkeypatch.setattr("stratum_open.checks.tools.group_by_root", lambda *args: built.append(args) or {})
    data = call("check_local", {"base": "main"}).structured_content
    assert built == []
    assert "go vet didn't run: it builds the code, which runs the code's build scripts and macros, and these tools only read." in " ".join(data["notes"])


@needs_sdk
def test_findings_are_capped_and_the_rest_counted(repo):
    for i in range(60):
        (repo.path / f"keys_{i:02}.py").write_text(f"{SECRET}\n")
    repo.git("add", ".")
    repo.git("commit", "-q", "-m", "More keys")
    data = call("check_local").structured_content
    assert data["total"] == 61 and len(data["findings"]) == 50 and data["omitted"] == 11


@needs_sdk
def test_explain_finding_shows_the_code_around_it_and_the_fix(repo):
    async def work(client):
        found = (await client.call_tool("check_local", {})).structured_content["findings"][0]
        known = await client.call_tool("explain_finding", {"fingerprint": found["fingerprint"]})
        unknown = await client.call_tool("explain_finding", {"fingerprint": "0123456789ab"})
        return found, known, unknown

    found, known, unknown = session(work)
    explained = known.structured_content
    assert {k: explained[k] for k in found} == found
    assert explained["context"] == f"1  x = 1\n2  {SECRET}\n3  \n4  \n5  def first(items):\n6  {FIRST}"
    assert explained["context_start"] == 1 and explained["head"] == repo.git("rev-parse", "HEAD").strip()
    assert unknown.is_error and "Call check_local or review_local first" in unknown.content[0].text
    # Kept in memory only: a new server knows nothing of the last one's findings.
    assert call("explain_finding", {"fingerprint": found["fingerprint"]}).is_error


@needs_sdk
def test_review_local_with_the_fake_model(repo, monkeypatch):
    model = FakeModel(model_answer(EMPTY_LIST))
    monkeypatch.setattr("stratum_open.mcp_server.model_from_policy", lambda policy: model)
    heard = []

    async def progress(done, total, message):
        heard.append((done, message))

    data = call("review_local", {"base": "main"}, progress_callback=progress).structured_content
    by_rule = {f["rule"]: f for f in data["findings"]}
    assert set(by_rule) == {"code/secret", "model:empty-list"}
    assert by_rule["model:empty-list"]["line"] == 6 and by_rule["model:empty-list"]["code"] == FIRST
    assert by_rule["model:empty-list"]["suggestion"] == "Return None for an empty list."
    assert data["model"] == {"name": "fake-7b", "answered": True, "profile": "small"}
    assert data["summary"] == "Adds a key and a helper." and data["notes"] == []
    assert model.prompts and "def first(items):" in model.prompts[0][1]
    assert heard[0] == (0, "Running the checks, then the model's review") and (1, "1 model call done") in heard


@needs_sdk
def test_review_local_with_no_model_answering_returns_the_checks_and_says_so(repo, monkeypatch):
    error = "couldn't reach the model at http://localhost:11434/v1: ConnectionError"
    monkeypatch.setattr("stratum_open.mcp_server.model_from_policy", lambda policy: FakeModel(*[Reply("", "fake-7b", 0, error=error)] * 5))
    data = call("review_local", {"base": "main"}).structured_content
    assert [f["rule"] for f in data["findings"]] == ["code/secret"]
    assert data["model"]["answered"] is False
    assert data["notes"][0].startswith(f"No model answered ({error}), so this has only the checks. To have the model review it, point "
                                       "STRATUM_MODEL_ENDPOINT")


@needs_sdk
def test_review_local_with_the_endpoint_off_returns_the_checks_and_says_why(repo, monkeypatch):
    monkeypatch.setenv("STRATUM_MODEL_ENDPOINT", "off")
    data = call("review_local", {"base": "main"}).structured_content
    assert [f["rule"] for f in data["findings"]] == ["code/secret"]
    assert data["model"] is None
    assert "The model is off (review.endpoint: off, or STRATUM_MODEL_ENDPOINT=off), so this has only the checks." in data["notes"]


@needs_sdk
def test_review_local_names_the_commit_it_started_on_when_head_moves(repo, monkeypatch):
    class Committing(FakeModel):
        def complete(self, system, user, **kwargs):  # the agent commits while the model reads
            (repo.path / "app.py").write_text("x = 1\n")
            repo.git("commit", "-q", "-am", "Take the key out")
            return super().complete(system, user, **kwargs)

    started = repo.git("rev-parse", "HEAD").strip()
    monkeypatch.setattr("stratum_open.mcp_server.model_from_policy", lambda policy: Committing(model_answer()))

    async def work(client):
        data = (await client.call_tool("review_local", {"base": "main"})).structured_content
        found = next(f for f in data["findings"] if f["rule"] == "code/secret")
        return data, (await client.call_tool("explain_finding", {"fingerprint": found["fingerprint"]})).structured_content

    data, explained = session(work)
    moved = repo.git("rev-parse", "HEAD").strip()
    assert data["head"] == explained["head"] == started != moved
    assert f"HEAD moved from {started[:7]} to {moved[:7]} during the review" in " ".join(data["notes"])
    assert SECRET in explained["context"]  # the code as it was on the commit reviewed


@needs_sdk
def test_review_local_stops_at_its_timeout(repo, monkeypatch):
    class Slow(FakeModel):
        def describe(self):  # a model server slow to say what it is: the time is up before the first call
            time.sleep(1.2)
            from stratum_open.profiles import ModelInfo
            return ModelInfo()

    model = Slow(model_answer(EMPTY_LIST))
    monkeypatch.setattr("stratum_open.mcp_server.model_from_policy", lambda policy: model)
    data = call("review_local", {"timeout": 1}).structured_content
    assert model.prompts == []
    assert [f["rule"] for f in data["findings"]] == ["code/secret"]
    assert "The review stopped at its timeout of 1 second, with 1 model call not made." in " ".join(data["notes"])
    assert data["base"] == "main"  # no base given, and no origin/HEAD
    assert call("review_local", {"timeout": 0}).is_error


def test_a_deadline_bounds_the_call_in_flight_and_refuses_the_rest():
    model = FakeModel("{}", "{}")
    model.timeout = 900
    deadline = Deadline(model, 5)
    deadline.context_tokens = 4096  # the engine sets the context window on the model it's given
    assert deadline.complete("system", "user").ok
    assert model.timeout <= 5 and model.context_tokens == 4096 and deadline.name == "fake-7b"
    late = Deadline(model, 0)
    reply = late.complete("system", "user")
    assert reply.error == "review_local's timeout of 0 seconds passed" and late.refused == 1 and len(model.prompts) == 1
    assert late.passed() and not deadline.passed()


@needs_sdk
def test_check_policy_reads_a_file_in_the_checkout_only(repo):
    (repo.path / ".stratum").mkdir()
    (repo.path / ".stratum" / "policy.yml").write_text("review:\n  model: cloud\n")
    data = call("check_policy").structured_content
    assert (data["path"], data["exists"], data["usable"]) == (".stratum/policy.yml", True, False)
    assert data["errors"] == ["review.model must be one of: local, hosted."]
    assert data["message"] == ".stratum/policy.yml can't be used: 1 error. Reviews use the defaults until it's fixed."
    assert call("check_policy", {"path": "missing.yml"}).structured_content["message"] == "No missing.yml: the defaults apply."
    outside = call("check_policy", {"path": "../gitconfig"})
    assert outside.is_error and "reads files in this checkout only" in outside.content[0].text


@needs_sdk
def test_triage_pr_says_what_it_needs():
    result = call("triage_pr", {"repo": "not a repo", "number": 1})
    assert result.is_error and "Name it owner/name" in result.content[0].text


def test_github_is_only_read():
    session_ = ReadOnly()
    for method, url, body in [
        ("POST", "https://api.github.com/repos/acme/widget/issues/7/comments", {"body": "hi"}),
        ("PATCH", "https://api.github.com/repos/acme/widget/issues/7", {"state": "closed"}),
        ("POST", "https://api.github.com/graphql", {"query": "mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}"}),
    ]:
        with pytest.raises(RuntimeError, match="only reads from GitHub"):
            session_.request(method, url, json=body)


def test_the_core_install_needs_no_sdk(tmp_path):
    """With the SDK missing, the CLI and this module still import, and `stratum-open mcp` says what to install."""
    code = ("import sys; sys.modules['mcp'] = None\n"
            "import stratum_open.mcp_server\n"
            "from stratum_open.cli import main\n"
            "raise SystemExit(main(['mcp']))\n")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, timeout=120)
    assert result.returncode == 1, result.stderr
    assert result.stderr.strip() == 'stratum-open mcp needs the MCP SDK, version 2: pip install "stratum-open[mcp]"'
    assert result.stdout == ""
