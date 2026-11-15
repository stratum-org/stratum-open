"""The seam between the review and the code host, and what the GitLab
client sends: the parts that need no server."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stratum_open.diff import parse_diff
from stratum_open.findings import Finding
from stratum_open.github import publish
from stratum_open.github.api import GitHub
from stratum_open.gitlab.api import HINT, GitLab, _git_diff, gitlab_body
from stratum_open.platform import SURFACE


ROOT = Path(__file__).resolve().parent.parent / "stratum_open"


def test_the_runner_and_publish_call_only_the_platform_surface():
    """A call added to the runner or to publish has to be one every host's
    client answers: add it to `Platform`, and to `GitLab`."""
    used = set()
    for path in (ROOT / "runner.py", ROOT / "github" / "publish.py"):
        used |= set(re.findall(r"\bgh\.([a-z_]+)", path.read_text(encoding="utf-8")))
    assert used <= SURFACE, sorted(used - SURFACE)


@pytest.mark.parametrize("cls", [GitHub, GitLab])
def test_every_client_answers_the_whole_surface(cls):
    """(`tests/fakes.FakeGitHub` answers what each of its tests needs, so it isn't held to the whole surface.)"""
    missing = {name for name in SURFACE - {"repo"} if not callable(getattr(cls, name, None))}
    assert not missing, sorted(missing)


# ── bodies ───────────────────────────────────────────────────────────────


def _finding(replacement: str | None, start: int = 5, end: int = 5) -> Finding:
    return Finding(title="T", detail="D", severity="high", category="bug", path="a.py", line_start=start, line_end=end,
                   source="model", rule="model:t", replacement=replacement)


def test_the_suggestion_takes_gitlabs_header():
    body = publish.comment_body(_finding("x = 1"), feedback=False)
    assert "```suggestion:-0+0\nx = 1\n```" in gitlab_body(body, suggestion_above=0)
    assert "```suggestion:-3+0\n" in gitlab_body(publish.comment_body(_finding("a\nb\nc\nd", 2, 5), feedback=False), suggestion_above=3)


def test_the_longest_fence_is_the_suggestion_and_code_inside_is_kept():
    code = "s = '```suggestion'\nt = 1"
    body = publish.comment_body(_finding(code), feedback=False)
    out = gitlab_body(body, suggestion_above=0)
    assert "````suggestion:-0+0\n" + code + "\n````" in out


def test_a_suggestion_gitlab_could_run_as_a_quick_action_is_left_out():
    body = publish.comment_body(_finding("x = 1\n/merge"), feedback=False)
    out = gitlab_body(body, suggestion_above=0)
    assert "suggestion" not in out and "/merge" not in out
    assert out.startswith("**High · bug:** T") and "stratum:finding" in out


def test_slash_lines_are_escaped_outside_code_blocks_only():
    body = "/approve\n  /close now\n```\n/not escaped\n```\n~~~\n/nor this\n~~~\n/label ~x\n// a comment\npath /a"
    out = gitlab_body(body)
    assert out.split("\n") == ["\\/approve", "  \\/close now", "```", "/not escaped", "```", "~~~", "/nor this", "~~~",
                               "\\/label ~x", "// a comment", "path /a"]


def test_an_unclosed_fence_runs_to_the_end():
    assert gitlab_body("```\n/merge") == "```\n/merge"


def test_the_hints_name_no_command_and_running_twice_changes_nothing():
    inline = publish.comment_body(_finding("x = 1"), feedback=False)
    once = gitlab_body(inline, suggestion_above=0)
    assert publish.HINT not in once and HINT in once and "/stratum" not in once
    assert gitlab_body(once) == once
    closed = publish.closed_body(once, "Fixed.")
    assert gitlab_body(closed) == closed  # the struck-through edit keeps the suggestion as posted


def test_the_summary_footer_drops_the_command_hint():
    from stratum_open.policy import Policy

    body = publish.render_summary(publish.Summary(findings=[], inline=set(), policy=Policy(), model_name=None), publish.State())
    assert "/stratum help" in body
    out = gitlab_body(body)
    assert "/stratum " not in out and out.endswith(publish.State().encode())
    assert "[Stratum Open](https://github.com/stratum-org/stratum-open)" in out  # the link stays: it's no command


# ── GitLab's diff list as a git diff ─────────────────────────────────────


@pytest.mark.parametrize("item, status, binary", [
    ({"old_path": "a.py", "new_path": "a.py", "diff": "@@ -1 +1 @@\n-a\n+b\n"}, "modified", False),
    ({"old_path": "a.py", "new_path": "a.py", "new_file": True, "diff": "@@ -0,0 +1 @@\n+b\n"}, "added", False),
    ({"old_path": "a.py", "new_path": "a.py", "deleted_file": True, "diff": "@@ -1 +0,0 @@\n-a\n"}, "deleted", False),
    ({"old_path": "a.py", "new_path": "b.py", "renamed_file": True, "diff": ""}, "renamed", False),
    ({"old_path": "a.py", "new_path": "b.py", "renamed_file": True, "diff": "@@ -1 +1 @@\n-a\n+b"}, "renamed", False),
    ({"old_path": "logo.png", "new_path": "logo.png", "diff": ""}, "modified", True),
    ({"old_path": "big.json", "new_path": "big.json", "diff": "", "too_large": True}, "modified", True),
], ids=["modified", "added", "deleted", "moved", "renamed-and-edited", "binary", "too-large"])
def test_each_kind_of_file_parses_as_git_would_write_it(item, status, binary):
    [file] = parse_diff(_git_diff(item))
    assert (file.status, file.binary) == (status, binary)
    assert file.path == item["new_path"] if status != "deleted" else file.path == item["old_path"]
    if status == "renamed":
        assert file.old_path == "a.py"
    if item["diff"]:
        assert file.hunks


def test_the_example_pipeline_checks_out_nothing_of_the_mr():
    import yaml

    job = yaml.safe_load((ROOT.parent / "examples" / "gitlab-ci.yml").read_text(encoding="utf-8"))["stratum"]
    assert job["variables"]["GIT_STRATEGY"] == "none"
    assert job["rules"] == [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'}]
    script = "\n".join(job["script"])
    assert script.rstrip().endswith("stratum-open gitlab")
    assert '--branch "$CI_DEFAULT_BRANCH"' in script and "CI_COMMIT" not in script and "CI_MERGE_REQUEST_SOURCE" not in script
