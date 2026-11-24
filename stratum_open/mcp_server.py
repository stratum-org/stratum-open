"""`stratum-open mcp`: Stratum Open as an MCP server on stdio, so a coding
agent can check its own changes before it opens a PR, and fix what's found.

Each tool runs the CLI's own code:
- `check_local(base)` is `review --local BASE --no-model`: the checks, in seconds;
- `review_local(base, timeout)` is `review --local BASE`: the checks and the
  model's review, with the model from the same `STRATUM_MODEL_*` variables
  and policy file;
- `explain_finding(fingerprint)` is one finding from an earlier call, with
  the code around it and how to fix it. It calls no model;
- `check_policy(path)` is `policy check`;
- `triage_pr(repo, number)` is the label `review owner/repo#N --no-model`
  would give the PR, which posts nothing.

Nothing here writes to GitHub, comments, labels or pushes: `triage_pr` reads
through a session that refuses anything but a GET or a GraphQL query before
it's sent. The local tools don't run the checks that build the code
(`checks.go_vet`, `checks.clippy`), since a build runs it. The findings are
kept in memory, for `explain_finding`, until the server stops; nothing is
stored.

The tool names and the JSON they return are the contract for any other
server of these tools (hosted Stratum's included): change both together.

The MCP SDK is the `mcp` extra (`pip install "stratum-open[mcp]"`), imported
only when the server starts, so the core install doesn't need it. This
module has no `from __future__ import annotations`: the SDK reads the
tools' annotations at run time to build their schemas.
"""

import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

try:  # pydantic, which the SDK reads the result types with, needs this TypedDict before Python 3.12
    from typing_extensions import TypedDict
except ImportError:  # pragma: no cover  (no SDK, so nothing reads them)
    from typing import TypedDict

from stratum_open import __version__, cli
from stratum_open.checks.tools import listed
from stratum_open.engine import snippet_of
from stratum_open.findings import Finding
from stratum_open.github.api import GitHubError
from stratum_open.model import Reply, model_from_policy, model_off
from stratum_open.policy import POLICY_PATH, Policy, parse_policy
from stratum_open.util import count

TOOLS = ("check_local", "review_local", "explain_finding", "check_policy", "triage_pr")
MAX_FINDINGS = 50  # in one answer, most severe first; `omitted` counts the rest
MAX_QUOTED_LINES = 20  # of a finding's own code in a list of findings; explain_finding shows them all
AROUND = 5  # lines shown on each side of a finding by explain_finding
MAX_CONTEXT_LINES = 80
MAX_KEPT = 2000  # findings explain_finding can still explain; the oldest are forgotten first
DEFAULT_TIMEOUT = 600  # seconds, for review_local: a model on a CPU can take minutes
# Why checks.go_vet and checks.clippy don't run from these tools, which only read: building the code runs it.
NO_BUILD = ("it builds the code, which runs the code's build scripts and macros, and these tools only read. "
            "`stratum-open review --local` runs it.")
INSTRUCTIONS = (
    "Stratum Open reviews the commits on this checkout's branch against a base branch, as it would review the pull "
    "request. Call check_local (seconds, no model) after you commit a change, fix what it finds, commit and call it "
    "again. Call review_local (minutes, needs a model server) before you open the pull request. Both read HEAD, not "
    "edits that aren't committed, and say which those are. explain_finding gives one finding's code and fix by its "
    "fingerprint. No tool writes to GitHub."
)


class FindingJSON(TypedDict):
    fingerprint: str  # the same while the code it's on stays the same: the key for explain_finding
    rule: str
    severity: str  # low | medium | high | critical
    category: str
    source: str  # check | model
    path: str  # "" for the change as a whole
    line: int | None
    line_end: int | None
    title: str
    message: str
    suggestion: str
    code: str  # the lines it's on, as the change leaves them


class ModelJSON(TypedDict):
    name: str
    answered: bool  # at least one of its calls came back
    profile: str


class LocalJSON(TypedDict):
    base: str
    head: str  # the commit reviewed
    files: int
    findings: list[FindingJSON]
    total: int
    omitted: int  # findings left out of `findings` by the cap
    conclusion: str  # success | neutral | failure, as the check run on the PR would say
    uncommitted: list[str]  # changes the review didn't read, since it reads HEAD
    model: ModelJSON | None
    summary: str  # what the model says the change does
    notes: list[str]


class ExplainedJSON(FindingJSON):
    replacement: str | None  # exact new code for line..line_end, when the finding has one
    head: str  # the commit it was found on; "" for a PR's finding from triage_pr
    context: str  # the code around it, each line with its number
    context_start: int | None


class PolicyJSON(TypedDict):
    path: str
    exists: bool
    usable: bool
    errors: list[str]
    warnings: list[str]
    message: str


class TriageJSON(TypedDict):
    repo: str
    number: int
    status: str  # dry-run, or skipped when the PR isn't open
    label: str  # the label as the repo's policy names it; "" when skipped
    key: str  # ready | needs_work | low_effort | needs_human
    reasons: list[str]
    evidence: list[str]
    score: float  # higher: review sooner
    findings: list[FindingJSON]
    total: int
    omitted: int
    notes: list[str]


class Refused(Exception):
    """A tool can't answer, for a reason the agent can act on: the message says it."""


class ReadOnly(requests.Session):
    """GitHub for `triage_pr`: GETs, and GraphQL queries (a PR's linked
    issues are one). Anything else is refused before it's sent."""

    def request(self, method, url, *args, **kwargs):
        body = kwargs.get("json")
        query = body.get("query") if isinstance(body, dict) else None
        verb = str(method).upper()
        reads = verb == "GET" or (verb == "POST" and urlparse(str(url)).path.endswith("/graphql")
                                  and isinstance(query, str) and "mutation" not in query)
        if not reads:
            raise RuntimeError(f"stratum-open mcp only reads from GitHub: it won't send a {method} to {url}")
        return super().request(method, url, *args, **kwargs)


class Deadline:
    """A model that stops `seconds` from now, or when `stop` is set: a call
    after that fails at once, and the review reports it as a failed call;
    one in flight waits no longer than the time left. `heard(n)` hears the
    nth call come back. Everything else (its name, describe(), the context
    window the engine sets) is the model's."""

    def __init__(self, model: Any, seconds: int, stop: threading.Event | None = None,
                 heard: Callable[[int], None] | None = None):
        object.__setattr__(self, "_model", model)
        object.__setattr__(self, "_end", time.monotonic() + seconds)
        object.__setattr__(self, "_stop", stop or threading.Event())
        object.__setattr__(self, "_heard", heard)
        object.__setattr__(self, "seconds", seconds)
        object.__setattr__(self, "made", 0)  # calls made
        object.__setattr__(self, "refused", 0)  # calls not made

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._model, name, value)

    def passed(self) -> bool:
        return self._stop.is_set() or time.monotonic() >= self._end

    def complete(self, system: str, user: str, **kwargs: Any) -> Reply:
        left = self._end - time.monotonic()
        if left <= 0 or self._stop.is_set():
            object.__setattr__(self, "refused", self.refused + 1)
            why = "the review was cancelled" if self._stop.is_set() else f"review_local's timeout of {count(self.seconds, 'second')} passed"
            return Reply("", str(getattr(self._model, "name", "") or ""), 0, error=why)
        timeout = getattr(self._model, "timeout", None)
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            self._model.timeout = min(timeout, left)
        reply = self._model.complete(system, user, **kwargs)
        object.__setattr__(self, "made", self.made + 1)
        if self._heard is not None:
            self._heard(self.made)
        return reply


class Tools:
    """What the tools do, apart from the SDK: `build` serves these, and the
    tests can call them directly. Paths and git run from the current
    directory, the checkout `serve` moved to."""

    def __init__(self) -> None:
        self._kept: dict[str, tuple[Finding, str, str]] = {}  # fingerprint -> (finding, its code, the commit it was found on)
        self._lock = threading.Lock()

    # ── the tools ────────────────────────────────────────────────────────

    def check_local(self, base: str | None = None) -> dict[str, Any]:
        return self._local(base, None)

    def review_local(self, base: str | None = None, timeout: int = DEFAULT_TIMEOUT,
                     progress: Callable[[int, str], None] | None = None, stop: threading.Event | None = None) -> dict[str, Any]:
        if timeout < 1:
            raise Refused("timeout is in seconds, and at least 1.")

        def heard(done: int) -> None:
            if progress is not None:
                progress(done, f"{count(done, 'model call')} done")

        def make_model(policy: Policy) -> Deadline | None:
            model = model_from_policy(policy)
            return None if model is None else Deadline(model, timeout, stop, heard)

        return self._local(base, make_model)

    def explain_finding(self, fingerprint: str) -> dict[str, Any]:
        with self._lock:
            kept = self._kept.get((fingerprint or "").strip().lower())
        if kept is None:
            raise Refused(f"No finding {fingerprint!r} in this session. Call check_local or review_local first: "
                          "explain_finding knows the findings of the calls since the server started.")
        finding, code, head = kept
        context, start = "", None
        if head and finding.path and finding.line_start is not None:
            text = cli._git_or_none(".", "show", f"{head}:{finding.path}")
            if text is not None:
                lines = text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")
                start = max(1, finding.line_start - AROUND)
                end = min(len(lines), (finding.line_end or finding.line_start) + AROUND, start + MAX_CONTEXT_LINES - 1)
                width = len(str(end))
                context = "\n".join(f"{n:>{width}}  {lines[n - 1]}" for n in range(start, end + 1))
        return {**_as_json(finding, code), "replacement": finding.replacement, "head": head, "context": context,
                "context_start": start if context else None}

    def check_policy(self, path: str = POLICY_PATH) -> dict[str, Any]:
        root = Path((cli._git_or_none(".", "rev-parse", "--show-toplevel") or ".").strip()).resolve()
        file = (root / path).resolve()
        if not file.is_relative_to(root):
            raise Refused(f"check_policy reads files in this checkout only, and {path} isn't in {root}.")
        shown = file.relative_to(root).as_posix()
        try:
            text = file.read_text(encoding="utf-8") if file.is_file() else None
        except (OSError, UnicodeDecodeError) as exc:
            raise Refused(f"Couldn't read {shown}: {exc}") from None
        policy = parse_policy(text)
        if text is None:
            message = f"No {shown}: the defaults apply."
        elif policy.errors:
            message = f"{shown} can't be used: {count(len(policy.errors), 'error')}. Reviews use the defaults until it's fixed."
        else:
            message = f"{shown} is usable."
        return {"path": shown, "exists": text is not None, "usable": not policy.errors, "errors": list(policy.errors),
                "warnings": list(policy.warnings), "message": message}

    def triage_pr(self, repo: str, number: int) -> dict[str, Any]:
        from stratum_open.checks.dependencies import PackageLookup
        from stratum_open.github.api import GitHub
        from stratum_open.runner import Context, load_context_policy, review_pull
        from stratum_open.triage import label_for

        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
            raise Refused(f"Not a repository: {repo!r}. Name it owner/name, as in stratum-org/stratum-open.")
        if number < 1:
            raise Refused("number is the PR's number, from 1.")

        def log(line: str) -> None:  # stdout is the protocol's
            print(line, file=sys.stderr)

        try:
            gh = GitHub(cli._token(), repo, api=cli._api(), session=ReadOnly(), log=log)
            policy = load_context_policy(gh, gh.repository().get("default_branch") or "main")
            cli._policy_notes(policy)
            # As `review owner/repo#N --no-model` does: the summary's state is read only under STRATUM_BOT_LOGIN.
            ctx = Context(gh=gh, policy=policy, model=None, bot=os.environ.get("STRATUM_BOT_LOGIN") or "-",
                          lookup=PackageLookup(), log=log)
            outcome = review_pull(ctx, number, post=False)
        except SystemExit as exc:  # the CLI's own one-line errors: no token, say
            raise Refused(str(exc.code)) from None
        except GitHubError as exc:
            raise Refused(f"{exc}. {cli._github_hint(exc.status)}".strip()) from None
        except requests.RequestException as exc:
            raise Refused(f"Couldn't reach {cli._host(exc)}: {type(exc).__name__}. Check the network, the URL and any proxy.") from None
        found = [self._keep(f, "", "") for f in outcome.findings]
        sorted_pr = outcome.triage
        notes = [outcome.note] if outcome.note else []
        notes += [f"policy: {e} Using the defaults." for e in policy.errors] + [f"policy: {w}" for w in policy.warnings]
        if sorted_pr is not None:
            notes.append("No model was run: with one, a critical finding or a description that doesn't match the code makes "
                         f"a PR {label_for(policy, 'needs_work')!r}.")
        return {
            "repo": repo, "number": number, "status": outcome.status,
            "label": label_for(policy, sorted_pr.key) if sorted_pr else "", "key": sorted_pr.key if sorted_pr else "",
            "reasons": sorted_pr.reasons if sorted_pr else [], "evidence": sorted_pr.evidence if sorted_pr else [],
            "score": round(sorted_pr.score, 2) if sorted_pr else 0.0,
            "findings": found[:MAX_FINDINGS], "total": len(found), "omitted": max(0, len(found) - MAX_FINDINGS), "notes": notes,
        }

    # ── the work they share ──────────────────────────────────────────────

    def _local(self, base: str | None, make_model) -> dict[str, Any]:
        base, notes = (base, []) if base else default_base()
        if base.startswith("-"):
            raise Refused(f"Not a branch or commit: {base!r}.")
        head = (cli._git_or_none(".", "rev-parse", "HEAD") or "").strip()
        try:
            local = cli.local_review(base, make_model=make_model, no_build=NO_BUILD)
        except SystemExit as exc:  # the CLI's own one-line error: git couldn't compare BASE with HEAD
            raise Refused(str(exc.code)) from None
        moved = (cli._git_or_none(".", "rev-parse", "HEAD") or "").strip()
        if moved != head:
            notes.append(f"HEAD moved from {head[:7]} to {moved[:7]} during the review, so part of it may have read the newer commit. "
                         "Call it again for an answer about one commit.")
        status = cli._git_or_none(".", "status", "--porcelain", "--untracked-files=normal") or ""
        uncommitted = [line[3:] for line in status.splitlines() if len(line) > 3]
        if uncommitted:
            notes.append(f"{listed(uncommitted)} {'has' if len(uncommitted) == 1 else 'have'} changes that aren't committed, "
                         "which this didn't read: it reviews HEAD. Commit them, and call it again to have them checked.")
        if local is None:
            return {"base": base, "head": head, "files": 0, "findings": [], "total": 0, "omitted": 0, "conclusion": "success",
                    "uncommitted": uncommitted, "model": None, "summary": "", "notes": notes + [f"No changes between {base} and HEAD."]}
        result, files, contents, model = local
        notes += [f"policy: {e} Using the defaults." for e in result.policy.errors] + [f"policy: {w}" for w in result.policy.warnings]
        notes += result.check_notes
        model_json, summary = None, ""
        if model is None and model_off(result.policy):
            notes.append("The model is off (review.endpoint: off, or STRATUM_MODEL_ENDPOINT=off), so this has only the checks.")
        if model is not None:
            review = result.review
            answered = review is not None and review.ran
            model_json = {"name": str(getattr(model, "name", "") or ""), "answered": answered, "profile": result.profile}
            if review is None or not review.calls:
                notes.append("The model had nothing to read: every changed file is binary, deleted or left out by the policy.")
            elif not answered:
                errors = "; ".join(sorted({c["error"] for c in review.calls if c.get("error")}))
                notes.append(f"No model answered ({errors}), so this has only the checks. To have the model review it, point "
                             "STRATUM_MODEL_ENDPOINT (and STRATUM_MODEL_NAME) at a running OpenAI-compatible server or Ollama, "
                             f"or set review.endpoint in {POLICY_PATH}.")
            elif review.failed_calls:
                notes.append(f"{review.failed_calls} of {len(review.calls)} model calls failed; some files weren't reviewed.")
            if isinstance(model, Deadline) and model.passed() and model.refused:
                notes.append(f"The review stopped at its timeout of {count(model.seconds, 'second')}, with "
                             f"{count(model.refused, 'model call')} not made. Call review_local with a longer timeout to finish it.")
            summary = review.summary if review is not None and review.ran else ""
        found = [self._keep(f, _quote(f, files, contents), head) for f in result.findings]
        return {
            "base": base, "head": head, "files": len(files), "findings": found[:MAX_FINDINGS], "total": len(found),
            "omitted": max(0, len(found) - MAX_FINDINGS), "conclusion": result.conclusion, "uncommitted": uncommitted,
            "model": model_json, "summary": summary, "notes": notes,
        }

    def _keep(self, finding: Finding, code: str, head: str) -> dict[str, Any]:
        """The finding as JSON, kept for explain_finding."""
        with self._lock:
            self._kept.pop(finding.fingerprint, None)  # the latest goes last, so it's forgotten last
            self._kept[finding.fingerprint] = (finding, code, head)
            while len(self._kept) > MAX_KEPT:
                del self._kept[next(iter(self._kept))]
        lines = code.split("\n")
        if len(lines) > MAX_QUOTED_LINES:
            code = "\n".join(lines[:MAX_QUOTED_LINES] + [f"... and {count(len(lines) - MAX_QUOTED_LINES, 'more line')}"])
        return _as_json(finding, code)


def default_base() -> tuple[str, list[str]]:
    """The base when a call names none: the remote's default branch
    (origin/HEAD), as the local branch of that name if there is one, else
    `main`; and a note saying which it is."""
    remote = (cli._git_or_none(".", "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").strip()
    name = remote.removeprefix("origin/")
    if not remote.startswith("origin/") or not name:
        return "main", ["Compared with main: no base was given, and origin/HEAD isn't set (`git remote set-head origin --auto` sets it)."]
    if cli._git_or_none(".", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}") is None:
        return remote, [f"Compared with {remote}, the remote's default branch: no base was given, and there's no local {name}."]
    return name, [f"Compared with {name}, the remote's default branch: no base was given."]


def _as_json(finding: Finding, code: str) -> dict[str, Any]:
    return {
        "fingerprint": finding.fingerprint, "rule": finding.rule, "severity": finding.severity, "category": finding.category,
        "source": finding.source, "path": finding.path, "line": finding.line_start, "line_end": finding.line_end,
        "title": finding.title, "message": finding.detail, "suggestion": finding.suggestion, "code": code,
    }


def _quote(finding: Finding, files: list, contents: dict[str, str]) -> str:
    """The code a finding is on: the diff's lines, or the file's own for a
    line the diff doesn't show (a check reading a file moved into its scope)."""
    if not finding.path or finding.line_start is None:
        return ""
    code = snippet_of(finding, files)
    if not code and finding.path in contents:
        code = "\n".join(contents[finding.path].split("\n")[finding.line_start - 1:finding.line_end or finding.line_start])
    return code


# ── the server ───────────────────────────────────────────────────────────


def build(tools: Tools | None = None):
    """The MCP server, with the five tools. Needs the SDK."""
    from mcp.server.mcpserver import Context, MCPServer  # first: without the SDK, its ImportError names it
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    import anyio  # the SDK's own dependency
    import anyio.from_thread
    import anyio.to_thread

    tools = tools or Tools()
    server = MCPServer("stratum-open", version=__version__, instructions=INSTRUCTIONS, log_level="WARNING")
    local = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    remote = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

    def answer(work, *args):
        try:
            return work(*args)
        except Refused as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=local)
    def check_local(base: str | None = None) -> LocalJSON:
        """The checks that need no model, on the commits between `base` and
        HEAD: workflows, dependencies, secrets, Bandit, Ruff, hidden Unicode,
        agent files and the rest, but not go vet or Clippy, which build the
        code and so run it. `base` defaults to the remote's default
        branch (origin/HEAD), else main; the answer's `base` says which.
        Answers in seconds: call it after each commit. Findings come most
        severe first, at most 50; `omitted` counts the rest."""
        return answer(tools.check_local, base)

    @server.tool(annotations=local)
    async def review_local(ctx: Context, base: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> LocalJSON:
        """`check_local`, plus the model's review of the change, with the
        model the STRATUM_MODEL_* variables or the policy name. Takes minutes
        on a CPU. It makes no model call after `timeout` seconds and answers
        with what it has. With no model answering, it returns the checks and
        says so in `notes`."""
        stop = threading.Event()

        def progress(done: int, message: str) -> None:
            try:
                anyio.from_thread.run(ctx.report_progress, done, None, message)
            except Exception:  # a client that went away doesn't stop the review
                pass

        await ctx.report_progress(0, None, "Running the checks, then the model's review")
        try:
            return await anyio.to_thread.run_sync(lambda: answer(tools.review_local, base, timeout, progress, stop),
                                                  abandon_on_cancel=True)
        finally:
            stop.set()  # cancelled: the model gets no more calls

    @server.tool(annotations=local)
    def explain_finding(fingerprint: str) -> ExplainedJSON:
        """One finding from an earlier check_local, review_local or
        triage_pr call, by its fingerprint: its full text, the code around
        it with line numbers, and how to fix it. Calls no model."""
        return answer(tools.explain_finding, fingerprint)

    @server.tool(annotations=local)
    def check_policy(path: str = POLICY_PATH) -> PolicyJSON:
        """Whether a Stratum policy file in this checkout is usable: its
        errors (which make reviews fall back to the defaults) and warnings."""
        return answer(tools.check_policy, path)

    @server.tool(annotations=remote)
    def triage_pr(repo: str, number: int) -> TriageJSON:
        """The triage label Stratum would give an open PR (`repo` is
        owner/name), with its reasons and the checks' findings, from the
        checks alone. Reads GitHub with GITHUB_TOKEN (or the GitHub CLI's
        sign-in) and posts nothing."""
        return answer(tools.triage_pr, repo, number)

    return server


def serve(repo: str = ".") -> int:
    """`stratum-open mcp`: the tools on stdio, reading the checkout at
    `repo`, until the client closes the connection."""
    try:
        server = build()
    except ImportError as exc:
        if (exc.name or "").split(".")[0] != "mcp":
            raise
        sys.exit('stratum-open mcp needs the MCP SDK, version 2: pip install "stratum-open[mcp]"')
    try:
        os.chdir(repo)
    except OSError as exc:
        sys.exit(f"Can't open {repo}: {exc.strerror or exc}.")
    server.run("stdio")
    return 0
