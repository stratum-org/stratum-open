"""In-memory stand-ins for GitHub and the model, shared by the tests."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

from stratum_open.github.api import GitHubError
from stratum_open.model import Reply
from stratum_open.platform import CI

_ids = itertools.count(1000)


class FakeModel:
    """Answers each call with the next reply in `replies` (text or Reply)."""

    def __init__(self, *replies, name: str = "fake-7b"):
        self.replies = list(replies)
        self.name = name
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 2048, json_mode: bool = True) -> Reply:
        self.prompts.append((system, user))
        reply = self.replies.pop(0) if self.replies else "{}"
        return reply if isinstance(reply, Reply) else Reply(reply, self.name, 0.1)


class FakeGitHub:
    def __init__(self, *, diff: str, repo: str = "acme/widget", number: int = 7, head: str = "a" * 40, author: str = "carol",
                 title: str = "Add a thing", body: str = "This adds a thing that the issue asked for, with tests.", files: dict | None = None,
                 permissions: dict | None = None, ci: CI = CI.PASSED, linked: list[int] | None = None):
        self.repo = repo
        self.diff = diff
        self.diffs: dict[int, str] = {}  # a PR's own diff, where it isn't `diff`
        self.files = files or {}  # (path, ref) -> text
        self.permissions = permissions or {}
        self.ci = ci
        self.linked = linked if linked is not None else [3]
        self.pulls = {number: {
            "number": number, "state": "open", "draft": False, "title": title, "body": body,
            "user": {"login": author}, "head": {"sha": head}, "additions": 10, "deletions": 2, "labels": [],
        }}
        self.issue_comments_: dict[int, list[dict]] = {number: []}
        self.reviews: list[dict] = []
        self.review_comments_: list[dict] = []
        self.threads: list[dict] = []
        self.checks: list[dict] = []
        self.labels: dict[int, list[str]] = {number: []}
        self.created_labels: list[str] = []
        self.replies: list[tuple[int, int, str]] = []
        self.resolved: list[str] = []
        self.reactions_: dict[int, list[dict]] = {}
        self.history_review_comments: list[dict] = []  # inline comments on other PRs, for review.learn_from_history
        self.compare: dict[tuple[str, str], list[str] | None] = {}
        self.closed: list[int] = []
        self.issues: list[dict] = []
        self.reject_review = False
        self.now = datetime.now(timezone.utc).isoformat()  # created_at for new comments
        self.edited: list[tuple[int, str]] = []
        self.refuse_resolve = False

    def move_head(self, number: int, sha: str, diff: str | None = None) -> None:
        self.pulls[number]["head"]["sha"] = sha
        if diff is not None:
            self.diff = diff

    # the GitHub surface the code uses
    def repository(self):
        return {"default_branch": "main"}

    def pull(self, number):
        return self.pulls[number]

    def open_pulls(self):
        return list(self.pulls.values())

    def closed_pulls(self):
        return iter(sorted((p for p in self.pulls.values() if p["state"] == "closed"), key=lambda p: -p["number"]))

    def pull_diff(self, number):
        return self.diffs.get(number, self.diff)

    def file_text(self, path, ref, *, max_bytes=None):
        text = self.files.get((path, ref))
        return None if text is not None and max_bytes is not None and len(text.encode("utf-8")) > max_bytes else text

    def compare_files(self, base, head):
        return self.compare.get((base, head))

    def issue_comments(self, number):
        return list(self.issue_comments_.get(number, []))

    def comment(self, number, body):
        comment = {"id": next(_ids), "body": body, "user": {"login": "stratum[bot]", "type": "Bot"}}
        self.issue_comments_.setdefault(number, []).append(comment)
        return comment

    def edit_comment(self, comment_id, body):
        for comments in self.issue_comments_.values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    return comment
        raise GitHubError(404, "no such comment")

    def review_comments(self, number):
        return list(self.review_comments_)

    def review_comment(self, comment_id):
        return next((c for c in self.review_comments_ if c["id"] == comment_id), None)

    def recent_review_comments(self, limit):
        """This PR's inline comments and `history_review_comments` (other PRs'), newest first."""
        url = f"https://api.github.com/repos/{self.repo}/pulls/{next(iter(self.pulls))}"
        found = [dict(c, pull_request_url=url) for c in self.review_comments_] + list(self.history_review_comments)
        return sorted(found, key=lambda c: -c["id"])[:limit]

    def recent_issue_comments(self, limit):
        found = [dict(c, issue_url=c.get("issue_url") or f"https://api.github.com/repos/{self.repo}/issues/{number}")
                 for number, comments in self.issue_comments_.items() for c in comments]
        return sorted(found, key=lambda c: -c["id"])[:limit]

    def create_review(self, number, head_sha, comments, body=""):
        if self.reject_review and len(comments) > 1:
            raise GitHubError(422, "Line could not be resolved")
        self.reviews.append({"head": head_sha, "comments": comments})
        for comment in comments:
            cid = next(_ids)
            self.review_comments_.append({"id": cid, "body": comment["body"], "path": comment["path"], "line": comment["line"],
                                          "user": {"login": "stratum[bot]"}, "reactions": {}})
            self.threads.append({"id": f"T{cid}", "resolved": False, "path": comment["path"],
                                 "comments": [{"id": cid, "body": comment["body"], "author": "stratum[bot]"}]})
        return {"id": next(_ids)}

    def edit_review_comment(self, comment_id, body):
        for comment in self.review_comments_:
            if comment["id"] == comment_id:
                comment["body"] = body
        for thread in self.threads:
            if thread["comments"][0]["id"] == comment_id:
                thread["comments"][0]["body"] = body
        self.edited.append((comment_id, body))

    def reply(self, number, comment_id, body):
        self.replies.append((number, comment_id, body))
        reply_id = next(_ids)
        for thread in self.threads:
            if thread["comments"][0]["id"] == comment_id:
                thread["comments"].append({"id": reply_id, "body": body, "author": "stratum[bot]"})
        self.review_comments_.append({"id": reply_id, "body": body, "in_reply_to_id": comment_id, "user": {"login": "stratum[bot]"},
                                      "created_at": self.now, "reactions": {}})
        return {"id": reply_id}

    def react(self, comment_id, content, *, review_comment=False):
        pass

    def reactions(self, comment_id):
        return self.reactions_.get(comment_id, [])

    def review_threads(self, number):
        return [dict(t, comments=list(t["comments"])) for t in self.threads]

    def resolve_thread(self, thread_id):
        if self.refuse_resolve:  # what GitHub does to an Actions token
            raise GitHubError(403, "Resource not accessible by integration")
        self.resolved.append(thread_id)
        for thread in self.threads:
            if thread["id"] == thread_id:
                thread["resolved"] = True

    def permission(self, login):
        return self.permissions.get(login, "read")

    def ci_state(self, sha, *, ignore):
        return self.ci

    def ci_state_at(self, sha, when, *, ignore):
        return self.ci

    def linked_issues(self, number):
        return self.linked

    def commit_emails(self, login):
        return set()

    def check_run(self, head_sha, name, conclusion, title, summary, text, annotations):
        self.checks.append({"head": head_sha, "name": name, "conclusion": conclusion, "title": title, "annotations": annotations})
        return {"id": 1}

    def ensure_label(self, name, color, description):
        if name not in self.created_labels:
            self.created_labels.append(name)

    def add_labels(self, number, labels):
        self.labels.setdefault(number, []).extend(labels)
        self.pulls[number]["labels"] = [{"name": n} for n in self.labels[number]]

    def remove_label(self, number, label):
        self.labels[number] = [n for n in self.labels.get(number, []) if n != label]
        self.pulls[number]["labels"] = [{"name": n} for n in self.labels[number]]

    def close(self, number):
        self.closed.append(number)

    def paginate(self, path, **params):
        return iter(self.issues)

    def create_issue(self, title, body):
        issue = {"number": next(_ids), "title": title, "body": body, "user": {"login": "stratum[bot]"}}
        self.issues.append(issue)
        return issue

    def edit_issue(self, number, body):
        for issue in self.issues:
            if issue["number"] == number:
                issue["body"] = body
