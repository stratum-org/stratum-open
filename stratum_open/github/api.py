"""The GitHub REST and GraphQL calls Stratum makes, and nothing else.

One small client over `requests`, so every call that leaves the
maintainers' infrastructure is listed in this file.
"""

from __future__ import annotations

import base64
import re
import sys
import time
from itertools import islice
from typing import Any, Callable, Iterator

import requests

from stratum_open.platform import CI
from stratum_open.util import retry_after

API = "https://api.github.com"
TIMEOUT = 30  # seconds, per call
RETRIES = 2  # extra tries on 5xx and rate limits
WRITE = {"admin", "maintain", "write"}  # the permission() roles with write access
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_COMMENTS = "pageInfo{hasNextPage endCursor} nodes{databaseId body author{login __typename}}"  # a page of a PR's comments, in GraphQL
PULL_PAGES = (50, 10)  # open PRs a query asks for, with their comments; the next size after GitHub gives up on one


class GitHubError(Exception):
    def __init__(self, status: int, message: str, *, rate_limited: bool = False):
        super().__init__(f"GitHub answered {status}: {message}")
        self.status = status
        self.rate_limited = rate_limited  # a 403 or 429 for a rate limit, not for the token's rights


class GitHub:
    def __init__(self, token: str, repo: str, *, api: str = API, session: requests.Session | None = None,
                 log: Callable[[str], None] = lambda line: print(line, file=sys.stderr)):
        self.repo = repo  # owner/name
        self.log = log  # a read that failed without stopping the answer; stderr is the job's log
        self.api = api.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "stratum-open",
        })

    # ── plumbing ─────────────────────────────────────────────────────────

    def request(self, method: str, path: str, *, accept: str | None = None, ok404: bool = False, **kwargs) -> Any:
        response = self._send(method, path, accept=accept, **kwargs)
        if ok404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GitHubError(response.status_code, _message(response), rate_limited=_secondary_limit(response))
        if response.status_code == 204 or not response.content:
            return None
        if accept and "json" not in accept:
            return response.text
        return _json(response)

    def _send(self, method: str, url: str, *, accept: str | None = None, **kwargs) -> requests.Response:
        """One call with bounded retries: 5xx and rate limits are tried three
        times, waiting what `Retry-After` says (seconds or an HTTP date) up to
        a minute. A connection failure is a `requests.RequestException`, which
        the entry points turn into one line for the user."""
        url = url if url.startswith("http") else f"{self.api}{url}"
        headers = {"Accept": accept} if accept else {}
        for attempt in range(RETRIES + 1):
            response = self.session.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
            # A 500 may come after GitHub applied a write, so only a read is tried again on one.
            transient = response.status_code in (502, 503, 504) or (response.status_code == 500 and method == "GET")
            if (transient or _secondary_limit(response)) and attempt < RETRIES:
                time.sleep(min(60.0, retry_after(response.headers.get("Retry-After"), 2 * 3**attempt)))
                continue
            return response
        return response  # pragma: no cover

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    def paginate(self, path: str, **params) -> Iterator[dict]:
        url: str | None = path
        params = {"per_page": 100, **params}
        while url:
            response = self._send("GET", url, params=params)
            if response.status_code >= 400:
                raise GitHubError(response.status_code, _message(response), rate_limited=_secondary_limit(response))
            data = _json(response) if response.content else []
            if isinstance(data, list):
                yield from data
            elif isinstance(data, dict):
                yield from data.get("check_runs", data.get("workflow_runs", data.get("items", data.get("repositories", [])))) or []
            match = _NEXT.search(response.headers.get("Link", ""))
            url, params = (match.group(1), None) if match else (None, None)

    def graphql(self, query: str, **variables) -> dict:
        data = self.request("POST", "/graphql", json={"query": query, "variables": variables})
        if not isinstance(data, dict):
            raise GitHubError(200, "GraphQL answered with something that isn't an object")
        if data.get("errors"):
            raise GitHubError(200, "; ".join(e.get("message", "") for e in data["errors"] if isinstance(e, dict)))
        return data.get("data") or {}

    @property
    def _r(self) -> str:
        return f"/repos/{self.repo}"

    # ── repository ───────────────────────────────────────────────────────

    def repository(self) -> dict:
        return self.get(self._r)

    def file_text(self, path: str, ref: str, *, max_bytes: int | None = None) -> str | None:
        """A file's text at `ref`, or None when it doesn't exist there, or is
        bigger than `max_bytes`."""
        data = self.request("GET", f"{self._r}/contents/{requests.utils.quote(path)}", params={"ref": ref}, ok404=True)
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        if max_bytes is not None and int(data.get("size") or 0) > max_bytes:
            return None
        if data.get("encoding") == "base64" and data.get("content"):
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        if data.get("download_url"):  # files over 1 MB come without content
            response = self.session.get(data["download_url"], timeout=30)
            return response.text if response.ok else None
        return None

    def compare_files(self, base: str, head: str) -> list[str] | None:
        """Paths changed between two commits; None when they don't share
        history (a force-push), so the caller reviews everything again."""
        data = self.request("GET", f"{self._r}/compare/{base}...{head}", ok404=True)
        if not data or data.get("status") not in ("ahead", "identical"):
            return None
        files = data.get("files", [])
        if len(files) >= 300:  # GitHub lists at most 300 files here
            return None
        return [f["filename"] for f in files] + [f["previous_filename"] for f in files if f.get("previous_filename")]

    # ── pull requests ────────────────────────────────────────────────────

    def pull(self, number: int) -> dict:
        return self.get(f"{self._r}/pulls/{number}")

    def open_pulls(self) -> list[dict]:
        return list(self.paginate(f"{self._r}/pulls", state="open"))

    def open_pulls_with_comments(self, limit: int) -> list[dict]:
        """The newest `limit` open PRs, drafts included, newest first as
        `/pulls` lists them, each with its "comments" in `issue_comments`'
        shape. A PR has only what the triage queue reads: "number", "title",
        "draft" and "user". One GraphQL query per 50 PRs, and one more for
        each further 100 comments on a PR with more than 100: 1 point of
        GitHub's GraphQL rate limit each, and no REST calls.

        GitHub gives up on a query after 10 seconds, and 50 PRs with long
        comment threads can take longer. So a page it gives up on is asked
        again with fewer PRs (`PULL_PAGES`), and the pages after it too, from
        the same cursor: the order and the limit stay the same."""
        owner, name = self.repo.split("/", 1)
        pulls: list[dict] = []
        cursor, sizes = None, list(PULL_PAGES)
        while len(pulls) < limit:
            try:
                data = self.graphql(
                    "query($owner:String!,$name:String!,$first:Int!,$cursor:String){repository(owner:$owner,name:$name)"
                    "{pullRequests(states:OPEN,first:$first,after:$cursor,orderBy:{field:CREATED_AT,direction:DESC})"
                    "{pageInfo{hasNextPage endCursor} nodes{number title isDraft author{login __typename}"
                    "comments(first:100){" + _COMMENTS + "}}}}}",
                    owner=owner, name=name, first=min(sizes[0], limit - len(pulls)), cursor=cursor,
                )
            except GitHubError as exc:
                # A timeout is a GraphQL error (200) or the gateway's 502/504; a refusal or a rate limit isn't one.
                if len(sizes) == 1 or exc.rate_limited or exc.status not in (200, 500, 502, 503, 504):
                    raise
                self.log(f"GitHub didn't answer a query for {sizes[0]} open PRs and their comments ({exc}); "
                         f"asking for {sizes[1]} at a time.")
                sizes.pop(0)
                continue
            page = (data.get("repository") or {}).get("pullRequests") or {}
            for node in page.get("nodes") or []:
                if isinstance(node, dict) and isinstance(node.get("number"), int):
                    author = node.get("author")
                    pulls.append({"number": node["number"], "title": node.get("title") or "", "draft": bool(node.get("isDraft")),
                                  "user": {"login": _login(author)} if author else None,
                                  "comments": self._all_comments(node["number"], node.get("comments") or {})})
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("endCursor"):
                break
            cursor = info["endCursor"]
        return pulls[:limit]

    def _all_comments(self, number: int, page: dict) -> list[dict]:
        """A PR's comments, from the first page of them a query brought on."""
        owner, name = self.repo.split("/", 1)
        comments: list[dict] = []
        while True:
            comments += [{"id": c.get("databaseId"), "body": c.get("body") or "", "user": {"login": _login(c.get("author"))}}
                         for c in page.get("nodes") or [] if isinstance(c, dict)]
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("endCursor"):
                return comments
            data = self.graphql(
                "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name)"
                "{pullRequest(number:$number){comments(first:100,after:$cursor){" + _COMMENTS + "}}}}",
                owner=owner, name=name, number=number, cursor=info["endCursor"],
            )
            page = ((data.get("repository") or {}).get("pullRequest") or {}).get("comments") or {}

    def closed_pulls(self) -> Iterator[dict]:
        """Closed PRs, merged or not, newest first. A page is fetched only
        when the caller reads that far."""
        return self.paginate(f"{self._r}/pulls", state="closed", sort="created", direction="desc")

    def pull_diff(self, number: int) -> str:
        """The PR's diff ("" when it changes nothing). GitHub refuses one over
        its size limit; the file list's patches stand in for it then (each
        capped by GitHub too)."""
        try:
            return self.request("GET", f"{self._r}/pulls/{number}", accept="application/vnd.github.diff") or ""
        except GitHubError as exc:
            if exc.status not in (406, 422):
                raise
        parts = []
        for file in self.paginate(f"{self._r}/pulls/{number}/files"):
            old = file.get("previous_filename") or file["filename"]
            header = f"diff --git a/{old} b/{file['filename']}\n"
            if file["status"] == "renamed" and not file.get("patch") and not file.get("changes"):
                # A move with no edits has no patch, and isn't binary: write it as git does.
                parts.append(f"{header}similarity index 100%\nrename from {old}\nrename to {file['filename']}\n")
                continue
            if file["status"] == "added":
                header += f"new file mode 100644\n--- /dev/null\n+++ b/{file['filename']}\n"
            elif file["status"] == "removed":
                header += f"deleted file mode 100644\n--- a/{old}\n+++ /dev/null\n"
            else:
                header += f"--- a/{old}\n+++ b/{file['filename']}\n"
            parts.append(header + (file.get("patch") or "Binary files differ") + "\n")
        return "".join(parts)

    def pull_commits(self, number: int) -> list[dict]:
        """The PR's first 100 commits, oldest first: one request, whatever
        the PR's size (GitHub lists 250 at most anyway)."""
        return self.request("GET", f"{self._r}/pulls/{number}/commits", params={"per_page": 100}) or []

    def commit_emails(self, login: str) -> set[str]:
        """Emails on this login's recent commits here; GitHub links them to
        the account, git history only has the emails."""
        commits = self.request("GET", f"{self._r}/commits", params={"author": login, "per_page": 100}, ok404=True) or []
        return {((c.get("commit") or {}).get("author") or {}).get("email") or "" for c in commits} - {""}

    def linked_issues(self, number: int) -> list[int]:
        owner, name = self.repo.split("/", 1)
        data = self.graphql(
            "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number)"
            "{closingIssuesReferences(first:10){nodes{number}}}}}",
            owner=owner, name=name, number=number,
        )
        nodes = (((data.get("repository") or {}).get("pullRequest") or {}).get("closingIssuesReferences") or {}).get("nodes") or []
        return [node["number"] for node in nodes]

    def ci_state(self, sha: str, *, ignore: set[str]) -> CI:
        """What the commit's checks, statuses and `pull_request` workflow
        runs say: failed when one failed, held when a workflow waits for a
        maintainer's approval, pending while one runs, none when there are
        none, else passed. Stratum's own checks (`ignore`, by name) and
        workflow don't count."""
        runs = [r for r in self.paginate(f"{self._r}/commits/{sha}/check-runs") if r.get("name") not in ignore]
        status = self.get(f"{self._r}/commits/{sha}/status")
        return _ci_verdict(runs, [s.get("state") for s in status.get("statuses", [])], lambda: self._held(sha))

    def ci_state_at(self, sha: str, when: str, *, ignore: set[str]) -> CI:
        """`ci_state` as it stood at `when`, a time as GitHub writes one: of
        each check, the last run started by then, pending if it ended later;
        of each status, the last one reported by then; of each workflow, the
        last run created by then, held if it was still waiting for approval
        then. A job run again afterwards doesn't change the answer, nor does
        a workflow approved afterwards: its first attempt is still held."""
        # GitHub writes every time the same way (2026-01-31T12:00:00Z), so they compare as text.
        latest: dict[tuple, dict] = {}
        for run in self.paginate(f"{self._r}/commits/{sha}/check-runs", filter="all"):
            key = ((run.get("check_suite") or {}).get("id"), run.get("name"))  # a job run again keeps both
            started = run.get("started_at") or ""
            if run.get("name") not in ignore and started <= when and started >= (latest.get(key, {}).get("started_at") or ""):
                latest[key] = run
        runs = [run if run.get("status") == "completed" and (run.get("completed_at") or "") <= when
                else {**run, "status": "in_progress", "conclusion": None} for run in latest.values()]
        states: dict[str, str] = {}
        for status in self.paginate(f"{self._r}/commits/{sha}/statuses"):  # newest first
            if (status.get("created_at") or "") <= when:
                states.setdefault(status.get("context") or "", status.get("state") or "")
        return _ci_verdict(runs, list(states.values()), lambda: self._held(sha, when=when))

    def _held(self, sha: str, *, when: str | None = None) -> bool:
        """Whether a `pull_request` workflow on the commit waits for a
        maintainer to approve it, as GitHub has a first-time contributor's
        fork PR wait. Such a run makes no check runs, so only the workflow
        runs show it. Of each workflow the last run counts (created by
        `when`, if given). Stratum's own workflow runs on
        `pull_request_target`, which GitHub never holds, so the event leaves
        it out.

        Listing the runs takes `actions: read` on a private repo. Without it
        the answer is the checks' alone, as before, and a line says why. A
        rate limit is raised, as on any other read."""
        try:
            found = list(self.paginate(f"{self._r}/actions/runs", head_sha=sha, event="pull_request"))
        except GitHubError as exc:
            if exc.status not in (403, 404) or exc.rate_limited:
                raise
            self.log(f"Couldn't list the workflow runs (GitHub answered {exc.status}), so CI waiting for a maintainer's "
                     "approval can't be told from CI that passed. The workflow's token needs `actions: read`.")
            return False
        last: dict[object, dict] = {}
        for run in found:
            created = run.get("created_at") or ""
            key = run.get("workflow_id") or run.get("path") or run.get("name")
            if (when is None or created <= when) and (
                    key not in last or (created, run.get("id") or 0) >= (last[key].get("created_at") or "", last[key].get("id") or 0)):
                last[key] = run
        return any(self._attempt_at(run, when).get("conclusion") == "action_required" for run in last.values())

    def _attempt_at(self, run: dict, when: str | None) -> dict:
        """The attempt of a workflow run that stood at `when`. Approving a held
        run, or running it again, keeps its id and `created_at` and starts a
        new attempt, so a run that started again after `when` is read back
        through its earlier attempts, newest first: one request each, and
        usually just one, for the held first attempt."""
        attempt = run.get("run_attempt") or 1
        if when is None or attempt <= 1 or (run.get("run_started_at") or "") <= when:
            return run
        for number in range(attempt - 1, 1, -1):
            earlier = self.get(f"{self._r}/actions/runs/{run['id']}/attempts/{number}") or {}
            if (earlier.get("run_started_at") or "") <= when:
                return earlier
        return self.get(f"{self._r}/actions/runs/{run['id']}/attempts/1") or {}  # created by `when`, so it stood then

    # ── comments, reviews and threads ────────────────────────────────────

    def issue_comments(self, number: int) -> list[dict]:
        return list(self.paginate(f"{self._r}/issues/{number}/comments"))

    def comment(self, number: int, body: str) -> dict:
        return self.request("POST", f"{self._r}/issues/{number}/comments", json={"body": body})

    def edit_comment(self, comment_id: int, body: str) -> dict:
        return self.request("PATCH", f"{self._r}/issues/comments/{comment_id}", json={"body": body})

    def review_comments(self, number: int) -> list[dict]:
        return list(self.paginate(f"{self._r}/pulls/{number}/comments"))

    def review_comment(self, comment_id: int) -> dict | None:
        return self.request("GET", f"{self._r}/pulls/comments/{comment_id}", ok404=True)

    def recent_review_comments(self, limit: int) -> list[dict]:
        """The repo's latest inline comments on any PR, newest first: at most
        `limit`, in one call per 100."""
        return self._latest(f"{self._r}/pulls/comments", limit)

    def recent_issue_comments(self, limit: int) -> list[dict]:
        """The repo's latest comments on its PRs and issues, newest first: at
        most `limit`, in one call per 100."""
        return self._latest(f"{self._r}/issues/comments", limit)

    def _latest(self, path: str, limit: int) -> list[dict]:
        # A page is fetched only when the one before it ran out, so this stops at `limit`.
        return list(islice(self.paginate(path, per_page=min(100, max(1, limit)), sort="created", direction="desc"), max(0, limit)))

    def create_review(self, number: int, head_sha: str, comments: list[dict], body: str = "") -> dict:
        return self.request("POST", f"{self._r}/pulls/{number}/reviews", json={
            "commit_id": head_sha, "event": "COMMENT", "body": body, "comments": comments,
        })

    def edit_review_comment(self, comment_id: int, body: str) -> dict:
        return self.request("PATCH", f"{self._r}/pulls/comments/{comment_id}", json={"body": body})

    def delete_review_comment(self, comment_id: int) -> None:
        self.request("DELETE", f"{self._r}/pulls/comments/{comment_id}")

    def reply(self, number: int, comment_id: int, body: str) -> dict:
        return self.request("POST", f"{self._r}/pulls/{number}/comments/{comment_id}/replies", json={"body": body})

    def react(self, comment_id: int, content: str, *, review_comment: bool = False) -> None:
        kind = "pulls/comments" if review_comment else "issues/comments"
        self.request("POST", f"{self._r}/{kind}/{comment_id}/reactions", json={"content": content})

    def reactions(self, comment_id: int) -> list[dict]:
        return list(self.paginate(f"{self._r}/pulls/comments/{comment_id}/reactions"))

    def review_threads(self, number: int) -> list[dict]:
        """Every review thread with its first comment's database id, body and
        author, and whether it's resolved."""
        owner, name = self.repo.split("/", 1)
        threads, cursor = [], None
        while True:
            data = self.graphql(
                "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name)"
                "{pullRequest(number:$number){reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor}"
                "nodes{id isResolved path comments(first:50){nodes{databaseId body author{login __typename}}}}}}}}",
                owner=owner, name=name, number=number, cursor=cursor,
            )
            pull = (data.get("repository") or {}).get("pullRequest")
            if not pull:
                raise GitHubError(404, f"pull request {number} not found")
            page = pull.get("reviewThreads") or {}
            for node in page.get("nodes") or []:
                comments = [{"id": c.get("databaseId"), "body": c.get("body") or "", "author": _login(c.get("author"))}
                            for c in ((node.get("comments") or {}).get("nodes") or []) if isinstance(c, dict)]
                if comments:
                    threads.append({"id": node["id"], "resolved": bool(node.get("isResolved")), "path": node.get("path"), "comments": comments})
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage") or not info.get("endCursor"):
                return threads
            cursor = info["endCursor"]

    def resolve_thread(self, thread_id: str) -> None:
        self.graphql("mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id}}}", id=thread_id)

    def permission(self, login: str) -> str:
        """admin | maintain | write | triage | read | none."""
        data = self.request("GET", f"{self._r}/collaborators/{login}/permission", ok404=True)
        if not data:
            return "none"
        return (data.get("user") or {}).get("role_name") or data.get("permission") or "none"

    # ── checks, labels, issues ───────────────────────────────────────────

    def check_run(self, head_sha: str, name: str, conclusion: str, title: str, summary: str, text: str, annotations: list[dict],
                  run_id: int | None = None) -> dict:
        """GitHub takes 50 annotations per request; the rest follow in updates.
        `run_id`: complete that run, one `start_check_run` began, instead of
        posting a new one (a new one when it's gone)."""
        output = {"title": title[:255], "summary": summary[:65000], "text": text[:65000], "annotations": annotations[:50]}
        run = None
        if run_id:
            try:
                run = self.request("PATCH", f"{self._r}/check-runs/{run_id}", json={
                    "status": "completed", "conclusion": conclusion, "output": output,
                })
            except GitHubError as exc:
                if exc.status != 404:
                    raise
        if run is None:
            run = self.request("POST", f"{self._r}/check-runs", json={
                "name": name, "head_sha": head_sha, "status": "completed", "conclusion": conclusion, "output": output,
            })
        for start in range(50, len(annotations), 50):
            self.request("PATCH", f"{self._r}/check-runs/{run['id']}", json={
                "output": {**output, "annotations": annotations[start:start + 50]},
            })
        return run

    def start_check_run(self, head_sha: str, name: str, title: str, summary: str) -> dict:
        """A check run in progress, which `check_run(..., run_id=)` completes."""
        return self.request("POST", f"{self._r}/check-runs", json={
            "name": name, "head_sha": head_sha, "status": "in_progress", "output": {"title": title[:255], "summary": summary[:65000]},
        })

    def check_run_output(self, run_id: int) -> dict:
        return (self.request("GET", f"{self._r}/check-runs/{run_id}", ok404=True) or {}).get("output") or {}

    def ensure_label(self, name: str, color: str, description: str) -> None:
        if self.request("GET", f"{self._r}/labels/{requests.utils.quote(name, safe='')}", ok404=True) is None:
            try:
                self.request("POST", f"{self._r}/labels", json={"name": name, "color": color, "description": description[:100]})
            except GitHubError as exc:
                if exc.status != 422 or "already_exists" not in str(exc):  # two runs created it at once
                    raise

    def add_labels(self, number: int, labels: list[str]) -> None:
        if labels:
            self.request("POST", f"{self._r}/issues/{number}/labels", json={"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        self.request("DELETE", f"{self._r}/issues/{number}/labels/{requests.utils.quote(label, safe='')}", ok404=True)

    def close(self, number: int) -> None:
        self.request("PATCH", f"{self._r}/pulls/{number}", json={"state": "closed"})

    def create_issue(self, title: str, body: str) -> dict:
        return self.request("POST", f"{self._r}/issues", json={"title": title, "body": body})

    def edit_issue(self, number: int, body: str) -> dict:
        return self.request("PATCH", f"{self._r}/issues/{number}", json={"body": body})


def _ci_verdict(runs: list[dict], states: list[str | None], held: Callable[[], bool]) -> CI:
    """A failure counts first, whatever else is held or running, so `held`
    (which reads the workflow runs) is asked only when nothing failed: a
    failing PR costs no request, and a read that fails can't lose it."""
    if any(r.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required") for r in runs) or any(
        s in ("failure", "error") for s in states
    ):
        return CI.FAILED
    if held():
        return CI.HELD
    if not runs and not states:
        return CI.NONE
    if any(r.get("status") != "completed" for r in runs) or any(s == "pending" for s in states):
        return CI.PENDING
    return CI.PASSED


def _login(author: dict | None) -> str:
    """GraphQL names a bot `github-actions`, REST `github-actions[bot]`. Use
    the REST form, and only for real bots: a person can have the same login
    as an App's name, and must never pass for it."""
    if not author:
        return ""
    login = author.get("login", "")
    return f"{login}[bot]" if author.get("__typename") == "Bot" and not login.endswith("[bot]") else login


def _secondary_limit(response: requests.Response) -> bool:
    return response.status_code in (403, 429) and (
        "Retry-After" in response.headers or response.headers.get("X-RateLimit-Remaining") == "0"
    )


def _json(response: requests.Response) -> Any:
    """The body as JSON, or a GitHubError saying it wasn't: a proxy's HTML
    error page with a 200 must not surface as a traceback."""
    try:
        return response.json()
    except ValueError:
        raise GitHubError(response.status_code, f"the reply wasn't JSON: {response.text[:120]!r}") from None


def _message(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if not isinstance(data, dict):
        return str(data)[:200]
    errors = data.get("errors")
    detail = f" ({errors})" if errors else ""
    return f"{data.get('message', '')}{detail}"[:300]
