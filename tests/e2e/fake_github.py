r"""A fake GitHub API: the REST and GraphQL calls Stratum makes, over HTTP, with
recorded PR payloads replayed from `fixtures/github/` or PRs built in the
test. Scriptable failures, real pagination, and a record of everything
Stratum posted back.

    with FakeGitHubServer() as gh:
        gh.add_pull(7, diff=DIFF, title="Add a thing")
        gh.fail("POST", r"/pulls/\d+/reviews", 502, times=1)
        gh.rate_limit(retry_after="2")
        ... run Stratum against gh.url with gh.token ...
        gh.check_runs_for(head)      # what was posted
        gh.inline_comments(7)
        gh.summary_comments(7)

Positions are checked the way GitHub checks them: an inline comment on a
line the diff doesn't show gets a 422 "Line could not be resolved".
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from stratum_open.diff import parse_diff

GHP = "ghp_" + "F4keT0kenF4keT0kenF4keT0kenF4keT0ken"  # 36 chars after the prefix, like a real one


class FakeGitHubServer:
    def __init__(self, *, repo: str = "acme/widget", token: str = GHP, bot: str = "stratum[bot]",
                 default_branch: str = "main", host: str = "127.0.0.1"):
        self.repo, self.token, self.bot, self.default_branch = repo, token, bot, default_branch
        self.owner, self.name = repo.split("/", 1)
        self._ids = iter(range(1000, 10_000_000))
        self._lock = threading.RLock()
        self.repo_json: dict[str, Any] = {"full_name": repo, "name": self.name, "owner": {"login": self.owner, "type": "User"},
                                          "default_branch": default_branch, "private": False, "fork": False}
        self.files: dict[tuple[str, str], str | bytes] = {}  # (path, ref) -> text (bytes = served as a download_url file)
        self.pulls: dict[int, dict[str, Any]] = {}
        self.diffs: dict[int, str | None] = {}  # None = GitHub refuses the .diff (406), serve /files instead
        self.pr_files: dict[int, list[dict[str, Any]]] = {}
        self.pr_commits: dict[int, list[dict[str, Any]]] = {}
        self.issue_comments: dict[int, list[dict[str, Any]]] = {}
        self.review_comments: list[dict[str, Any]] = []
        self.reviews: list[dict[str, Any]] = []
        self.resolved: set[int] = set()  # root comment ids of resolved threads
        self.check_runs: list[dict[str, Any]] = []
        self.ci_runs: dict[str, list[dict[str, Any]]] = {}  # sha -> other projects' check runs
        self.statuses: dict[str, dict[str, Any]] = {}  # sha -> combined status
        self.workflow_runs: dict[str, list[dict[str, Any]]] = {}  # sha -> workflow runs, each with its "event"
        self.labels: dict[str, dict[str, Any]] = {}
        self.issue_labels: dict[int, list[str]] = {}
        self.issues: list[dict[str, Any]] = []
        self.opened: list[tuple[str, int]] = []  # ("pull" | "issue", number) in the order they were opened, for /issues' order
        self.pulls_page_limit: int | None = None  # a GraphQL page of more open PRs than this gets a timeout ...
        self.pulls_page_timeout = 200  # ... as GitHub's GraphQL error (200), or as the gateway's 502 or 504
        self.reactions: dict[int, list[dict[str, Any]]] = {}
        self.permissions: dict[str, str] = {}
        self.compare: dict[tuple[str, str], list[dict[str, Any]] | None] = {}
        self.commits_by_author: dict[str, list[dict[str, Any]]] = {}
        self.closing_issues: dict[int, list[int]] = {}
        self.closed: list[int] = []
        self.refuse_resolve = False  # what an Actions token gets
        self.installations: list[dict[str, Any]] = [{"id": 1}]
        self.installation_repos: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []  # tracebacks of bugs in the fake itself
        self._failures: list[dict[str, Any]] = []
        self._rate_limited: dict[str, Any] | None = None
        self.per_page_cap = 100
        self.remaining = 4999  # what X-RateLimit-Remaining says
        self.on_request: Callable[[dict[str, Any]], None] | None = None  # a hook: move the head mid-run, etc.
        self._server = ThreadingHTTPServer((host, 0), _handler(self))
        self._server.daemon_threads = True
        self._server.block_on_close = False  # a keep-alive connection from requests.Session must not block stop()
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> FakeGitHubServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> FakeGitHubServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def next_id(self) -> int:
        return next(self._ids)

    # ── building PRs ─────────────────────────────────────────────────────

    def add_pull(self, number: int, *, diff: str | None, title: str = "Add a thing",
                 body: str | None = "This adds a thing that the issue asked for, with tests.", author: str | None = "carol",
                 head: str | None = None, base: str | None = None, draft: bool = False, state: str = "open", fork: bool = False,
                 fork_deleted: bool = False, labels: list[str] | None = None, additions: int | None = None,
                 deletions: int | None = None, commits: int = 1, files: list[dict[str, Any]] | None = None,
                 linked_issues: list[int] | None = None, merged: bool = False) -> dict[str, Any]:
        """A PR with this diff. `author=None` is a deleted account (GitHub
        sends `user: null`); `diff=None` makes the .diff endpoint answer 406 so
        Stratum falls back to the per-file patches in `files`."""
        head = head or ("a" * 40 if number == 7 else f"{number:040x}")
        parsed = parse_diff(diff or "")
        if additions is None:
            additions = sum(f.additions for f in parsed)
        if deletions is None:
            deletions = sum(f.deletions for f in parsed)
        head_repo = None if fork_deleted else {"full_name": f"{'forker' if fork else self.owner}/{self.name}", "fork": fork}
        pull = {
            "number": number, "state": state, "draft": draft, "title": title, "body": body, "merged": merged,
            "user": {"login": author, "type": "User"} if author else None,
            "head": {"sha": head, "ref": f"feature-{number}", "repo": head_repo},
            "base": {"sha": base or "b" * 40, "ref": self.default_branch, "repo": {"full_name": self.repo}},
            "additions": additions, "deletions": deletions, "changed_files": len(parsed), "commits": commits,
            "labels": [{"name": n} for n in labels or []], "html_url": f"https://github.com/{self.repo}/pull/{number}",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
            "closed_at": "2026-01-02T00:00:00Z" if state == "closed" else None, "merged_at": "2026-01-02T00:00:00Z" if merged else None,
        }
        with self._lock:
            if number not in self.pulls:
                self.opened.append(("pull", number))
            self.pulls[number] = pull
            self.diffs[number] = diff
            self.pr_files[number] = files if files is not None else _files_from_diff(parsed)
            self.pr_commits.setdefault(number, [{"sha": head, "commit": {"message": title, "author": {"email": f"{author or 'ghost'}@users.noreply.github.com"}}}])
            self.issue_comments.setdefault(number, [])
            self.issue_labels[number] = list(labels or [])
            if linked_issues is not None:
                self.closing_issues[number] = linked_issues
        return pull

    def set_policy(self, text: str | None, ref: str | None = None) -> None:
        key = (".stratum/policy.yml", ref or self.default_branch)
        if text is None:
            self.files.pop(key, None)
        else:
            self.files[key] = text

    def move_head(self, number: int, sha: str, diff: str | None = None, changed: list[str] | None = None) -> None:
        """A new push: the head moves; `changed` is what `compare` says changed since the old head."""
        with self._lock:
            old = self.pulls[number]["head"]["sha"]
            self.pulls[number]["head"]["sha"] = sha
            if diff is not None:
                self.diffs[number] = diff
                self.pr_files[number] = _files_from_diff(parse_diff(diff))
            if changed is not None:
                self.compare[(old, sha)] = [{"filename": path, "status": "modified"} for path in changed]

    # ── scripting failures ───────────────────────────────────────────────

    def fail(self, method: str, path: str, status: int, *, body: Any = None, headers: dict[str, str] | None = None,
             times: int = 1, text: str | None = None) -> None:
        """The next `times` requests matching `method` and the `path` regex
        get `status`. `times=0` means every time. `text` serves raw text
        (an HTML error page) instead of JSON."""
        with self._lock:
            self._failures.append({"method": method.upper(), "path": re.compile(path), "status": status,
                                   "body": body, "headers": headers or {}, "times": times, "text": text})

    def rate_limit(self, *, retry_after: str | None = "1", remaining: str = "0", times: int = 1, status: int = 403) -> None:
        headers = {"X-RateLimit-Remaining": remaining, "X-RateLimit-Reset": str(int(time.time()) + 60)}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        self.fail(".*", ".*", status, body={"message": "API rate limit exceeded for user ID 1.", "documentation_url": "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api"},
                  headers=headers, times=times)

    def _scripted(self, method: str, path: str) -> dict[str, Any] | None:
        with self._lock:
            for failure in list(self._failures):
                if re.fullmatch(failure["method"], method) and failure["path"].search(path):
                    if failure["times"] > 0:
                        failure["times"] -= 1
                        if failure["times"] == 0:
                            self._failures.remove(failure)
                    return failure
        return None

    # ── what Stratum posted ──────────────────────────────────────────────

    def check_runs_for(self, head: str) -> list[dict[str, Any]]:
        return [run for run in self.check_runs if run.get("head_sha") == head]

    def summary_comments(self, number: int) -> list[dict[str, Any]]:
        return [c for c in self.issue_comments.get(number, []) if "<!-- stratum:summary -->" in (c.get("body") or "")
                and (c.get("user") or {}).get("login", "").lower() == self.bot.lower()]

    def inline_comments(self, number: int) -> list[dict[str, Any]]:
        return [c for c in self.review_comments if c.get("pull_request_number") == number and not c.get("in_reply_to_id")]

    def labels_on(self, number: int) -> list[str]:
        return list(self.issue_labels.get(number, []))

    def posted_text(self) -> str:
        """Everything Stratum wrote to GitHub, for grepping."""
        parts = [c.get("body") or "" for comments in self.issue_comments.values() for c in comments]
        parts += [c.get("body") or "" for c in self.review_comments]
        parts += [json.dumps(run.get("output") or {}) for run in self.check_runs]
        parts += [i.get("body") or "" for i in self.issues]
        return "\n".join(parts)

    def requests_to(self, method: str, pattern: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method.upper() and re.search(pattern, r["path"])]


def _files_from_diff(files) -> list[dict[str, Any]]:
    """The /pulls/N/files shape, with a `patch` for each (none for binaries)."""
    out = []
    for file in files:
        status = {"added": "added", "deleted": "removed", "renamed": "renamed", "modified": "modified"}[file.status]
        entry: dict[str, Any] = {"filename": file.path, "status": status, "additions": file.additions, "deletions": file.deletions,
                                 "changes": file.additions + file.deletions, "sha": "0" * 40}
        if file.old_path:
            entry["previous_filename"] = file.old_path
        if not file.binary and file.hunks:
            entry["patch"] = _render_patch(file)
        out.append(entry)
    return out


def _render_patch(file) -> str:
    lines = []
    for hunk in file.hunks:
        old = sum(1 for l in hunk.lines if l.kind != "+")
        new = sum(1 for l in hunk.lines if l.kind != "-")
        lines.append(f"@@ -{hunk.old_start},{old} +{hunk.new_start},{new} @@ {hunk.header}".rstrip())
        lines += [f"{l.kind}{l.text}" for l in hunk.lines]
    return "\n".join(lines)


def _handler(fake: FakeGitHubServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "fake-github/1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        # ── plumbing ─────────────────────────────────────────────────────

        def _dispatch(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = None
            record = {"method": method, "path": path, "query": query, "body": body, "accept": self.headers.get("Accept", ""),
                      "auth": self.headers.get("Authorization", "")}
            with fake._lock:
                fake.requests.append(record)
            if fake.on_request:
                fake.on_request(record)
            try:
                scripted = fake._scripted(method, path)
                if scripted is not None:
                    if scripted["text"] is not None:
                        return self._send(scripted["status"], scripted["text"].encode("utf-8"), "text/html", scripted["headers"])
                    body_out = scripted["body"] if scripted["body"] is not None else {"message": f"fake failure {scripted['status']}"}
                    return self._json(scripted["status"], body_out, scripted["headers"])
                if not self._authorized(path):
                    return self._json(401, {"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"})
                handler = getattr(self, f"_{method.lower()}")
                result = handler(path, query, body)
                if result is None:
                    return self._json(404, {"message": "Not Found", "documentation_url": "https://docs.github.com/rest"})
                status, data, headers = result
                if isinstance(data, (bytes, str)) and headers.get("Content-Type", "").startswith("application/vnd.github.diff"):
                    return self._send(status, data.encode("utf-8") if isinstance(data, str) else data, headers["Content-Type"])
                return self._json(status, data, headers)
            except _Raw as raw:
                return self._send(raw.status, raw.body, "text/plain; charset=utf-8")
            except _Reply as reply:
                return self._json(reply.status, reply.body, reply.headers)
            except Exception:  # a bug in the fake: say so in the response, and remember it for the test
                import traceback
                text = traceback.format_exc()
                fake.errors.append(f"{method} {path}\n{text}")
                return self._json(500, {"message": "fake GitHub bug", "traceback": text})

        def _authorized(self, path: str) -> bool:
            auth = self.headers.get("Authorization", "")
            if path.startswith("/app") or path == "/installation/repositories":
                return auth.startswith("Bearer ")  # an App JWT, not checked further
            return auth == f"Bearer {fake.token}"

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_PATCH(self) -> None:
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def _json(self, status: int, data: Any, headers: dict[str, str] | None = None) -> None:
            if status == 204 or data is None and status in (200, 204):
                return self._send(204, b"", "application/json", headers)
            self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8", headers)

        def _send(self, status: int, data: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            given = {name.lower() for name in (headers or {})}
            if "x-ratelimit-remaining" not in given:
                self.send_header("X-RateLimit-Limit", "5000")
                self.send_header("X-RateLimit-Remaining", str(fake.remaining))
            for name, value in (headers or {}).items():
                if name.lower() != "content-type":
                    self.send_header(name, value)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _page(self, items: list, query: dict[str, str], path: str) -> tuple[int, Any, dict[str, str]]:
            per_page = min(int(query.get("per_page") or 30), fake.per_page_cap)
            page = int(query.get("page") or 1)
            start = (page - 1) * per_page
            chunk = items[start:start + per_page]
            headers: dict[str, str] = {}
            if start + per_page < len(items):
                next_query = {**query, "page": str(page + 1), "per_page": str(per_page)}
                headers["Link"] = f'<{fake.url}{path}?{urllib.parse.urlencode(next_query)}>; rel="next"'
            return 200, chunk, headers

        def _r(self) -> str:
            return re.escape(f"/repos/{fake.repo}")

        # ── GET ──────────────────────────────────────────────────────────

        def _get(self, path: str, query: dict[str, str], body: Any):
            r = self._r()
            if path == "/user":
                return 200, {"login": fake.bot.removesuffix("[bot]"), "type": "Bot" if fake.bot.endswith("[bot]") else "User"}, {}
            if path == "/app":
                return 200, {"slug": fake.bot.removesuffix("[bot]"), "id": 1}, {}
            if path == "/app/installations":
                return self._page(fake.installations, query, path)
            if path == "/installation/repositories":
                status, repos, headers = self._page(fake.installation_repos, query, path)
                return status, {"total_count": len(fake.installation_repos), "repositories": repos}, headers
            if path == "/rate_limit":
                return 200, {"resources": {"core": {"limit": 5000, "remaining": 4999}}}, {}
            if re.fullmatch(f"{r}", path):
                return 200, fake.repo_json, {}
            if m := re.fullmatch(f"{r}/contents/(.+)", path):
                file_path = urllib.parse.unquote(m.group(1))
                ref = query.get("ref") or fake.default_branch
                text = fake.files.get((file_path, ref))
                if text is None:
                    return None
                if isinstance(text, bytes):  # over 1 MB: GitHub leaves the content out
                    return 200, {"type": "file", "encoding": "none", "content": "", "size": len(text),
                                 "download_url": f"{fake.url}/raw/{urllib.parse.quote(file_path)}?ref={ref}"}, {}
                return 200, {"type": "file", "encoding": "base64", "path": file_path, "size": len(text),
                             "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}, {}
            if m := re.fullmatch(r"/raw/(.+)", path):
                text = fake.files.get((urllib.parse.unquote(m.group(1)), query.get("ref") or fake.default_branch))
                if text is None:
                    return None
                raise _Raw(200, text if isinstance(text, bytes) else text.encode("utf-8"))
            if m := re.fullmatch(f"{r}/compare/([^.]+)\\.\\.\\.(.+)", path):
                key = (m.group(1), m.group(2))
                if key not in fake.compare:
                    return None
                files = fake.compare[key]
                if files is None:
                    return 200, {"status": "diverged", "files": []}, {}
                return 200, {"status": "identical" if not files else "ahead", "files": files}, {}
            if re.fullmatch(f"{r}/pulls", path):
                state = query.get("state", "open")
                pulls = [p for p in fake.pulls.values() if state == "all" or p["state"] == state]
                if query.get("direction") == "desc":  # newest first; here a higher number is a newer PR
                    pulls.sort(key=lambda p: -p["number"])
                listed = [{k: v for k, v in p.items() if k not in ("additions", "deletions", "changed_files", "commits", "merged")} for p in pulls]
                return self._page(listed, query, path)
            if m := re.fullmatch(f"{r}/pulls/(\\d+)", path):
                number = int(m.group(1))
                pull = fake.pulls.get(number)
                if pull is None:
                    return None
                if "diff" in self.headers.get("Accept", ""):
                    diff = fake.diffs.get(number)
                    if diff is None:
                        raise _Reply(406, {"message": "Sorry, the diff exceeded the maximum number of files (300)."})
                    return 200, diff, {"Content-Type": "application/vnd.github.diff; charset=utf-8"}
                return 200, pull, {}
            if m := re.fullmatch(f"{r}/pulls/(\\d+)/files", path):
                number = int(m.group(1))
                if number not in fake.pulls:
                    return None
                return self._page(fake.pr_files.get(number, []), query, path)
            if m := re.fullmatch(f"{r}/pulls/(\\d+)/commits", path):
                return self._page(fake.pr_commits.get(int(m.group(1)), []), query, path)
            if m := re.fullmatch(f"{r}/pulls/(\\d+)/comments", path):
                number = int(m.group(1))
                comments = [self._review_comment_json(c) for c in fake.review_comments if c.get("pull_request_number") == number]
                return self._page(comments, query, path)
            if m := re.fullmatch(f"{r}/pulls/comments/(\\d+)", path):
                comment = self._find_review_comment(int(m.group(1)))
                return (200, self._review_comment_json(comment), {}) if comment else None
            if m := re.fullmatch(f"{r}/pulls/comments/(\\d+)/reactions", path):
                return self._page(fake.reactions.get(int(m.group(1)), []), query, path)
            if re.fullmatch(f"{r}/issues", path):
                state = query.get("state", "open")
                issues = [i for i in fake.issues if state == "all" or i.get("state", "open") == state]
                issues += [{"number": p["number"], "title": p["title"], "state": p["state"], "user": p.get("user"), "pull_request": {"url": "x"}}
                           for p in fake.pulls.values() if state == "all" or p["state"] == state]
                # Newest first, as GitHub lists them by default; an issue a test appended itself counts as the oldest.
                rank = {key: n for n, key in enumerate(fake.opened)}
                issues.sort(key=lambda i: rank.get(("pull" if "pull_request" in i else "issue", i["number"]), -1),
                            reverse=query.get("direction", "desc") == "desc")
                return self._page(issues, query, path)
            if m := re.fullmatch(f"{r}/issues/(\\d+)/comments", path):
                number = int(m.group(1))
                if number not in fake.pulls and not any(i["number"] == number for i in fake.issues):
                    return None
                return self._page(fake.issue_comments.get(number, []), query, path)
            if m := re.fullmatch(f"{r}/collaborators/([^/]+)/permission", path):
                login = urllib.parse.unquote(m.group(1))
                role = fake.permissions.get(login)
                if role is None:
                    return None if login == "ghost" else (200, {"permission": "read", "user": {"login": login, "role_name": "read"}}, {})
                return 200, {"permission": role, "user": {"login": login, "role_name": role}}, {}
            if re.fullmatch(f"{r}/commits", path):
                return self._page(fake.commits_by_author.get(query.get("author", ""), []), query, path)
            if m := re.fullmatch(f"{r}/check-runs/(\\d+)", path):
                run = next((run for run in fake.check_runs if run["id"] == int(m.group(1))), None)
                return (200, run, {}) if run else None
            if m := re.fullmatch(f"{r}/commits/([^/]+)/check-runs", path):
                runs = fake.ci_runs.get(m.group(1), []) + [run for run in fake.check_runs if run.get("head_sha") == m.group(1)]
                return 200, {"total_count": len(runs), "check_runs": runs}, {}
            if re.fullmatch(f"{r}/actions/runs", path):
                runs = [run for run in fake.workflow_runs.get(query.get("head_sha", ""), [])
                        if "event" not in query or run.get("event") == query["event"]]
                status, chunk, headers = self._page(runs, query, path)
                return status, {"total_count": len(runs), "workflow_runs": chunk}, headers
            if m := re.fullmatch(f"{r}/commits/([^/]+)/status", path):
                return 200, fake.statuses.get(m.group(1), {"state": "pending", "statuses": []}), {}
            if m := re.fullmatch(f"{r}/commits/([^/]+)/statuses", path):  # every status reported, newest first
                return self._page(fake.statuses.get(m.group(1), {}).get("statuses", []), query, path)
            if m := re.fullmatch(f"{r}/labels/(.+)", path):
                label = fake.labels.get(urllib.parse.unquote(m.group(1)))
                return (200, label, {}) if label else None
            if re.fullmatch(f"{r}/labels", path):
                return self._page(list(fake.labels.values()), query, path)
            return None

        # ── POST ─────────────────────────────────────────────────────────

        def _post(self, path: str, query: dict[str, str], body: Any):
            r = self._r()
            if path == "/graphql":
                return 200, self._graphql(body or {}), {}
            if m := re.fullmatch(r"/app/installations/(\d+)/access_tokens", path):
                expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
                return 201, {"token": fake.token, "expires_at": expires}, {}
            if m := re.fullmatch(f"{r}/issues/(\\d+)/comments", path):
                number = int(m.group(1))
                if number not in fake.pulls and not any(i["number"] == number for i in fake.issues):
                    return None
                comment = {"id": fake.next_id(), "body": (body or {}).get("body", ""), "user": {"login": fake.bot, "type": "Bot"},
                           "created_at": _now(), "updated_at": _now()}
                with fake._lock:
                    fake.issue_comments.setdefault(number, []).append(comment)
                return 201, comment, {}
            if m := re.fullmatch(f"{r}/pulls/(\\d+)/reviews", path):
                return self._create_review(int(m.group(1)), body or {})
            if m := re.fullmatch(f"{r}/pulls/(\\d+)/comments/(\\d+)/replies", path):
                number, root_id = int(m.group(1)), int(m.group(2))
                root = self._find_review_comment(root_id)
                if root is None:
                    return None
                comment = {"id": fake.next_id(), "body": (body or {}).get("body", ""), "user": {"login": fake.bot, "type": "Bot"},
                           "pull_request_number": number, "in_reply_to_id": root_id, "path": root.get("path"), "line": root.get("line"),
                           "created_at": _now(), "reactions": {}}
                with fake._lock:
                    fake.review_comments.append(comment)
                return 201, self._review_comment_json(comment), {}
            if m := re.fullmatch(f"{r}/(?:issues|pulls)/comments/(\\d+)/reactions", path):
                cid = int(m.group(1))
                reaction = {"id": fake.next_id(), "content": (body or {}).get("content"), "user": {"login": fake.bot}}
                with fake._lock:
                    fake.reactions.setdefault(cid, []).append(reaction)
                return 201, reaction, {}
            if re.fullmatch(f"{r}/check-runs", path):
                run = {**(body or {}), "id": fake.next_id()}
                if run.get("status") == "completed" and not run.get("conclusion"):
                    raise _Reply(422, {"message": "Validation Failed", "errors": [{"resource": "CheckRun", "code": "missing_field", "field": "conclusion"}]})
                for annotation in (run.get("output") or {}).get("annotations") or []:
                    if not annotation.get("path") or annotation.get("start_line") is None or annotation.get("end_line") is None:
                        raise _Reply(422, {"message": "Validation Failed", "errors": [{"resource": "CheckRun", "code": "invalid", "field": "annotations"}]})
                    if annotation["end_line"] < annotation["start_line"]:
                        raise _Reply(422, {"message": "Validation Failed", "errors": [{"resource": "CheckRun", "code": "invalid", "field": "annotations", "message": "end_line must be >= start_line"}]})
                if len((run.get("output") or {}).get("annotations") or []) > 50:
                    raise _Reply(422, {"message": "Validation Failed", "errors": [{"resource": "CheckRun", "code": "custom", "field": "annotations", "message": "Only 50 annotations are permitted per request"}]})
                with fake._lock:
                    fake.check_runs.append(run)
                return 201, run, {}
            if re.fullmatch(f"{r}/labels", path):
                name = (body or {}).get("name", "")
                if name in fake.labels:
                    raise _Reply(422, {"message": "Validation Failed", "errors": [{"resource": "Label", "code": "already_exists", "field": "name"}]})
                label = {"name": name, "color": (body or {}).get("color", "ededed"), "description": (body or {}).get("description", "")}
                with fake._lock:
                    fake.labels[name] = label
                return 201, label, {}
            if m := re.fullmatch(f"{r}/issues/(\\d+)/labels", path):
                number = int(m.group(1))
                names = (body or {}).get("labels") or []
                with fake._lock:
                    for name in names:
                        if name not in fake.labels:
                            fake.labels[name] = {"name": name, "color": "ededed", "description": ""}
                        if name not in fake.issue_labels.setdefault(number, []):
                            fake.issue_labels[number].append(name)
                    if number in fake.pulls:
                        fake.pulls[number]["labels"] = [{"name": n} for n in fake.issue_labels[number]]
                return 200, [fake.labels[n] for n in fake.issue_labels[number]], {}
            if re.fullmatch(f"{r}/issues", path):
                issue = {"number": fake.next_id(), "title": (body or {}).get("title", ""), "body": (body or {}).get("body", ""),
                         "state": "open", "user": {"login": fake.bot, "type": "Bot"}}
                with fake._lock:
                    fake.issues.append(issue)
                    fake.opened.append(("issue", issue["number"]))
                return 201, issue, {}
            return None

        def _create_review(self, number: int, body: dict[str, Any]):
            pull = fake.pulls.get(number)
            if pull is None:
                return None
            if body.get("commit_id") and body["commit_id"] != pull["head"]["sha"]:
                raise _Reply(422, {"message": "Validation Failed", "errors": ["commit_id is not part of the pull request"]})
            files = {f.path: f for f in parse_diff(fake.diffs.get(number) or "")}
            for comment in body.get("comments") or []:
                file = files.get(comment.get("path"))
                line = comment.get("line")
                start = comment.get("start_line", line)
                if file is None or line is None or not file.can_comment(start, line):
                    raise _Reply(422, {"message": "Validation Failed", "errors": [{"resource": "PullRequestReviewComment", "code": "custom",
                                                                               "field": "line", "message": "Line could not be resolved"}]})
                if len(comment.get("body") or "") > 65536:
                    raise _Reply(422, {"message": "Validation Failed", "errors": [{"resource": "PullRequestReviewComment", "code": "custom",
                                                                               "field": "body", "message": "Body is too long (maximum is 65536 characters)"}]})
            review_id = fake.next_id()
            with fake._lock:
                fake.reviews.append({"id": review_id, "pull_request_number": number, "body": body.get("body", ""), "commit_id": body.get("commit_id")})
                for comment in body.get("comments") or []:
                    fake.review_comments.append({
                        "id": fake.next_id(), "pull_request_review_id": review_id, "pull_request_number": number,
                        "body": comment.get("body", ""), "path": comment["path"], "line": comment.get("line"),
                        "start_line": comment.get("start_line"), "side": comment.get("side", "RIGHT"),
                        "user": {"login": fake.bot, "type": "Bot"}, "created_at": _now(), "reactions": {},
                    })
            return 200, {"id": review_id, "state": "COMMENTED"}, {}

        # ── PATCH / DELETE ───────────────────────────────────────────────

        def _patch(self, path: str, query: dict[str, str], body: Any):
            r = self._r()
            if m := re.fullmatch(f"{r}/issues/comments/(\\d+)", path):
                cid = int(m.group(1))
                for comments in fake.issue_comments.values():
                    for comment in comments:
                        if comment["id"] == cid:
                            comment["body"] = (body or {}).get("body", comment["body"])
                            comment["updated_at"] = _now()
                            return 200, comment, {}
                return None
            if m := re.fullmatch(f"{r}/pulls/comments/(\\d+)", path):
                comment = self._find_review_comment(int(m.group(1)))
                if comment is None:
                    return None
                comment["body"] = (body or {}).get("body", comment["body"])
                return 200, self._review_comment_json(comment), {}
            if m := re.fullmatch(f"{r}/check-runs/(\\d+)", path):
                run_id = int(m.group(1))
                for run in fake.check_runs:
                    if run["id"] == run_id:
                        output = {**(run.get("output") or {}), **((body or {}).get("output") or {})}
                        run.update({k: v for k, v in (body or {}).items() if k != "output"})
                        run["output"] = output
                        return 200, run, {}
                return None
            if m := re.fullmatch(f"{r}/pulls/(\\d+)", path):
                pull = fake.pulls.get(int(m.group(1)))
                if pull is None:
                    return None
                if (body or {}).get("state") == "closed":
                    pull["state"] = "closed"
                    fake.closed.append(pull["number"])
                return 200, pull, {}
            if m := re.fullmatch(f"{r}/issues/(\\d+)", path):
                number = int(m.group(1))
                for issue in fake.issues:
                    if issue["number"] == number:
                        issue.update({k: v for k, v in (body or {}).items() if k in ("title", "body", "state")})
                        return 200, issue, {}
                return None
            return None

        def _delete(self, path: str, query: dict[str, str], body: Any):
            r = self._r()
            if m := re.fullmatch(f"{r}/pulls/comments/(\\d+)", path):
                comment = self._find_review_comment(int(m.group(1)))
                if comment is None:
                    return None
                with fake._lock:
                    fake.review_comments.remove(comment)
                return 204, None, {}
            if m := re.fullmatch(f"{r}/issues/(\\d+)/labels/(.+)", path):
                number, name = int(m.group(1)), urllib.parse.unquote(m.group(2))
                with fake._lock:
                    if name not in fake.issue_labels.get(number, []):
                        return None
                    fake.issue_labels[number].remove(name)
                    if number in fake.pulls:
                        fake.pulls[number]["labels"] = [{"name": n} for n in fake.issue_labels[number]]
                return 200, [fake.labels.get(n, {"name": n}) for n in fake.issue_labels[number]], {}
            return None

        def _put(self, path: str, query: dict[str, str], body: Any):
            return None

        # ── GraphQL ──────────────────────────────────────────────────────

        def _graphql(self, body: dict[str, Any]) -> dict[str, Any]:
            query = body.get("query", "")
            variables = body.get("variables") or {}
            if "closingIssuesReferences" in query:
                number = variables.get("number")
                if number not in fake.pulls:
                    return {"data": {"repository": {"pullRequest": None}},
                            "errors": [{"type": "NOT_FOUND", "message": f"Could not resolve to a PullRequest with the number of {number}."}]}
                nodes = [{"number": n} for n in fake.closing_issues.get(number, [])]
                return {"data": {"repository": {"pullRequest": {"closingIssuesReferences": {"nodes": nodes}}}}}
            if "reviewThreads" in query:
                number = variables.get("number")
                if number not in fake.pulls:
                    return {"data": {"repository": {"pullRequest": None}},
                            "errors": [{"type": "NOT_FOUND", "message": f"Could not resolve to a PullRequest with the number of {number}."}]}
                threads = self._threads(number)
                cursor = variables.get("cursor")
                start = int(cursor) if cursor else 0
                page = threads[start:start + 100]
                has_next = start + 100 < len(threads)
                return {"data": {"repository": {"pullRequest": {"reviewThreads": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": str(start + 100) if has_next else None},
                    "nodes": page}}}}}
            if "resolveReviewThread" in query:
                if fake.refuse_resolve:
                    return {"data": None, "errors": [{"type": "FORBIDDEN", "message": "Resource not accessible by integration"}]}
                thread_id = variables.get("id", "")
                root_id = int(thread_id[1:]) if thread_id.startswith("T") and thread_id[1:].isdigit() else -1
                with fake._lock:
                    fake.resolved.add(root_id)
                return {"data": {"resolveReviewThread": {"thread": {"id": thread_id}}}}
            if "pullRequests(" in query:  # the open PRs with their comments, for the triage queue
                first = int(variables.get("first") or 0)
                if not 1 <= first <= 100:
                    return {"data": None, "errors": [{"message": "Values of `first` and `last` must be within 1-100."}]}
                if fake.pulls_page_limit is not None and first > fake.pulls_page_limit:  # GitHub gave up after 10 seconds
                    if fake.pulls_page_timeout != 200:
                        raise _Reply(fake.pulls_page_timeout, {"message": "We couldn't respond to your request in time. Sorry about that."})
                    return {"data": None, "errors": [{"message": "Something went wrong while executing your query. "
                                                                 "This may be the result of a timeout, or it could be a GitHub bug."}]}
                pulls = [p for p in fake.pulls.values() if p["state"] == "open"]
                if "direction:DESC" in query:  # newest first; here a higher number is a newer PR
                    pulls.sort(key=lambda p: -p["number"])
                start = int(variables.get("cursor") or 0)
                has_next = start + first < len(pulls)
                nodes = [{"number": p["number"], "title": p["title"], "isDraft": p["draft"], "author": self._gql_author(p.get("user")),
                          "comments": self._gql_comments(p["number"], query, None)} for p in pulls[start:start + first]]
                return {"data": {"repository": {"pullRequests": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": str(start + first) if has_next else None}, "nodes": nodes}}}}
            if "pullRequest(" in query and "comments(" in query:  # a later page of one PR's comments
                number = variables.get("number")
                if number not in fake.pulls:
                    return {"data": {"repository": {"pullRequest": None}},
                            "errors": [{"type": "NOT_FOUND", "message": f"Could not resolve to a PullRequest with the number of {number}."}]}
                return {"data": {"repository": {"pullRequest": {"comments": self._gql_comments(number, query, variables.get("cursor"))}}}}
            return {"data": None, "errors": [{"message": "unsupported query in the fake"}]}

        def _gql_comments(self, number: int, query: str, cursor: str | None) -> dict[str, Any]:
            first = int(re.search(r"comments\(first:(\d+)", query).group(1))
            comments = fake.issue_comments.get(number, [])
            start = int(cursor or 0)
            has_next = start + first < len(comments)
            return {"pageInfo": {"hasNextPage": has_next, "endCursor": str(start + first) if has_next else None},
                    "nodes": [self._gql_comment(c) for c in comments[start:start + first]]}

        def _threads(self, number: int) -> list[dict[str, Any]]:
            roots = [c for c in fake.review_comments if c.get("pull_request_number") == number and not c.get("in_reply_to_id")]
            threads = []
            for root in roots:
                replies = [c for c in fake.review_comments if c.get("in_reply_to_id") == root["id"]]
                comments = [self._gql_comment(c) for c in [root, *replies]]
                threads.append({"id": f"T{root['id']}", "isResolved": root["id"] in fake.resolved, "path": root.get("path"),
                                "comments": {"nodes": comments}})
            return threads

        def _gql_comment(self, comment: dict[str, Any]) -> dict[str, Any]:
            return {"databaseId": comment["id"], "body": comment.get("body") or "", "author": self._gql_author(comment.get("user"))}

        def _gql_author(self, user: dict[str, Any] | None) -> dict[str, Any] | None:
            if not user:
                return None  # a deleted account
            login = user.get("login", "")
            bot = login.endswith("[bot]") or user.get("type") == "Bot"
            return {"login": login.removesuffix("[bot]"), "__typename": "Bot" if bot else "User"}

        # ── helpers ──────────────────────────────────────────────────────

        def _find_review_comment(self, cid: int) -> dict[str, Any] | None:
            return next((c for c in fake.review_comments if c["id"] == cid), None)

        def _review_comment_json(self, comment: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in comment.items() if k != "pull_request_number"} | {"pull_request_review_id": comment.get("pull_request_review_id")}

    return Handler


class _Reply(Exception):
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None):
        self.status, self.body, self.headers = status, body, headers or {}


class _Raw(_Reply):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
