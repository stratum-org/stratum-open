"""The GitHub App: one small webhook server the maintainers run, for projects
that would rather install an App than add a workflow to each repo.

It's the only mode that stores anything (findings, 👍/👎 votes, a short job
log; see DATA.md), and the only one that keeps a clone of each repo's default
branch between runs. Run it with `stratum-open serve`, or any WSGI server
pointed at `stratum_open.app:application`.

Settings, all from the environment:
  STRATUM_APP_ID, STRATUM_APP_PRIVATE_KEY (or STRATUM_APP_PRIVATE_KEY_FILE),
  STRATUM_WEBHOOK_SECRET, STRATUM_DB (default stratum.db), STRATUM_WORKDIR
  (default ./repos), STRATUM_WORKERS (default 2), and the model's
  STRATUM_MODEL_ENDPOINT / STRATUM_MODEL_NAME / STRATUM_MODEL_API_KEY.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import OrderedDict
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

import requests

from stratum_open.checks.dependencies import PackageLookup
from stratum_open.github.api import API, GitHub, GitHubError
from stratum_open.github.commands import LABEL_COMMANDS, handle, parse
from stratum_open.github.feedback import collect_votes
from stratum_open.model import model_from_policy
from stratum_open.policy import REVIEW_EVENTS, Policy
from stratum_open.runner import Context, checkout_ref, load_context_policy, review_pull, skip_reason
from stratum_open.storage import SQLiteStorage, Storage
from stratum_open.track_record import describe_subprocess_error
from stratum_open.triage_queue import update_queue
from stratum_open.util import redact

MAX_BODY = 25 * 1024 * 1024  # GitHub's own webhook payload limit
DELIVERY_TTL = 3600.0  # seconds a delivery ID is remembered
MAX_DELIVERIES = 5000
JOBS_DAYS = 14
PURGE_EVERY = 24 * 3600
# A repo's triage queue is rebuilt once no job has come for it for QUEUE_WAIT seconds and none is
# waiting or running, so a burst of reviews costs one rebuild; on a repo that's never that quiet,
# QUEUE_MAX_WAIT after the first job it waits for. The rebuild loop looks every QUEUE_TICK seconds.
QUEUE_WAIT = 60.0
QUEUE_MAX_WAIT = 600.0
QUEUE_TICK = 5.0
QUEUE_ONLY = ("closed", "converted_to_draft")  # PR events that only change the queue: the PR leaves it (the Action's QUEUE_ONLY)
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


class AppAuth:
    """App JWTs and short-lived installation tokens."""

    def __init__(self, app_id: str, private_key: str, *, api: str = API, session: requests.Session | None = None):
        self.app_id, self.private_key, self.api = app_id, private_key, api.rstrip("/")
        self.session = session or requests.Session()
        self._tokens: dict[int, tuple[str, float]] = {}
        self._login: str | None = None
        self._lock = threading.Lock()

    def jwt(self) -> str:
        import jwt  # PyJWT, the `app` extra

        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key, algorithm="RS256")

    def _app_get(self, path: str) -> Any:
        response = self.session.get(f"{self.api}{path}", headers=self._headers(), timeout=30)
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.jwt()}", "Accept": "application/vnd.github+json", "User-Agent": "stratum-open"}

    def token(self, installation_id: int) -> str:
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached and cached[1] - time.time() > 300:
                return cached[0]
            response = self.session.post(f"{self.api}/app/installations/{installation_id}/access_tokens", headers=self._headers(), timeout=30)
            response.raise_for_status()
            data = response.json()
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            self._tokens[installation_id] = (data["token"], expires)
            return data["token"]

    def bot_login(self) -> str:
        if self._login is None:
            self._login = f"{self._app_get('/app')['slug']}[bot]"
        return self._login

    def installations(self) -> list[dict]:
        """Every installation, page by page, with the client's retries."""
        return list(GitHub(self.jwt(), "", api=self.api).paginate("/app/installations"))


class Server:
    def __init__(
        self, auth: AppAuth, secret: str, storage: Storage, workdir: str, *,
        env: Mapping[str, str] = os.environ, workers: int = 2, log: Callable[[str], None] = print,
    ):
        self.auth, self.secret, self.storage, self.workdir, self.env, self.log = auth, secret.encode(), storage, workdir, env, log
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._pending: dict[tuple, tuple[str, dict]] = {}
        self._busy: set[tuple] = set()
        self._forgotten: set[str] = set()  # uninstalled repos, until a webhook comes for one again
        self._repo_locks: dict[str, threading.Lock] = {}
        self._deliveries: OrderedDict[str, float] = OrderedDict()
        self._rebuilds: dict[str, dict[str, Any]] = {}  # repo -> the triage queue rebuild it waits for
        self.clock: Callable[[], float] = time.monotonic
        self._lock = threading.Lock()
        os.makedirs(workdir, exist_ok=True)
        for _ in range(workers):
            threading.Thread(target=self._work, daemon=True).start()
        threading.Thread(target=self._purge_loop, daemon=True).start()
        threading.Thread(target=self._rebuild_loop, daemon=True).start()

    # ── WSGI ─────────────────────────────────────────────────────────────

    def __call__(self, environ: dict, start_response) -> list[bytes]:
        path, method = environ.get("PATH_INFO", ""), environ.get("REQUEST_METHOD", "")
        if path == "/healthz":
            return _respond(start_response, "200 OK", {"ok": True})
        if path != "/webhook" or method != "POST":
            return _respond(start_response, "404 Not Found", {"error": "not found"})
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            return _respond(start_response, "400 Bad Request", {"error": "bad Content-Length"})
        if length <= 0 or length > MAX_BODY:
            return _respond(start_response, "413 Payload Too Large", {"error": "bad size"})
        body = environ["wsgi.input"].read(length)
        if not self.verify(body, environ.get("HTTP_X_HUB_SIGNATURE_256", "")):
            return _respond(start_response, "401 Unauthorized", {"error": "bad signature"})
        try:
            payload = json.loads(body)
        except ValueError:
            return _respond(start_response, "400 Bad Request", {"error": "not JSON"})
        if not isinstance(payload, dict):
            return _respond(start_response, "400 Bad Request", {"error": "not a JSON object"})
        delivery = environ.get("HTTP_X_GITHUB_DELIVERY", "")
        if self.seen(delivery):
            return _respond(start_response, "202 Accepted", {"queued": False, "duplicate": True})
        try:
            accepted = self.dispatch(environ.get("HTTP_X_GITHUB_EVENT", ""), payload)
        except (KeyError, TypeError, AttributeError) as exc:  # a field GitHub always sends is missing, or of another type
            self.unsee(delivery)
            self.log(f"webhook: a {environ.get('HTTP_X_GITHUB_EVENT', '')!r} payload isn't shaped as expected: {type(exc).__name__}: {exc}")
            return _respond(start_response, "400 Bad Request", {"error": "unexpected payload shape"})
        except Exception:
            self.unsee(delivery)
            raise
        return _respond(start_response, "202 Accepted", {"queued": accepted})

    def verify(self, body: bytes, signature: str) -> bool:
        expected = "sha256=" + hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        return bool(self.secret) and hmac.compare_digest(expected, signature)

    def seen(self, delivery: str) -> bool:
        """Whether this delivery came in the last hour. GitHub redelivers a
        webhook it thinks timed out, and each repeat would start another
        review (or answer a command twice). Checked after the signature, so
        a forged ID can't hide a real delivery."""
        if not delivery:
            return False
        now = time.monotonic()
        with self._lock:
            while self._deliveries and now - next(iter(self._deliveries.values())) > DELIVERY_TTL:
                self._deliveries.popitem(last=False)
            if delivery in self._deliveries:
                return True
            self._deliveries[delivery] = now
            if len(self._deliveries) > MAX_DELIVERIES:
                self._deliveries.popitem(last=False)
            return False

    def unsee(self, delivery: str) -> None:
        """Forget a delivery that wasn't handled, so a redelivery of it runs."""
        with self._lock:
            self._deliveries.pop(delivery, None)

    # ── events ───────────────────────────────────────────────────────────

    def dispatch(self, event: str, payload: dict) -> bool:
        installation = _dict(payload.get("installation")).get("id")
        if event == "installation" and payload.get("action") == "deleted":
            for repo in payload.get("repositories") or []:
                if _dict(repo).get("full_name"):
                    self.forget(repo["full_name"])
            return False
        if event == "installation_repositories" and payload.get("action") == "removed":
            for repo in payload.get("repositories_removed") or []:
                if _dict(repo).get("full_name"):
                    self.forget(repo["full_name"])
            return False
        repo = _dict(payload.get("repository")).get("full_name")
        if not installation or not repo:
            return False
        # Drafts, review.events and the review.skip_* settings are the repo's
        # own policy, so they're checked when the job runs and the policy's loaded.
        pull = _dict(payload.get("pull_request"))
        if event == "pull_request" and payload.get("action") in REVIEW_EVENTS and pull.get("number"):
            self.enqueue((repo, pull["number"], "review"), event, payload)
            return True
        if event == "pull_request" and payload.get("action") in QUEUE_ONLY and pull.get("number"):
            # No review, only the triage queue, which the repo's policy is read for when it's rebuilt.
            with self._lock:
                self._forgotten.discard(repo.lower())  # a webhook for it: installed again
            self.rebuild_soon(installation, repo, _dict(payload.get("repository")).get("default_branch") or "main")
            return True
        if event in ("issue_comment", "pull_request_review_comment") and payload.get("action") == "created":
            comment = _dict(payload.get("comment"))
            issue = _dict(payload.get("issue"))
            number = (issue or pull).get("number")
            # The parser the job runs: a comment without a command (Stratum's own, with a
            # "Reply `/stratum ignore`" footer, or a link to stratum-org/stratum-open) has no job to do.
            # Nor has a bot's, or one on an issue that isn't a PR, which handle() would drop
            # once a worker had cloned the repo.
            if _dict(comment.get("user")).get("type") == "Bot" or (event == "issue_comment" and "pull_request" not in issue):
                return False
            if parse(comment.get("body") or "") and comment.get("id") and number:
                self.enqueue((repo, number, "comment", comment["id"]), event, payload)
                return True
        return False

    def forget(self, repo: str) -> None:
        """Uninstalled: every stored row and the clone go at once. A job still
        queued for the repo is dropped, and what a running one writes is
        deleted when it ends."""
        with self._lock:
            self._forgotten.add(repo.lower())
            for key in [k for k in self._pending if k[0].lower() == repo.lower()]:
                del self._pending[key]
            for name in [n for n in self._rebuilds if n.lower() == repo.lower()]:
                del self._rebuilds[name]
        deleted = self._delete(repo)
        self.log(f"{repo}: uninstalled; deleted {deleted} stored row(s) and its clone")

    def _delete(self, repo: str) -> int:
        deleted = self.storage.delete_repo(repo)
        shutil.rmtree(self._clone_dir(repo), ignore_errors=True)
        return deleted

    def enqueue(self, key: tuple, event: str, payload: dict) -> None:
        """A newer push replaces a review still waiting for the same PR."""
        with self._lock:
            self._forgotten.discard(key[0].lower())  # a webhook for it: installed again
            queued = key in self._pending
            self._pending[key] = (event, payload)
            if not queued:
                self._queue.put(key)

    def _work(self) -> None:
        while True:
            key = self._queue.get()
            with self._lock:
                if key not in self._pending:  # dropped when the App was uninstalled
                    continue
                if (key[0], key[1]) in {(k[0], k[1]) for k in self._busy}:
                    self._queue.put(key)  # one job per PR at a time
                    retry = True
                else:
                    retry = False
                    event, payload = self._pending.pop(key)
                    self._busy.add(key)
            if retry:
                time.sleep(1)
                continue
            try:
                self.run(event, payload)
            except Exception as exc:
                self.log(redact(f"{key[0]}#{key[1]}: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"))
                try:
                    self.storage.record_job(key[0], key[1], event, "error", f"{type(exc).__name__}: {exc}"[:500])
                except Exception as failed:  # the job log is best effort: a broken database must not kill the worker
                    self.log(redact(f"{key[0]}#{key[1]}: couldn't record the failed job either: {type(failed).__name__}: {failed}"))
            finally:
                with self._lock:
                    forgotten = key[0].lower() in self._forgotten
                if forgotten:  # uninstalled while the job ran: what it wrote goes too
                    try:
                        self._delete(key[0])
                    except Exception as failed:
                        self.log(redact(f"{key[0]}#{key[1]}: couldn't delete the uninstalled repo's rows: {type(failed).__name__}: {failed}"))
                with self._lock:
                    self._busy.discard(key)

    def run(self, event: str, payload: dict) -> None:
        repo = payload["repository"]["full_name"]
        ctx = self.context(payload["installation"]["id"], repo, payload["repository"].get("default_branch") or "main")
        if event == "pull_request":
            number = payload["pull_request"]["number"]
            reason = skip_reason(payload["pull_request"], payload.get("action", ""), ctx.policy)
            if reason:
                self.storage.record_job(repo, number, "review", "skipped", reason)
                self._queue_after(ctx, payload)  # the queue lists PRs that aren't reviewed too
                return
            try:
                collect_votes(ctx.gh, number, ctx.bot, self.storage)
            except GitHubError as exc:
                self.log(f"{repo}#{number}: couldn't read reactions: {exc}")
            outcome = review_pull(ctx, number)
            self.storage.record_job(repo, number, "review", outcome.status, outcome.note)
            self._queue_after(ctx, payload)
        else:
            number = (payload.get("issue") or payload.get("pull_request") or {}).get("number")
            done = handle(ctx, event, payload)
            self.storage.record_job(repo, number, "command", done or "none")
            if done in LABEL_COMMANDS.values():  # `/stratum review` or `ignore` ran, and may have changed the PR's label
                self._queue_after(ctx, payload)
        self.storage.purge(findings_days=ctx.policy.data.findings_retention_days, jobs_days=JOBS_DAYS, repo=repo)

    def context(self, installation_id: int, repo: str, default_branch: str) -> Context:
        token = self.auth.token(installation_id)
        gh = GitHub(token, repo, api=self.auth.api)
        policy = load_context_policy(gh, default_branch)
        checkout = self.checkout(repo, token, default_branch, policy.track_record.window_days)
        return Context(
            gh=gh, policy=policy, model=model_from_policy(policy, self.env), bot=self.auth.bot_login(),
            storage=self.storage, checkout=checkout, history_ref=checkout_ref(checkout, default_branch) if checkout else "HEAD",
            lookup=PackageLookup(), feedback=True, log=self.log,
        )

    # ── the triage queue ─────────────────────────────────────────────────

    def _queue_after(self, ctx: Context, payload: dict) -> None:
        """A job that can have changed the repo's triage queue: rebuild it soon, if the repo keeps one."""
        if ctx.policy.triage.queue:
            repository = payload["repository"]
            self.rebuild_soon(payload["installation"]["id"], repository["full_name"], repository.get("default_branch") or "main")

    def rebuild_soon(self, installation: int, repo: str, default_branch: str) -> None:
        """Rebuild the repo's triage queue when its wait is over (QUEUE_WAIT).
        A job that ends meanwhile makes it wait longer, so one rebuild reads
        every job's result."""
        now = self.clock()
        with self._lock:
            if repo.lower() in self._forgotten:  # uninstalled while the job ran
                return
            first = self._rebuilds.get(repo, {}).get("first", now)
            self._rebuilds[repo] = {"installation": installation, "branch": default_branch, "first": first, "last": now}

    def rebuild_due(self) -> list[str]:
        """Rebuild each triage queue whose wait is over, one at a time; the repos it was due for."""
        now = self.clock()
        with self._lock:
            busy = {key[0].lower() for key in [*self._pending, *self._busy]}
            due = {repo: wait for repo, wait in self._rebuilds.items() if now >= wait["first"] + QUEUE_MAX_WAIT
                   or (now >= wait["last"] + QUEUE_WAIT and repo.lower() not in busy)}
            for repo in due:
                del self._rebuilds[repo]
        for repo, wait in due.items():
            self.rebuild_queue(wait["installation"], repo, wait["branch"])
        return list(due)

    def rebuild_queue(self, installation: int, repo: str, default_branch: str) -> None:
        """No model and no clone: the policy, then `update_queue`. The App's
        bot posts the issue, so it finds and edits its own. A failure is a
        line in the log, and the next job's rebuild tries again."""
        try:
            gh = GitHub(self.auth.token(installation), repo, api=self.auth.api)
            policy = load_context_policy(gh, default_branch)
            if policy.triage.queue:  # turned off since, or a closed PR's repo that keeps none
                update_queue(Context(gh=gh, policy=policy, model=None, bot=self.auth.bot_login(), log=self.log))
        except Exception as exc:
            self.log(redact(f"{repo}: couldn't rebuild the triage queue: {type(exc).__name__}: {exc}"))

    def _rebuild_loop(self) -> None:
        while True:
            time.sleep(QUEUE_TICK)
            try:
                self.rebuild_due()
            except Exception as exc:  # rebuild_queue logs its own; this keeps the loop alive whatever happens
                self.log(redact(f"triage queue: {type(exc).__name__}: {exc}"))

    # ── the clone of each default branch ─────────────────────────────────

    def _clone_dir(self, repo: str) -> str:
        return os.path.join(self.workdir, _SAFE_NAME.sub("_", repo.replace("/", "__")))

    def checkout(self, repo: str, token: str, branch: str, window_days: int) -> str | None:
        """Clone or update the default branch with the window's history (just
        its newest commit when none is in the window). The token goes in a
        header for this one command, never into the clone's config or URL."""
        path = self._clone_dir(repo)
        header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
        since = (datetime.now(timezone.utc) - timedelta(days=window_days + 1)).strftime("%Y-%m-%d")
        url = f"https://github.com/{repo}.git"
        lock = self._repo_locks.setdefault(repo, threading.Lock())
        with lock:
            try:
                try:
                    _clone_or_fetch(path, url, branch, header, f"--shallow-since={since}")
                except subprocess.CalledProcessError as exc:
                    if "no commits selected for shallow requests" not in (exc.stderr or ""):
                        raise
                    # No commit in the window: the newest one alone gives the tree, and no history, which is right.
                    _clone_or_fetch(path, url, branch, header, "--depth=1")
                return path
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                self.log(redact(f"{repo}: couldn't update the clone ({type(exc).__name__} {describe_subprocess_error(exc)}); reviewing without history"))
                return None

    # ── retention ────────────────────────────────────────────────────────

    def _purge_loop(self) -> None:
        while True:
            time.sleep(60)
            try:
                self.purge_all()
            except Exception as exc:
                self.log(f"purge: {type(exc).__name__}: {exc}")
            time.sleep(PURGE_EVERY)

    def purge_all(self) -> None:
        """Every repo's rows past its own retention, even repos with no PRs
        lately, so the data page's promises hold without any activity. An
        installation or repo that fails is logged and skipped, not the rest."""
        # Every repo's job log, installed or not: the 14 days are the same for all.
        try:
            self.storage.purge(findings_days=None, jobs_days=JOBS_DAYS)
        except Exception as exc:
            self.log(redact(f"purge: couldn't purge the job log: {type(exc).__name__}: {exc}"))
        for installation in self.auth.installations():
            try:
                token = self.auth.token(installation["id"])
                repos = list(GitHub(token, "", api=self.auth.api).paginate("/installation/repositories"))
            except Exception as exc:
                self.log(redact(f"purge: installation {installation.get('id')}: couldn't list its repos: {type(exc).__name__}: {exc}"))
                continue
            for repo in repos:
                name = repo["full_name"]
                try:
                    self._purge_repo(token, name, repo.get("default_branch") or "main")
                except Exception as exc:
                    self.log(redact(f"{name}: couldn't purge: {type(exc).__name__}: {exc}"))

    def _purge_repo(self, token: str, repo: str, default_branch: str) -> None:
        try:
            days = load_context_policy(GitHub(token, repo, api=self.auth.api), default_branch).data.findings_retention_days
        except Exception as exc:  # a blocked repo keeps nothing for good: the default retention applies
            days = Policy().data.findings_retention_days
            self.log(redact(f"{repo}: couldn't read the policy ({type(exc).__name__}: {exc}); purging with the default {days} days"))
        self.storage.purge(findings_days=days, jobs_days=JOBS_DAYS, repo=repo)


def _clone_or_fetch(path: str, url: str, branch: str, header: str, shallow: str) -> None:
    if os.path.isdir(os.path.join(path, ".git")):
        _git(["-c", f"http.extraHeader={header}", "-C", path, "fetch", "--quiet", shallow, "origin", branch])
        _git(["-C", path, "checkout", "--quiet", "-B", branch, "FETCH_HEAD"])
        _git(["-C", path, "update-ref", f"refs/remotes/origin/{branch}", "FETCH_HEAD"])
    else:
        _git(["-c", f"http.extraHeader={header}", "clone", "--quiet", "--single-branch", "--branch", branch, shallow, url, path])


def _git(args: list[str]) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _respond(start_response, status: str, body: dict) -> list[bytes]:
    data = json.dumps(body).encode()
    start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(data)))])
    return [data]


def from_env(env: Mapping[str, str] = os.environ) -> Server:
    """The server from its environment. Anything missing or malformed stops
    the start with one readable line, not a traceback on the first webhook."""
    missing = [k for k in ("STRATUM_APP_ID", "STRATUM_WEBHOOK_SECRET") if not env.get(k)]
    key = env.get("STRATUM_APP_PRIVATE_KEY")
    if not key and env.get("STRATUM_APP_PRIVATE_KEY_FILE"):
        try:
            with open(env["STRATUM_APP_PRIVATE_KEY_FILE"], encoding="utf-8") as handle_file:
                key = handle_file.read()
        except OSError as exc:
            raise SystemExit(f"Couldn't read STRATUM_APP_PRIVATE_KEY_FILE ({env['STRATUM_APP_PRIVATE_KEY_FILE']}): {exc.strerror or exc}.") from None
    if not key:
        missing.append("STRATUM_APP_PRIVATE_KEY")
    if missing:
        raise SystemExit("Missing settings: " + ", ".join(missing) + ". See the README's GitHub App section.")
    if "PRIVATE KEY-----" not in key:
        raise SystemExit("STRATUM_APP_PRIVATE_KEY doesn't look like a PEM private key (it should start with -----BEGIN RSA PRIVATE KEY-----). "
                         "Download the App's private key from GitHub and point STRATUM_APP_PRIVATE_KEY_FILE at it.")
    try:
        workers = int(env.get("STRATUM_WORKERS", "2"))
    except ValueError:
        raise SystemExit(f"STRATUM_WORKERS must be a whole number, not {env.get('STRATUM_WORKERS')!r}.") from None
    db_path = env.get("STRATUM_DB", "stratum.db")
    try:
        storage = SQLiteStorage(db_path)
    except (sqlite3.Error, RuntimeError, OSError) as exc:
        raise SystemExit(f"Couldn't open the database at {db_path}: {exc}") from None
    return Server(
        AppAuth(env["STRATUM_APP_ID"], key, api=env.get("STRATUM_GITHUB_API", API)),
        env["STRATUM_WEBHOOK_SECRET"],
        storage,
        env.get("STRATUM_WORKDIR", "repos"),
        env=env,
        workers=max(1, workers),
    )


def application(environ: dict, start_response):  # for gunicorn and friends
    global _server
    if _server is None:
        _server = from_env()
    return _server(environ, start_response)


_server: Server | None = None
