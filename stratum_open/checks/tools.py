"""What the checks that build the PR's code share: go vet (`checks.go_vet`)
and Clippy (`checks.clippy`).

Ruff, ShellCheck and actionlint parse a file's text, so they get it from
`contents` and read it in a temporary folder. These two can't: they compile
the package the file is in, which needs the rest of the module or crate and
its dependencies. So they run in a `Worktree`, a checkout of the PR's head on
disk, and only where the caller has one: the CLI's `review --local`, and the
Action when the workflow checks the PR out and passes `head-checkout`.

Building code runs code. `cargo clippy` runs the crate's build scripts and
procedural macros, and reads `.cargo/config.toml`, which can name a program
to run in rustc's place. `go vet` runs cgo, which runs the C compiler, and
downloads the modules `go.mod` names. A PR's code mustn't run where a token
that can write to the repository can be reached, so a caller hands over a
worktree it mustn't build as `refused`, with the reason, and these checks
then don't run. The runner refuses a PR from a fork or a bot (`runner.worktree_for`).

A tool that isn't installed, a worktree that's missing or refused, a build
that fails and a run that takes too long all end the same way: no findings
from it, and a line in the review's coverage saying what didn't run and why.
A build failure is for the project's CI to report, never a finding here.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from stratum_open.diff import FileDiff
from stratum_open.findings import Finding

MAX_LISTED = 5
KILL_WAIT_SECONDS = 10  # how long a stopped tool's pipes may take to close


@dataclass(frozen=True)
class Worktree:
    """The PR's head, checked out at `path`. `refused` says why its code
    mustn't be built here; the checks that build code don't run then."""
    path: str
    refused: str = ""


@dataclass
class Issue:
    """One thing a tool reported, already judged worth keeping: where (a path
    relative to the worktree, with `/`), and the finding it makes."""
    path: str
    line: int
    end_line: int
    rule: str
    title: str
    detail: str
    severity: str = "medium"
    suggestion: str = ""


class Skipped(Exception):
    """A tool's findings are missing for a reason that isn't a bug: it took
    too long, or the code didn't build. The message is the coverage line."""


# Runs the tool once, in one module or crate: (the worktree, the root's path relative to it, the PR's
# files under that root, the seconds left) -> the issues to keep, and a coverage line when part of it
# didn't run ("" when all of it did).
RunRoot = Callable[[str, str, list[str], float], tuple[list[Issue], str]]


def check(
    files: list[FileDiff],
    worktree: Worktree | None,
    notes: list[str],
    *,
    name: str,
    installed: bool,
    install: str,
    wanted: Callable[[FileDiff], bool],
    marker: str,
    max_roots: int,
    timeout: float,
    run_root: RunRoot,
) -> list[Finding]:
    """Run a tool over the modules or crates holding the files `wanted`
    picks, and keep what it reports on the lines the PR adds.

    `name` is the tool as the coverage line names it; `install`, what to do
    when it isn't `installed`; `marker`, the file at a module's or crate's
    root (`go.mod`, `Cargo.toml`). At most `max_roots` roots are read, in
    `timeout` seconds in all. Anything that keeps the tool from running, or
    from finishing, goes in `notes` as a coverage line."""
    targets = [file.path for file in files if file.status != "deleted" and not file.binary and file.added() and wanted(file)]
    if not targets:
        return []
    if not installed:
        notes.append(f"{name} didn't run: {install}")
        return []
    why = unavailable(name, worktree)
    if worktree is None or why is not None:
        notes.append(why or "")
        return []
    groups = group_by_root(worktree.path, targets, marker)
    loose = groups.pop(None, [])
    if loose:
        notes.append(f"Not read by {name}, with no `{marker}` above {'it' if len(loose) == 1 else 'them'}: {listed(loose)}")
    roots = sorted(groups, key=lambda root: (-len(groups[root]), root or ""))
    if len(roots) > max_roots:
        skipped = [path for root in roots[max_roots:] for path in groups[root]]
        notes.append(f"Not read by {name}, past its limit of {max_roots} {'modules' if marker == 'go.mod' else 'crates'}: {listed(skipped)}")
    added = {file.path: dict(file.added()) for file in files if file.path in targets}
    deadline = time.monotonic() + timeout
    findings = []
    for root in roots[:max_roots]:
        left = deadline - time.monotonic()
        if left <= 0:
            notes.append(f"{name} ran out of its {timeout:.0f} s before reading {listed(groups[root])}.")
            continue
        try:
            issues, problem = run_root(worktree.path, root or "", groups[root], left)
        except Skipped as exc:
            notes.append(str(exc))
            continue
        if problem:
            notes.append(problem)
        for issue in issues:
            line = first_added(added.get(issue.path, {}), issue.line, issue.end_line)
            if line is None:
                continue
            findings.append(Finding(
                title=issue.title, detail=issue.detail, severity=issue.severity, category="bug", path=issue.path,
                line_start=line, line_end=line, suggestion=issue.suggestion, source="check", rule=issue.rule, confidence=0.9,
            ))
    return findings


def unavailable(name: str, worktree: Worktree | None) -> str | None:
    """Why a check that builds the code can't run on this worktree, as a
    coverage line, or None when it can."""
    if worktree is None:
        return (f"{name} didn't run: it builds the code, and this review has no checkout of the PR's head "
                "(the Action has one only when the workflow passes `head-checkout`).")
    if worktree.refused:
        return f"{name} didn't run: {worktree.refused}"
    return None


# The environment a build never gets: Stratum's own settings (its GitHub token, the model's key), the job's
# token, the runner's own channels to GitHub, and the Action's inputs. A build script that dumps its
# environment then finds none of them. Code running as the same user can still read the parent's, so
# this narrows what leaks; refusing to build code that isn't trusted (runner.worktree_for) is the gate.
_SECRET_PREFIXES = ("STRATUM_", "ACTIONS_", "INPUT_")
_SECRET_NAMES = frozenset({"GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                           # The files a step writes to set variables, PATH and outputs for the steps after it, the
                           # `stratum-open action` step among them, which holds the token.
                           "GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_STATE"})


def build_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment for a tool that builds the PR's code,
    without the variables that hold this job's tokens and keys."""
    source = os.environ if environ is None else environ
    return {key: value for key, value in source.items()
            if not key.upper().startswith(_SECRET_PREFIXES) and key.upper() not in _SECRET_NAMES}


def run(command: list[str], *, cwd: str, timeout: float, name: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a tool with a time limit. Raises Skipped when it doesn't finish,
    after stopping it and every process it started (the compiler, a build
    script, a macro), which would otherwise go on running. Ctrl+C, or any
    other exception, stops them too: in a session of its own the tool doesn't
    get the terminal's interrupt."""
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                          encoding="utf-8", errors="replace", cwd=cwd, env=env, **group) as process:
        try:
            stdout, stderr = process.communicate(timeout=max(1.0, timeout))
        except subprocess.TimeoutExpired:
            _stop(process)
            raise Skipped(f"{name} took longer than {timeout:.0f} s, so its findings are missing from this review.") from None
        except BaseException:
            _stop(process)
            raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _stop(process: subprocess.Popen) -> None:
    """Stop the tree, then wait a little for the pipes to close: a process
    that left the group can hold them open for ever."""
    stop_tree(process)
    try:
        process.communicate(timeout=KILL_WAIT_SECONDS)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def stop_tree(process: subprocess.Popen) -> None:
    """Kill a process and everything it started."""
    try:
        if os.name == "nt":
            # By its full path: a bare name is looked for in the current folder first, which can be the PR's checkout.
            taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
            subprocess.run([taskkill, "/F", "/T", "/PID", str(process.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    process.kill()


def group_by_root(worktree: str, paths: list[str], marker: str) -> dict[str | None, list[str]]:
    """The paths by the nearest folder above them, inside the worktree, that
    holds `marker`, as a path relative to the worktree ("" for its top);
    None for those with no such folder."""
    groups: dict[str | None, list[str]] = {}
    for path in paths:
        parts = path.split("/")[:-1]
        root = next(("/".join(parts[:depth]) for depth in range(len(parts), -1, -1)
                     if os.path.isfile(os.path.join(worktree, *parts[:depth], marker))), None)
        groups.setdefault(root, []).append(path)
    return groups


def relative(worktree: str, path: str) -> str | None:
    """A path a tool printed, absolute or relative to the worktree, as one
    relative to the worktree with `/`, or None when it's outside it."""
    top = os.path.realpath(worktree)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(top, path))
    try:
        rel = os.path.relpath(full, top)
    except ValueError:  # another drive, on Windows
        return None
    return None if rel == ".." or rel.startswith(".." + os.sep) else rel.replace(os.sep, "/")


def first_added(added: dict[int, str], line: int, end: int) -> int | None:
    """The first line of `line`..`end` that the PR adds, or None."""
    return next((n for n in range(line, max(line, end) + 1) if n in added), None)


def first_line(text: str) -> str:
    """The first line of a tool's error output worth showing, cut short."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line[:200]
    return ""


def listed(paths: list[str]) -> str:
    return ", ".join(f"`{p}`" for p in paths[:MAX_LISTED]) + (f" and {len(paths) - MAX_LISTED} more" if len(paths) > MAX_LISTED else "")


def code_span(text: str) -> str:
    """Text a tool printed, inside a code span, so a mention or a link the
    PR's code put in it (a `compile_error!`) stays text."""
    return "`" + text.replace("`", "'").replace("\n", " ").strip()[:200] + "`"


def title(tool: str, code: str, message: str) -> str:
    """`<tool> <code>: <the message's first sentence>`, the message inside a
    code span, so text from the PR it quotes can't mention anyone or link."""
    first = message.replace("`", "'").replace("\n", " ").strip().split(". ")[0].rstrip(".")
    return f"{tool} {code}: `" + (first[:77] + "..." if len(first) > 80 else first) + "`"
