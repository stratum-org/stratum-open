"""The GitLab REST calls Stratum makes, and nothing else.

`GitLab` answers the calls in `stratum_open.platform` in GitHub's shapes, so
the runner and `github.publish` review a merge request as they review a PR.
Where GitLab differs, the difference is handled here:

- The summary is an MR note; inline comments are diff threads (discussions
  with a `position`: the MR's base, start and head SHAs, the paths, and the
  new line, plus the old line for an unchanged one).
- GitLab takes one thread per request, so a batch of comments is refused
  with a 422 before anything is sent, and `publish_inline` posts them one at
  a time, as it does when GitHub won't place one of a batch.
- A suggestion block is ```suggestion:-N+0, N being the lines above the
  commented one that it replaces (the comment sits on the range's last line).
- The check run is a commit status, in the job's own pipeline.
- Every body is cleaned before it's sent: a line that starts with `/` would
  be run as a quick action with the token's rights, so outside code blocks
  it's escaped, and a suggestion with such a line is left out. The hints that
  name `/stratum` commands, which GitLab CI can't read, are reworded.

GitLab's REST API: https://docs.gitlab.com/api/merge_requests/,
https://docs.gitlab.com/api/notes/, https://docs.gitlab.com/api/discussions/,
https://docs.gitlab.com/api/commits/ and https://docs.gitlab.com/api/rest/.
"""

from __future__ import annotations

import base64
import re
import time
from typing import Any, Iterator
from urllib.parse import quote

import requests

from stratum_open.diff import FileDiff, Line, parse_diff
from stratum_open.github import publish
from stratum_open.github.api import GitHubError
from stratum_open.platform import CI
from stratum_open.util import retry_after

TIMEOUT = 30  # seconds, per call
RETRIES = 2  # extra tries on 5xx and rate limits
PER_PAGE = 100  # GitLab's most per page
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
# The users behind project and group access tokens: project_123_bot_<hash>, group_45_bot_<hash>.
_TOKEN_BOT = re.compile(r"^(project|group)_\d+_bot(_\w+)?$")
# A line GitLab could read as a quick action: a slash and a word at its start.
_QUICK_ACTION = re.compile(r"^([ \t]*)/(?=[A-Za-z])")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_SUGGESTION = re.compile(r"^(`{3,})suggestion$", re.MULTILINE)
STATES = {"success": "success", "neutral": "success", "failure": "failed"}  # a check's conclusion as a commit status
PENDING = {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled", "waiting_for_callback"}
HINT = "Resolve this thread if it's wrong: Stratum won't post it again on this MR."
FOOTER_COMMANDS = " Comment `/stratum help` for commands."


class GitLabError(GitHubError):
    """A refused call. It's a GitHubError so that the runner's handling of
    one (log it, say so in the summary, go on with the next step) applies."""

    def __init__(self, status: int, message: str):
        Exception.__init__(self, f"GitLab answered {status}: {message}")
        self.status = status


class GitLab:
    platform = "GitLab"

    def __init__(self, token: str, project: str | int, *, api: str, session: requests.Session | None = None,
                 pipeline_id: int | None = None, target_url: str | None = None, statuses: bool = True):
        """`api` is the instance's /api/v4 URL (CI_API_V4_URL in a job), so a
        self-hosted GitLab works as gitlab.com does. `pipeline_id` and
        `target_url` put the commit status in the job's own pipeline and link
        it to the job; `statuses=False` posts none."""
        self.project = str(project)
        self.repo = self.project
        self.api = api.rstrip("/")
        self.pipeline_id = pipeline_id
        self.target_url = target_url
        self.statuses = statuses
        self.session = session or requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token, "User-Agent": "stratum-open"})
        self._mrs: dict[int, dict] = {}  # iid -> the MR as last read
        self._diffs: dict[tuple[int, str], str] = {}  # (iid, head) -> the diff as git writes it
        self._files: dict[tuple[int, str], dict[str, FileDiff]] = {}  # (iid, head) -> path -> its diff
        self._note_mr: dict[int, int] = {}  # note id -> MR iid
        self._note_thread: dict[int, str] = {}  # note id -> discussion id
        self._thread_mr: dict[str, int] = {}  # discussion id -> MR iid
        self._refs: dict[str, str] = {}  # head sha -> the ref its pipeline runs on
        self._started: dict[int, dict] = {}  # commit status id -> what `start_check_run` said

    # ── plumbing ─────────────────────────────────────────────────────────

    @property
    def _p(self) -> str:
        return f"/projects/{quote(self.project, safe='')}"

    def _mr(self, iid: int) -> str:
        return f"{self._p}/merge_requests/{iid}"

    def request(self, method: str, path: str, *, ok404: bool = False, **kwargs) -> Any:
        response = self._send(method, path, **kwargs)
        if ok404 and response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise GitLabError(response.status_code, _message(response))
        if response.status_code == 204 or not response.content:
            return None
        return _json(response)

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        """One call with bounded retries: 5xx (a 500 only on a read, since a
        write may have gone through) and 429, waiting what `Retry-After` says
        up to a minute. A connection failure is a `requests.RequestException`."""
        url = url if url.startswith("http") else f"{self.api}{url}"
        for attempt in range(RETRIES + 1):
            response = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
            transient = response.status_code in (429, 502, 503, 504) or (response.status_code == 500 and method == "GET")
            if transient and attempt < RETRIES:
                time.sleep(min(60.0, retry_after(response.headers.get("Retry-After"), 2 * 3**attempt)))
                continue
            return response
        return response  # pragma: no cover

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    def paginate(self, path: str, **params) -> Iterator[dict]:
        """Every page: the `Link` header's next URL, as GitLab asks, or
        `x-next-page` when a proxy dropped the link."""
        url: str | None = path
        query: dict | None = {"per_page": PER_PAGE, **params}
        while url:
            response = self._send("GET", url, params=query)
            if response.status_code >= 400:
                raise GitLabError(response.status_code, _message(response))
            data = _json(response) if response.content else []
            if isinstance(data, list):
                yield from (item for item in data if isinstance(item, dict))
            match = _NEXT.search(response.headers.get("Link", ""))
            following = (response.headers.get("x-next-page") or "").strip()
            if match:
                url, query = match.group(1), None
            elif following.isdigit() and isinstance(data, list) and data:
                url, query = path, {"per_page": PER_PAGE, **params, "page": int(following)}
            else:
                url = None

    # ── project and files ────────────────────────────────────────────────

    def repository(self) -> dict:
        project = self.get(self._p)
        return {"default_branch": project.get("default_branch"), "full_name": project.get("path_with_namespace")}

    def current_user(self) -> dict:
        return self.get("/user") or {}

    def file_text(self, path: str, ref: str, *, max_bytes: int | None = None) -> str | None:
        """A file's text at `ref`, or None when it isn't there, or is bigger
        than `max_bytes`."""
        data = self.request("GET", f"{self._p}/repository/files/{quote(path, safe='')}", params={"ref": ref}, ok404=True)
        if not isinstance(data, dict):
            return None
        if max_bytes is not None and int(data.get("size") or 0) > max_bytes:
            return None
        if data.get("encoding") == "base64" and data.get("content") is not None:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        if isinstance(data.get("content"), str):
            return data["content"]
        return None

    def compare_files(self, base: str, head: str) -> list[str] | None:
        """Paths that differ between two commits; None unless `base` is an
        ancestor of `head` (a force-push), or when GitLab gave up comparing,
        so the caller reviews everything again."""
        try:
            merge_base = self.request("GET", f"{self._p}/repository/merge_base", params=[("refs[]", base), ("refs[]", head)], ok404=True)
        except GitLabError as exc:
            if exc.status == 400:  # a commit GitLab doesn't have any more
                return None
            raise
        if not isinstance(merge_base, dict) or merge_base.get("id") != base:
            return None
        data = self.request("GET", f"{self._p}/repository/compare", params={"from": base, "to": head, "straight": "true"}, ok404=True)
        if not isinstance(data, dict) or data.get("compare_timeout"):
            return None
        diffs = [d for d in data.get("diffs") or [] if isinstance(d, dict)]
        return [d["new_path"] for d in diffs] + [d["old_path"] for d in diffs if d.get("old_path") and d["old_path"] != d["new_path"]]

    # ── merge requests ───────────────────────────────────────────────────

    def pull(self, number: int) -> dict:
        """The MR in a PR's shape. Its diff is read with it (once per head),
        since GitLab doesn't count added and removed lines on the MR."""
        mr = self.get(self._mr(number))
        self._mrs[number] = mr
        head = mr.get("sha") or (mr.get("diff_refs") or {}).get("head_sha") or ""
        self._refs[head] = ((mr.get("head_pipeline") or {}).get("ref")) or mr.get("source_branch") or ""
        state = {"opened": "open"}.get(mr.get("state"), mr.get("state"))
        files = self._parsed(number, head) if state == "open" else {}
        author = mr.get("author")
        login = (author or {}).get("username") or ""
        return {
            "number": number, "state": state, "draft": bool(mr.get("draft") or mr.get("work_in_progress")),
            "title": mr.get("title") or "", "body": mr.get("description") or "",
            "user": {"login": login, "type": "Bot" if _TOKEN_BOT.match(login) or (author or {}).get("bot") else "User"} if author else None,
            "head": {"sha": head, "ref": mr.get("source_branch") or ""},
            "base": {"ref": mr.get("target_branch") or "", "sha": (mr.get("diff_refs") or {}).get("base_sha") or ""},
            "labels": [{"name": label if isinstance(label, str) else (label or {}).get("name", "")} for label in mr.get("labels") or []],
            "additions": sum(f.additions for f in files.values()), "deletions": sum(f.deletions for f in files.values()),
            "html_url": mr.get("web_url") or "",
        }

    def pull_diff(self, number: int) -> str:
        mr = self._mrs.get(number) or self.get(self._mr(number))
        head = mr.get("sha") or ""
        if (number, head) not in self._diffs:
            self._parsed(number, head)
        return self._diffs[(number, head)]

    def _parsed(self, number: int, head: str) -> dict[str, FileDiff]:
        key = (number, head)
        if key not in self._files:
            text = "".join(_git_diff(item) for item in self._diff_items(number))
            self._diffs[key] = text
            self._files[key] = {f.path: f for f in parse_diff(text)}
        return self._files[key]

    def _diff_items(self, number: int) -> list[dict]:
        """The MR's files and their hunks: /diffs, or /changes on a GitLab
        older than 15.7, which has no /diffs."""
        try:
            return list(self.paginate(f"{self._mr(number)}/diffs"))
        except GitLabError as exc:
            if exc.status != 404:
                raise
        data = self.get(f"{self._mr(number)}/changes") or {}
        return [c for c in data.get("changes") or [] if isinstance(c, dict)]

    def pull_commits(self, number: int) -> list[dict]:
        """The MR's first 100 commits in GitHub's shape: one request."""
        commits = self.request("GET", f"{self._mr(number)}/commits", params={"per_page": PER_PAGE}) or []
        return [{"sha": c.get("id") or "", "commit": {"message": c.get("message") or "",
                                                      "author": {"name": c.get("author_name") or "", "email": c.get("author_email") or ""}}}
                for c in commits if isinstance(c, dict)]

    def linked_issues(self, number: int) -> list[int]:
        return [i["iid"] for i in self.paginate(f"{self._mr(number)}/closes_issues") if isinstance(i.get("iid"), int)]

    def ci_state(self, sha: str, *, ignore: set[str]) -> CI:
        """Failed when a job or status on the commit failed (one allowed to
        fail doesn't count), pending while one is, none when there are none,
        else passed. Stratum's own (`ignore`) don't count. Never held: GitLab
        doesn't hold a contributor's pipeline for approval as GitHub does."""
        params = {"ref": self._refs[sha]} if self._refs.get(sha) else {}
        states = [(s.get("status"), bool(s.get("allow_failure")))
                  for s in self.paginate(f"{self._p}/repository/commits/{sha}/statuses", **params) if s.get("name") not in ignore]
        if any(state in ("failed", "canceled") and not allowed for state, allowed in states):
            return CI.FAILED
        if not states:
            return CI.NONE
        if any(state in PENDING for state, _ in states):
            return CI.PENDING
        return CI.PASSED

    def commit_emails(self, login: str) -> set[str]:
        """Emails on recent commits by the user with this username. GitLab
        searches commits by author name or email, not username, so the
        user's name is looked up first."""
        users = self.request("GET", "/users", params={"username": login}, ok404=True) or []
        if not users or not isinstance(users[0], dict):
            return set()
        user = users[0]
        found = {user.get("public_email") or ""}
        if user.get("name"):
            commits = self.request("GET", f"{self._p}/repository/commits", params={"author": user["name"], "per_page": PER_PAGE}, ok404=True) or []
            found |= {c.get("author_email") or "" for c in commits if isinstance(c, dict)}
        return found - {""}

    # ── notes and threads ────────────────────────────────────────────────

    def issue_comments(self, number: int) -> list[dict]:
        """The MR's notes that aren't on a line, oldest first; system notes
        ("added 1 commit") left out."""
        found = []
        for note in self.paginate(f"{self._mr(number)}/notes", sort="asc", order_by="created_at"):
            if note.get("system") or note.get("type") == "DiffNote":
                continue
            self._note_mr[note["id"]] = number
            found.append(_note(note))
        return found

    def comment(self, number: int, body: str) -> dict:
        note = self.request("POST", f"{self._mr(number)}/notes", json={"body": gitlab_body(body)})
        self._note_mr[note["id"]] = number
        return _note(note)

    def edit_comment(self, comment_id: int, body: str) -> dict:
        if comment_id not in self._note_mr:
            raise GitLabError(404, f"note {comment_id} isn't one Stratum read on this MR")
        note = self.request("PUT", f"{self._mr(self._note_mr[comment_id])}/notes/{comment_id}", json={"body": gitlab_body(body)})
        return _note(note or {"id": comment_id, "body": body})

    def review_threads(self, number: int) -> list[dict]:
        """Every diff thread with its notes, and whether it's resolved."""
        threads = []
        for discussion in self.paginate(f"{self._mr(number)}/discussions"):
            notes = [n for n in discussion.get("notes") or [] if isinstance(n, dict) and not n.get("system")]
            if not notes or notes[0].get("type") != "DiffNote":
                continue
            self._thread_mr[discussion["id"]] = number
            for note in notes:
                self._note_mr[note["id"]] = number
                self._note_thread[note["id"]] = discussion["id"]
            position = notes[0].get("position") or {}
            threads.append({
                "id": discussion["id"], "resolved": bool(notes[0].get("resolved")),
                "path": position.get("new_path") or position.get("old_path"),
                "comments": [{"id": n["id"], "body": n.get("body") or "", "author": (n.get("author") or {}).get("username") or ""} for n in notes],
            })
        return threads

    def create_review(self, number: int, head_sha: str, comments: list[dict], body: str = "") -> dict:
        """One diff thread. A batch is refused before anything is sent, so
        `publish_inline` posts its comments one by one; a line GitLab won't
        place is a 422, as on GitHub."""
        if len(comments) != 1:
            raise GitLabError(422, "GitLab takes one diff thread per request")
        comment = comments[0]
        mr = self._mrs.get(number) or self.get(self._mr(number))
        refs = mr.get("diff_refs") or {}
        file = self._parsed(number, mr.get("sha") or "").get(comment["path"])
        line = _new_line(file, comment["line"]) if file else None
        if line is None or not all(refs.get(k) for k in ("base_sha", "start_sha", "head_sha")):
            raise GitLabError(422, "Line could not be resolved")
        position: dict[str, Any] = {
            "position_type": "text", "base_sha": refs["base_sha"], "start_sha": refs["start_sha"], "head_sha": refs["head_sha"],
            "old_path": file.old_path or file.path, "new_path": file.path, "new_line": comment["line"],
        }
        if line.kind == " ":  # an unchanged line takes both numbers; an added one only its new one
            position["old_line"] = line.old
        above = comment["line"] - comment.get("start_line", comment["line"])
        try:
            discussion = self.request("POST", f"{self._mr(number)}/discussions",
                                      json={"body": gitlab_body(comment["body"], suggestion_above=above), "position": position})
        except GitLabError as exc:
            if exc.status in (400, 422):  # "line_code can't be blank": a position GitLab won't place
                raise GitLabError(422, f"Line could not be resolved ({exc})") from None
            raise
        self._thread_mr[discussion["id"]] = number
        for note in discussion.get("notes") or []:
            self._note_mr[note["id"]] = number
            self._note_thread[note["id"]] = discussion["id"]
        return discussion

    def edit_review_comment(self, comment_id: int, body: str) -> dict:
        thread = self._note_thread.get(comment_id)
        if thread is None:
            raise GitLabError(404, f"note {comment_id} isn't in a thread Stratum read")
        return self.request("PUT", f"{self._mr(self._note_mr[comment_id])}/discussions/{thread}/notes/{comment_id}",
                            json={"body": gitlab_body(body)}) or {}

    def delete_review_comment(self, comment_id: int) -> None:
        """Delete a note of a diff thread; a thread left with no notes goes with it."""
        thread = self._note_thread.get(comment_id)
        if thread is None:
            raise GitLabError(404, f"note {comment_id} isn't in a thread Stratum read")
        self.request("DELETE", f"{self._mr(self._note_mr[comment_id])}/discussions/{thread}/notes/{comment_id}")
        self._note_thread.pop(comment_id, None)

    def resolve_thread(self, thread_id: str) -> None:
        if thread_id not in self._thread_mr:
            raise GitLabError(404, f"thread {thread_id} isn't one Stratum read")
        self.request("PUT", f"{self._mr(self._thread_mr[thread_id])}/discussions/{thread_id}", json={"resolved": True})

    # ── status, labels, state ────────────────────────────────────────────

    def check_run(self, head_sha: str, name: str, conclusion: str, title: str, summary: str, text: str, annotations: list[dict],
                  run_id: int | None = None) -> dict:
        """A commit status named like the check run: failed for a failure,
        success otherwise (GitLab has no neutral). The findings themselves are
        in the threads and the summary, so `annotations` aren't sent.
        `run_id` needs no handling: GitLab keeps one status per name in a
        pipeline, so this completes the one `start_check_run` set running."""
        return self._status(head_sha, STATES.get(conclusion, "success"), name, title)

    def start_check_run(self, head_sha: str, name: str, title: str, summary: str) -> dict:
        """A check run in progress: a `running` commit status of that name,
        which `check_run` then completes."""
        status = self._status(head_sha, "running", name, title)
        if isinstance(status.get("id"), int):
            self._started[status["id"]] = {"title": title, "summary": summary}
        return status

    def check_run_output(self, run_id: int) -> dict:
        """What a check run in progress says. GitLab has no call that reads
        one commit status by its id, so only one this client started is
        known; for any other, {} (and the caller words the title itself)."""
        return dict(self._started.get(run_id, {}))

    def _status(self, sha: str, state: str, name: str, title: str) -> dict:
        if not self.statuses:
            return {}
        data: dict[str, Any] = {"state": state, "name": name, "description": _short(title, 255)}
        if self.target_url:
            data["target_url"] = self.target_url[:255]
        if self.pipeline_id:
            data["pipeline_id"] = self.pipeline_id
        elif self._refs.get(sha):
            data["ref"] = self._refs[sha][:255]
        return self.request("POST", f"{self._p}/statuses/{sha}", json=data) or {}

    def ensure_label(self, name: str, color: str, description: str) -> None:
        if self.request("GET", f"{self._p}/labels/{quote(name, safe='')}", ok404=True) is None:
            try:
                self.request("POST", f"{self._p}/labels", json={"name": name, "color": f"#{color.lstrip('#')}", "description": description})
            except GitLabError as exc:
                if exc.status != 409:  # two runs created it at once
                    raise

    def add_labels(self, number: int, labels: list[str]) -> None:
        if labels:
            self.request("PUT", self._mr(number), json={"add_labels": ",".join(labels)})

    def remove_label(self, number: int, label: str) -> None:
        self.request("PUT", self._mr(number), json={"remove_labels": label})

    def close(self, number: int) -> None:
        self.request("PUT", self._mr(number), json={"state_event": "close"})


# ── what GitLab is sent ──────────────────────────────────────────────────


def gitlab_body(body: str, *, suggestion_above: int | None = None) -> str:
    """A comment as `publish` writes it, made right for GitLab: the
    suggestion block in GitLab's form (`suggestion_above`: the lines above
    the commented one it replaces) or left out when it holds a line GitLab
    could run as a quick action; the hints that name commands reworded; and
    every other line that starts with a slash escaped, outside code blocks.
    Running it twice changes nothing more."""
    body = body.replace(publish.HINT, HINT).replace(FOOTER_COMMANDS, "").replace(publish.RETRY_COMMAND, "")
    fences = list(_SUGGESTION.finditer(body))
    if fences:
        opening = max(fences, key=lambda m: len(m.group(1)))  # longer than any run of backticks in the code
        fence = opening.group(1)
        close = body.find(f"\n{fence}", opening.end())
        code = body[opening.end():close] if close >= 0 else ""
        if close < 0 or any(_QUICK_ACTION.match(line) for line in code.split("\n")):
            end = close + len(fence) + 1 if close >= 0 else len(body)
            body = body[:opening.start()].rstrip("\n") + body[end:]
        else:
            span = max(0, suggestion_above or 0)
            body = f"{body[:opening.start()]}{fence}suggestion:-{span}+0{body[opening.end():]}"
    return _escape_quick_actions(body)


def _escape_quick_actions(body: str) -> str:
    """`\\/` for a slash that starts a line outside a code block: Markdown
    shows a slash, and GitLab doesn't read it as a quick action."""
    lines, fence = [], ""
    for line in body.split("\n"):
        match = _FENCE.match(line)
        if fence:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not line.strip()[len(match.group(1)):]:
                fence = ""
        elif match:
            fence = match.group(1)
        else:
            line = _QUICK_ACTION.sub(lambda m: m.group(1) + "\\/", line)
        lines.append(line)
    return "\n".join(lines)


def _git_diff(item: dict) -> str:
    """One file of GitLab's diff list as `git diff` writes it. GitLab sends
    the hunks without the header; an empty diff that isn't a plain move is a
    binary file, or one too big for GitLab to show, and is written as binary."""
    old, new = item.get("old_path") or item.get("new_path") or "", item.get("new_path") or item.get("old_path") or ""
    text = item.get("diff") or ""
    header = f"diff --git a/{old} b/{new}\n"
    if item.get("renamed_file"):
        header += f"rename from {old}\nrename to {new}\n"
        if not text:
            return f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"
    if item.get("new_file"):
        header += f"new file mode {item.get('b_mode') or '100644'}\n--- /dev/null\n+++ b/{new}\n"
    elif item.get("deleted_file"):
        header += f"deleted file mode {item.get('a_mode') or '100644'}\n--- a/{old}\n+++ /dev/null\n"
    else:
        header += f"--- a/{old}\n+++ b/{new}\n"
    if not text.strip():
        text = "Binary files differ\n"
    return header + text + ("" if text.endswith("\n") else "\n")


def _new_line(file: FileDiff, number: int) -> Line | None:
    """The diff's line with this new-file number, added or unchanged."""
    for hunk in file.hunks:
        for line in hunk.lines:
            if line.new == number:
                return line
    return None


def _note(note: dict) -> dict:
    return {"id": note.get("id"), "body": note.get("body") or "", "user": {"login": (note.get("author") or {}).get("username") or ""},
            "created_at": note.get("created_at") or ""}


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        raise GitLabError(response.status_code, f"the reply wasn't JSON: {response.text[:120]!r}") from None


def _message(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if not isinstance(data, dict):
        return str(data)[:200]
    return str(data.get("message") or data.get("error") or data)[:300]
