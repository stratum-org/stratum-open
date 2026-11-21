"""`stratum-open mcp` as a real process, as a coding agent's client starts
it: the SDK's stdio client talks to it over its stdin and stdout, against a
checkout with a planted secret and the fake GitHub."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest

from conftest import DIFF
from contract import REPO_ROOT

pytestmark = pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason='needs the MCP SDK: pip install -e ".[mcp]"')

SECRET = 'API_KEY = "sk-live-0123456789abcdef0123"'


def checkout(tmp_path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "gitconfig").write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "a",
           "GIT_AUTHOR_EMAIL": "a@x.y", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x.y"}

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text(f"x = 1\n{SECRET}\n", encoding="utf-8")
    git("commit", "-q", "-am", "Add a key")
    return str(repo)


def test_the_server_over_stdio_checks_a_checkout_and_triages_a_pr_reading_only(fake_github, tmp_path):
    import anyio
    from mcp import Client, StdioServerParameters, stdio_client

    fake_github.add_pull(7, diff=DIFF)
    repo = checkout(tmp_path)
    server = StdioServerParameters(
        command=sys.executable, args=["-X", "dev", "-W", "error::Warning:stratum_open", "-m", "stratum_open", "mcp", "--repo", repo],
        env={"PYTHONPATH": str(REPO_ROOT), "GITHUB_TOKEN": fake_github.token, "STRATUM_GITHUB_API": fake_github.url,
             "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"},
    )

    async def main():
        async with Client(stdio_client(server)) as client:  # mcp 2.0's Client takes a transport, not the parameters
            tools = await client.list_tools()
            checked = await client.call_tool("check_local", {"base": "main"})
            triaged = await client.call_tool("triage_pr", {"repo": fake_github.repo, "number": 7})
            return tools, checked, triaged

    tools, checked, triaged = anyio.run(main)
    assert [tool.name for tool in tools.tools] == ["check_local", "review_local", "explain_finding", "check_policy", "triage_pr"]
    assert not checked.is_error, checked.content
    [found] = checked.structured_content["findings"]
    assert (found["rule"], found["path"], found["line"], found["code"]) == ("code/secret", "app.py", 2, SECRET)

    assert not triaged.is_error, triaged.content
    data = triaged.structured_content
    assert (data["status"], data["key"], data["label"]) == ("dry-run", "needs_work", "triage: needs work")
    assert "code/sql" in {f["rule"] for f in data["findings"]}
    # Read only: GETs, and the GraphQL query for the linked issues; nothing posted, labelled or closed.
    assert {(r["method"], r["path"]) for r in fake_github.requests if r["method"] != "GET"} == {("POST", "/graphql")}
    assert fake_github.posted_text() == "" and fake_github.issue_labels[7] == [] and not fake_github.labels and not fake_github.closed
