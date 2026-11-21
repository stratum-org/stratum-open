"""A fake GitLab REST API (v4): the calls Stratum makes, over HTTP, served
under a path prefix the way a self-hosted instance is
(https://git.example.com/gitlab/api/v4). Offset pagination with `Link`,
`x-next-page` and `x-total` headers, scriptable failures, and a record of
everything Stratum sent.

    with FakeGitLabServer() as gl:
        gl.add_mr(7, diff=DIFF)
        gl.fail("POST", r"/discussions$", 500, times=1)
        ... run Stratum with CI_API_V4_URL=gl.url ...
        gl.summary_notes(7), gl.threads(7), gl.posted_statuses(head)

A diff thread is placed the way GitLab's docs say it must be: the MR's three
diff SHAs, both paths, `new_line` alone for an added line and both numbers
for an unchanged one; anything else is a 400, as GitLab answers.

What's modelled comes from GitLab's REST docs (merge requests, notes,
discussions, commits, repositories, repository files, labels, pagination);
it hasn't been checked against a live GitLab.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from stratum_open.diff import parse_diff

GLPAT = "glpat-" + "F4keT0kenF4keT0ken12"


class FakeGitLabServer:
    def __init__(self, *, project_id: int = 42, path: str = "acme/widget", token: str = GLPAT, bot: str = "project_42_bot_5f3a",
                 prefix: str = "/gitlab/api/v4", default_branch: str = "main"):
        self.project_id, self.path, self.token, self.bot, self.prefix = project_id, path, token, bot, prefix
        self.default_branch = default_branch
        self._ids = iter(range(5000, 10_000_000))
        self._lock = threading.RLock()
        self.files: dict[tuple[str, str], str] = {}  # (path, ref) -> text
        self.mrs: dict[int, dict[str, Any]] = {}
        self.diffs: dict[int, list[dict[str, Any]]] = {}  # iid -> GitLab's diff items
        self.notes: dict[int, list[dict[str, Any]]] = {}  # iid -> every note, threads' included
        self.discussions: dict[int, list[dict[str, Any]]] = {}
        self.statuses: dict[str, list[dict[str, Any]]] = {}  # sha -> jobs and statuses, the CI's and Stratum's
        self.posted: list[dict[str, Any]] = []  # the statuses Stratum posted, as they stand
        self.status_history: list[tuple[int, str]] = []  # (status id, state) for each post
        self.labels: dict[str, dict[str, Any]] = {}
        self.closes: dict[int, list[int]] = {}
        self.mr_commits: dict[int, list[dict[str, Any]]] = {}  # iid -> its commits, as /merge_requests/:iid/commits lists them
        self.parents: dict[str, str] = {}  # head -> the head before it, for merge_base
        self.changed: dict[tuple[str, str], list[str]] = {}  # (from, to) -> paths, for compare
        self.users: dict[str, dict[str, Any]] = {}
        self.commits_by_author: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.per_page_cap = 100
        self.links = True  # False: no Link header, as behind a proxy that drops it; x-next-page still says
        self.has_diffs_endpoint = True  # False: a GitLab before 15.7, which only has /changes
        self._failures: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self) -> FakeGitLabServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def url(self) -> str:
        return self.origin + self.prefix

    def next_id(self) -> int:
        return next(self._ids)

    # ── building MRs ─────────────────────────────────────────────────────

    def add_mr(self, iid: int, *, diff: str, title: str = "Add a thing", description: str = "This adds a thing that the issue asked for, with tests.",
               author: str | None = "carol", head: str | None = None, draft: bool = False, state: str = "opened",
               labels: list[str] | None = None, source_branch: str = "feature") -> dict[str, Any]:
        head = head or ("a" * 40 if iid == 7 else f"{iid:040x}")
        mr = {
            "id": 900 + iid, "iid": iid, "project_id": self.project_id, "title": title, "description": description, "state": state,
            "draft": draft, "work_in_progress": draft, "source_branch": source_branch, "target_branch": self.default_branch,
            "author": {"id": 7, "username": author, "name": author.title()} if author else None,
            "labels": list(labels or []), "sha": head, "web_url": f"{self.origin}/{self.path}/-/merge_requests/{iid}",
            "diff_refs": {"base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": head},
            "head_pipeline": {"id": 77, "ref": f"refs/merge-requests/{iid}/head", "status": "running"},
        }
        self.mrs[iid] = mr
        self.diffs[iid] = gitlab_items(diff)
        self.notes.setdefault(iid, [])
        self.discussions.setdefault(iid, [])
        return mr

    def move_head(self, iid: int, sha: str, diff: str | None = None, changed: list[str] | None = None) -> None:
        mr = self.mrs[iid]
        old = mr["sha"]
        self.parents[sha] = old
        mr["sha"] = sha
        mr["diff_refs"] = {**mr["diff_refs"], "head_sha": sha}
        if diff is not None:
            self.diffs[iid] = gitlab_items(diff)
        self.changed[(old, sha)] = changed if changed is not None else [item["new_path"] for item in self.diffs[iid]]

    def set_policy(self, text: str | None, ref: str | None = None) -> None:
        key = (".stratum/policy.yml", ref or self.default_branch)
        if text is None:
            self.files.pop(key, None)
        else:
            self.files[key] = text

    def add_note(self, iid: int, body: str, *, author: str = "dave", system: bool = False) -> dict[str, Any]:
        note = self._note(body, author=author, system=system)
        self.notes[iid].append(note)
        self.discussions[iid].append({"id": f"{note['id']:040x}", "individual_note": True, "notes": [note]})
        return note

    def fail(self, method: str, path: str, status: int, *, body: Any = None, times: int = 1) -> None:
        """The next `times` calls of `method` to a path matching the regex `path` answer `status`."""
        self._failures.append({"method": method, "path": re.compile(path), "status": status, "body": body, "times": times})

    # ── what Stratum did ─────────────────────────────────────────────────

    def summary_notes(self, iid: int) -> list[dict[str, Any]]:
        return [n for n in self.notes[iid] if n["author"]["username"] == self.bot and "<!-- stratum:summary -->" in n["body"]]

    def threads(self, iid: int) -> list[dict[str, Any]]:
        return [d for d in self.discussions[iid] if d["notes"][0].get("type") == "DiffNote"]

    def posted_statuses(self, sha: str) -> list[dict[str, Any]]:
        return [s for s in self.posted if s["sha"] == sha]

    def requests_to(self, method: str, pattern: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and re.search(pattern, r["path"])]

    def posted_text(self) -> str:
        return "\n".join(json.dumps(r["body"]) for r in self.requests if r["method"] in ("POST", "PUT"))

    # ── internals ────────────────────────────────────────────────────────

    def _note(self, body: str, *, author: str, system: bool = False, kind: str | None = None, position: dict | None = None) -> dict[str, Any]:
        note: dict[str, Any] = {"id": self.next_id(), "type": kind, "body": body, "author": {"username": author}, "system": system,
                                "created_at": datetime.now(timezone.utc).isoformat(), "resolvable": kind == "DiffNote", "resolved": False}
        if position is not None:
            note["position"] = position
        return note


def gitlab_items(diff: str) -> list[dict[str, Any]]:
    """A git diff as GitLab's /diffs lists it: one item per file, the hunks
    without the header."""
    items = []
    for chunk in re.split(r"(?m)^(?=diff --git )", diff):
        if not chunk.startswith("diff --git "):
            continue
        [file] = parse_diff(chunk)
        at = chunk.find("\n@@")
        binary = "\nBinary files " in chunk
        items.append({
            "old_path": file.old_path or file.path, "new_path": file.path, "a_mode": "100644", "b_mode": "100644",
            "new_file": file.status == "added", "deleted_file": file.status == "deleted", "renamed_file": file.status == "renamed",
            "diff": "" if binary or at < 0 else chunk[at + 1:],
        })
    return items


class _Reply(Exception):
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None):
        self.status, self.body, self.headers = status, body, headers or {}


def _bad_position(message: str) -> _Reply:
    return _Reply(400, {"message": f"400 Bad request - Note {{:line_code=>[\"can't be blank\", \"must be a valid line code\"]}} ({message})"})


def _handler(fake: FakeGitLabServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _dispatch(self, method: str) -> None:
            raw, _, query_text = self.path.partition("?")
            query = urllib.parse.parse_qs(query_text)
            length = int(self.headers.get("Content-Length") or 0)
            body: Any = None
            if length:
                text = self.rfile.read(length).decode("utf-8")
                body = json.loads(text) if "json" in (self.headers.get("Content-Type") or "") else urllib.parse.parse_qs(text)
            with fake._lock:
                fake.requests.append({"method": method, "path": raw, "query": query, "body": body, "token": self.headers.get("PRIVATE-TOKEN")})
            try:
                if not raw.startswith(fake.prefix + "/"):
                    raise _Reply(404, {"message": "404 Not Found"})
                path = raw[len(fake.prefix):]
                for failure in fake._failures:
                    if failure["times"] > 0 and failure["method"] == method and failure["path"].search(path):
                        failure["times"] -= 1
                        raise _Reply(failure["status"], failure["body"] or {"message": f"{failure['status']} scripted"})
                if self.headers.get("PRIVATE-TOKEN") != fake.token:
                    raise _Reply(401, {"message": "401 Unauthorized"})
                segments = [urllib.parse.unquote(s) for s in path.strip("/").split("/")]
                with fake._lock:
                    status, data, headers = self._route(method, segments, {k: v[-1] for k, v in query.items()}, query, body)
            except _Reply as reply:
                status, data, headers = reply.status, reply.body, reply.headers
            except Exception as exc:  # a bug in the fake
                import traceback
                fake.errors.append(traceback.format_exc())
                status, data, headers = 500, {"message": f"fake bug: {exc}"}, {}
            payload = json.dumps(data).encode("utf-8") if data is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def do_DELETE(self) -> None:
            self._dispatch("DELETE")

        def do_PUT(self) -> None:
            self._dispatch("PUT")

        def _page(self, items: list, query: dict[str, str], segments: list[str]) -> tuple[int, Any, dict[str, str]]:
            per_page = min(int(query.get("per_page", 20)), fake.per_page_cap)
            page = int(query.get("page", 1))
            chunk = items[(page - 1) * per_page: page * per_page]
            more = page * per_page < len(items)
            headers = {"x-page": str(page), "x-per-page": str(per_page), "x-total": str(len(items)), "x-next-page": str(page + 1) if more else ""}
            if more and fake.links:
                rest = {**query, "page": str(page + 1), "per_page": str(per_page)}
                path = "/".join(urllib.parse.quote(s, safe="") for s in segments)
                headers["Link"] = f'<{fake.url}/{path}?{urllib.parse.urlencode(rest)}>; rel="next"'
            return 200, chunk, headers

        def _project(self, key: str) -> None:
            if key not in (str(fake.project_id), fake.path):
                raise _Reply(404, {"message": "404 Project Not Found"})

        def _mr(self, iid: str) -> dict[str, Any]:
            if not iid.isdigit() or int(iid) not in fake.mrs:
                raise _Reply(404, {"message": "404 Not found"})
            return fake.mrs[int(iid)]

        def _route(self, method: str, s: list[str], q: dict[str, str], multi: dict[str, list[str]], body: Any):
            if s == ["user"] and method == "GET":
                return 200, {"id": 99, "username": fake.bot, "bot": True}, {}
            if s == ["users"] and method == "GET":
                user = fake.users.get(q.get("username", ""))
                return 200, [user] if user else [], {}
            if len(s) < 2 or s[0] != "projects":
                raise _Reply(404, {"message": "404 Not Found"})
            self._project(s[1])
            rest = s[2:]
            if not rest and method == "GET":
                return 200, {"id": fake.project_id, "path_with_namespace": fake.path, "default_branch": fake.default_branch}, {}
            if rest[:1] == ["merge_requests"] and len(rest) >= 2:
                return self._merge_request(method, self._mr(rest[1]), rest[2:], q, body, s)
            if rest[:2] == ["repository", "files"] and len(rest) == 3 and method == "GET":
                text = fake.files.get((rest[2], q.get("ref", "")))
                if text is None:
                    raise _Reply(404, {"message": "404 File Not Found"})
                raw = text.encode("utf-8")
                return 200, {"file_path": rest[2], "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode()}, {}
            if rest == ["repository", "merge_base"] and method == "GET":
                refs = multi.get("refs[]", [])
                if len(refs) != 2:
                    raise _Reply(400, {"message": "Provide at least 2 refs"})
                base, head = refs
                seen, sha = set(), head
                while sha and sha not in seen:
                    if sha == base:
                        return 200, {"id": base}, {}
                    seen.add(sha)
                    sha = fake.parents.get(sha, "")
                return 200, {"id": "d" * 40}, {}  # diverged: a common ancestor that isn't `base`
            if rest == ["repository", "compare"] and method == "GET":
                paths = fake.changed.get((q.get("from", ""), q.get("to", "")), [])
                return 200, {"commits": [], "diffs": [{"old_path": p, "new_path": p} for p in paths], "compare_timeout": False}, {}
            if rest[:2] == ["repository", "commits"] and len(rest) == 4 and rest[3] == "statuses" and method == "GET":
                return self._page([x for x in fake.statuses.get(rest[2], []) if x.get("ref", q.get("ref")) == q.get("ref")], q, s)
            if rest == ["repository", "commits"] and method == "GET":
                return 200, fake.commits_by_author.get(q.get("author", ""), []), {}
            if rest[:1] == ["statuses"] and len(rest) == 2 and method == "POST":
                if body.get("state") not in ("pending", "running", "success", "failed", "canceled", "skipped"):
                    raise _Reply(400, {"message": "state does not have a valid value"})
                name = body.get("name") or "default"
                # One status per name in a pipeline: posting it again moves it to the new state, and GitLab refuses a move to the state it's in.
                status = next((x for x in fake.posted if (x["sha"], x["name"], x["pipeline_id"]) == (rest[1], name, body.get("pipeline_id"))), None)
                if status is not None and status["status"] == body["state"]:
                    raise _Reply(400, {"message": f"Cannot transition status via :{body['state']} from :{status['status']}"})
                if status is None:
                    status = {"id": fake.next_id(), "sha": rest[1], "name": name, "pipeline_id": body.get("pipeline_id"), "allow_failure": False}
                    fake.posted.append(status)
                status.update({"status": body["state"], "description": body.get("description"), "target_url": body.get("target_url"), "ref": body.get("ref")})
                fake.status_history.append((status["id"], body["state"]))
                return 201, dict(status), {}
            if rest[:1] == ["labels"]:
                if len(rest) == 2 and method == "GET":
                    if rest[1] not in fake.labels:
                        raise _Reply(404, {"message": "404 Label Not Found"})
                    return 200, fake.labels[rest[1]], {}
                if len(rest) == 1 and method == "POST":
                    if body["name"] in fake.labels:
                        raise _Reply(409, {"message": "Label already exists"})
                    if not re.fullmatch(r"#[0-9a-fA-F]{6}", body.get("color", "")):
                        raise _Reply(400, {"message": "color is invalid"})
                    fake.labels[body["name"]] = dict(body)
                    return 201, fake.labels[body["name"]], {}
            raise _Reply(404, {"message": "404 Not Found"})

        def _merge_request(self, method: str, mr: dict[str, Any], rest: list[str], q: dict[str, str], body: Any, s: list[str]):
            iid = mr["iid"]
            if not rest:
                if method == "GET":
                    return 200, mr, {}
                if method == "PUT":
                    for name in (body.get("add_labels") or "").split(","):
                        if name and name not in mr["labels"]:
                            mr["labels"].append(name)
                    for name in (body.get("remove_labels") or "").split(","):
                        if name in mr["labels"]:
                            mr["labels"].remove(name)
                    if body.get("state_event") == "close":
                        mr["state"] = "closed"
                    return 200, mr, {}
            if rest == ["diffs"] and method == "GET":
                if not fake.has_diffs_endpoint:
                    raise _Reply(404, {"error": "404 Not Found"})
                return self._page(fake.diffs[iid], q, s)
            if rest == ["changes"] and method == "GET":
                return 200, {**mr, "changes": fake.diffs[iid], "overflow": False}, {}
            if rest == ["closes_issues"] and method == "GET":
                return self._page([{"iid": n, "id": 100 + n} for n in fake.closes.get(iid, [])], q, s)
            if rest == ["commits"] and method == "GET":
                return self._page(fake.mr_commits.get(iid, []), q, s)
            if rest == ["notes"]:
                if method == "GET":
                    notes = list(fake.notes[iid])
                    if q.get("sort", "desc") == "desc":
                        notes.reverse()
                    return self._page(notes, q, s)
                if method == "POST":
                    return 201, fake.add_note(iid, body["body"], author=fake.bot), {}
            if len(rest) == 2 and rest[0] == "notes" and method == "PUT":
                note = next((n for n in fake.notes[iid] if str(n["id"]) == rest[1]), None)
                if note is None:
                    raise _Reply(404, {"message": "404 Not found"})
                if note["author"]["username"] != fake.bot:
                    raise _Reply(403, {"message": "403 Forbidden"})
                note["body"] = body["body"]
                return 200, note, {}
            if rest == ["discussions"]:
                if method == "GET":
                    return self._page(fake.discussions[iid], q, s)
                if method == "POST":
                    return 201, self._new_thread(mr, body), {}
            if len(rest) >= 2 and rest[0] == "discussions":
                discussion = next((d for d in fake.discussions[iid] if d["id"] == rest[1]), None)
                if discussion is None:
                    raise _Reply(404, {"message": "404 Not found"})
                if len(rest) == 2 and method == "PUT":
                    if not discussion["notes"][0]["resolvable"]:
                        raise _Reply(400, {"message": "not resolvable"})
                    for note in discussion["notes"]:
                        note["resolved"] = bool(body.get("resolved"))
                    return 200, discussion, {}
                if len(rest) == 4 and rest[2] == "notes" and method == "DELETE":
                    note = next((n for n in discussion["notes"] if str(n["id"]) == rest[3]), None)
                    if note is None:
                        raise _Reply(404, {"message": "404 Not found"})
                    discussion["notes"].remove(note)
                    fake.notes[iid].remove(note)
                    if not discussion["notes"]:
                        fake.discussions[iid].remove(discussion)
                    return 204, None, {}
                if len(rest) == 4 and rest[2] == "notes" and method == "PUT":
                    note = next((n for n in discussion["notes"] if str(n["id"]) == rest[3]), None)
                    if note is None:
                        raise _Reply(404, {"message": "404 Not found"})
                    note["body"] = body["body"]
                    return 200, note, {}
            raise _Reply(404, {"message": "404 Not Found"})

        def _new_thread(self, mr: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
            position = body.get("position")
            if not position:
                note = fake.add_note(mr["iid"], body["body"], author=fake.bot)
                return fake.discussions[mr["iid"]][-1] | {"notes": [note]}
            refs = mr["diff_refs"]
            if position.get("position_type") != "text":
                raise _bad_position("position_type")
            if any(position.get(k) != refs[k] for k in ("base_sha", "start_sha", "head_sha")):
                raise _bad_position("diff SHAs")
            if not position.get("old_path") or not position.get("new_path"):
                raise _bad_position("both paths are required")
            item = next((i for i in fake.diffs[mr["iid"]] if i["new_path"] == position["new_path"] and i["old_path"] == position["old_path"]), None)
            if item is None or not item["diff"]:
                raise _bad_position("no such file in the diff")
            [file] = parse_diff(f"diff --git a/{item['old_path']} b/{item['new_path']}\n--- a/{item['old_path']}\n+++ b/{item['new_path']}\n{item['diff']}")
            lines = [line for hunk in file.hunks for line in hunk.lines]
            new, old = position.get("new_line"), position.get("old_line")
            if new is not None and old is None:
                ok = any(line.kind == "+" and line.new == new for line in lines)
            elif new is not None and old is not None:
                ok = any(line.kind == " " and line.new == new and line.old == old for line in lines)
            else:
                ok = any(line.kind == "-" and line.old == old for line in lines)
            if not ok:
                raise _bad_position(f"line {old}/{new}")
            note = fake._note(body["body"], author=fake.bot, kind="DiffNote", position=dict(position))
            fake.notes[mr["iid"]].append(note)
            discussion = {"id": f"{note['id']:040x}", "individual_note": False, "notes": [note]}
            fake.discussions[mr["iid"]].append(discussion)
            return discussion

    return Handler
