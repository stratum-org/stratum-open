"""What a review needs from the code host: the seam between the review and
GitHub, GitLab or anything else.

`runner.review_pull` and `github.publish` call these methods on
`Context.gh`, and nothing else. `github.api.GitHub` is the reference
implementation, and the shapes below are GitHub's: another host's client
(`gitlab.api.GitLab`) answers in the same shapes, so the review, the summary,
the inline comments and the triage code are shared as they are. A client
raises `GitHubError` (or a subclass) when the host refuses a call; the runner
catches that one type.

The shapes, as the runner and `publish` read them:

- `pull(number)`: {"number", "state": "open" | ..., "draft", "title", "body",
  "user": {"login", "type"} | None, "head": {"sha", "ref"}, "labels":
  [{"name"}], "additions", "deletions"}.
- `issue_comments(number)`: oldest first, [{"id", "body", "user": {"login"}}].
- `pull_commits(number)`: the first 100 of the PR's commits, [{"sha",
  "commit": {"message", "author": {"name", "email"}}}], for `triage.agent_prs`.
- `review_threads(number)`: [{"id", "resolved", "path", "comments": [{"id",
  "body", "author"}]}], the first comment being the one that opened the thread.
- `create_review(number, head_sha, comments)`: each comment is {"path",
  "line", "side": "RIGHT", "body"} plus "start_line" for a range, in new-file
  line numbers; a comment the host won't place is a `GitHubError` with
  status 422, and the caller then posts the comments one at a time.
- `check_run(...)`: the review's verdict on the head commit (a check run on
  GitHub, a commit status on GitLab). `review.early_checks` starts it first
  with `start_check_run`, and `stop_early` reads it back with
  `check_run_output`.

`SURFACE` lists the names, and a test keeps it in step with what the runner
and `publish` call, so a new call there shows up as one every host's client
has to answer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class CI(StrEnum):
    """What a commit's CI says, as `ci_state` reads it."""

    PASSED = "passed"  # every check and status passed
    FAILED = "failed"  # one failed
    PENDING = "pending"  # one is still running or queued
    HELD = "held"  # a workflow waits for a maintainer to approve it, so it hasn't run (GitHub, on a first-time contributor's PR)
    NONE = "none"  # the commit has no CI


class Platform(Protocol):
    repo: str  # a name for logs and storage keys: owner/name, or a GitLab project path

    def repository(self) -> dict: ...  # {"default_branch": ...}

    def pull(self, number: int) -> dict: ...

    def pull_diff(self, number: int) -> str: ...  # a unified diff, as `git diff` writes it

    def pull_commits(self, number: int) -> list[dict]: ...  # one request: the first 100

    def file_text(self, path: str, ref: str, *, max_bytes: int | None = None) -> str | None: ...

    def compare_files(self, base: str, head: str) -> list[str] | None: ...  # None: review everything again

    def issue_comments(self, number: int) -> list[dict]: ...

    def comment(self, number: int, body: str) -> dict: ...

    def edit_comment(self, comment_id: int, body: str) -> dict: ...

    def review_threads(self, number: int) -> list[dict]: ...

    def create_review(self, number: int, head_sha: str, comments: list[dict], body: str = "") -> dict: ...

    def edit_review_comment(self, comment_id: int, body: str) -> dict: ...

    def delete_review_comment(self, comment_id: int) -> None: ...  # `review.early_checks`: an early comment the review doesn't keep

    def resolve_thread(self, thread_id: str) -> None: ...

    def check_run(self, head_sha: str, name: str, conclusion: str, title: str, summary: str, text: str,
                  annotations: list[dict], run_id: int | None = None) -> dict: ...  # run_id: complete the one started below

    def start_check_run(self, head_sha: str, name: str, title: str, summary: str) -> dict: ...  # in progress; {"id": ...}

    def check_run_output(self, run_id: int) -> dict: ...  # a started run's {"title", "summary"}; {} when unknown

    def ci_state(self, sha: str, *, ignore: set[str]) -> CI: ...

    def linked_issues(self, number: int) -> list[int]: ...

    def commit_emails(self, login: str) -> set[str]: ...

    def ensure_label(self, name: str, color: str, description: str) -> None: ...

    def add_labels(self, number: int, labels: list[str]) -> None: ...

    def remove_label(self, number: int, label: str) -> None: ...

    def close(self, number: int) -> None: ...


SURFACE = frozenset(name for name in vars(Platform) if not name.startswith("_")) | {"repo"}
