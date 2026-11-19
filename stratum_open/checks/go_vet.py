"""go vet on the Go packages a PR changes, when `checks.go_vet` is on and
`go` is on PATH, and staticcheck's SA checks too when `staticcheck` is.

They run in a checkout of the PR's head (see tools.py for where there is one,
and why a PR from a fork never gets one), from the root of each module, the
folder with the `go.mod` nearest a changed file, on the packages holding the
changed `.go` files. Only what they report on lines the PR adds is kept. Test
files are skipped.

Both need the module to build, with its dependencies, which `go` downloads
into its module cache if they aren't there. A package that doesn't build gets
a coverage line, never a finding: a compile error is for the project's CI to
report. `go vet` doesn't run the code or `go generate`, but it does run cgo,
and so the C compiler, on a package that imports "C", and it downloads the
modules `go.mod` names; that's why a PR it mustn't build is refused.

Of go vet's analyzers, all but `composites` (struct literals without field
names, a matter of style) and `tests` (test files aren't read) are kept, each
with what goes wrong in ANALYZERS. One that a newer Go adds isn't kept until
it's listed. Of staticcheck, only the SA checks, its "correctness" ones,
except those in DROPPED_SA. It runs with `-show-ignored`, so a `//lint:ignore`
the PR adds can't hide one, and a finding on such a line says so.

Neither has been measured on merged PRs yet: building each project of the
benchmark's corpus takes minutes (benchmark/README.md).
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import time
from typing import Any

from stratum_open.checks import tools
from stratum_open.checks.common import extension, find_program, is_test_path
from stratum_open.diff import FileDiff
from stratum_open.findings import Finding

MAX_MODULES = 3
TIMEOUT_SECONDS = 300  # for go vet and staticcheck together, over every module; downloading dependencies counts
_WRONG_OUTPUT = "The output isn't what the code says it should be."
# go vet's analyzers that are kept: name -> (severity, what goes wrong).
ANALYZERS: dict[str, tuple[str, str]] = {
    "appends": ("medium", "`append` with nothing to add returns the slice unchanged, so the call does nothing."),
    "asmdecl": ("high", "The assembly doesn't match its Go declaration, so it reads or writes the wrong place on the stack."),
    "assign": ("medium", "A variable assigned to itself doesn't change: another variable was probably meant."),
    "atomic": ("high", "Assigning the result of an atomic add back to the variable is not atomic, so updates can be lost."),
    "bools": ("medium", "The condition is always true or always false, or repeats itself, so a branch can't work as written."),
    "buildtag": ("high", "A build constraint that's malformed or misplaced is ignored, so the file is built where it shouldn't be, or never."),
    "cgocall": ("high", "Passing this Go pointer to C breaks cgo's rules, which can crash the program at run time."),
    "copylocks": ("high", "A lock copied by value is a separate lock, so the copy and the original don't exclude each other."),
    "defers": ("medium", "`defer` evaluates its arguments when it's reached, not when the function returns, so the value is the early one."),
    "directive": ("medium", "A `//go:` directive in the wrong place, or misspelled, is ignored."),
    "errorsas": ("high", "`errors.As` panics unless its second argument is a non-nil pointer to an error type or interface."),
    "framepointer": ("high", "The assembly overwrites the frame pointer before saving it, which breaks profiling and tracebacks."),
    "hostport": ("medium", "An address built with `%s:%d` breaks for IPv6 hosts; `net.JoinHostPort` adds the brackets they need."),
    "httpresponse": ("high", "The response is used before the error is checked, so a failed request dereferences nil."),
    "ifaceassert": ("high", "This interface-to-interface type assertion can never succeed."),
    "loopclosure": ("high", "A goroutine or `defer` in the loop uses the loop variable, which before Go 1.22 is one variable for every iteration."),
    "lostcancel": ("medium", "The context's cancel function isn't called on every path, so the context and its resources live until the parent ends."),
    "nilfunc": ("medium", "A function compared with nil is never nil: calling it was probably meant."),
    "printf": ("medium", "The format string and its arguments don't match. " + _WRONG_OUTPUT),
    "shift": ("high", "A shift by the type's width or more always gives 0."),
    "sigchanyzer": ("medium", "`signal.Notify` with an unbuffered channel can drop the signal if nothing is receiving at that moment."),
    "slog": ("medium", "The key-value arguments don't pair up, so the log record gets `!BADKEY` entries."),
    "stdmethods": ("high", "A method named like one of a well-known interface's has the wrong signature, so the type doesn't implement it."),
    "stdversion": ("high", "This standard-library name is newer than the Go version `go.mod` declares, so the build fails with that version."),
    "stringintconv": ("medium", "`string(n)` on an integer gives the character with that code, not its digits."),
    "structtag": ("medium", "The struct tag is malformed, so `encoding/json` and the like ignore it."),
    "testinggoroutine": ("medium", "`t.Fatal` from a goroutine the test started doesn't stop the test."),
    "timeformat": ("high", "`2006-02-01` is year-day-month, so dates are formatted or parsed wrong."),
    "unmarshal": ("high", "Unmarshalling into something that isn't a pointer fails, and leaves the value unchanged."),
    "unreachable": ("medium", "This code can never run."),
    "unsafeptr": ("high", "Converting a `uintptr` back to `unsafe.Pointer` here isn't one of the allowed patterns, so the garbage collector can move or free the memory."),
    "unusedresult": ("medium", "The result of a call that does nothing else is thrown away, so the call is useless."),
    "waitgroup": ("high", "`wg.Add` inside the goroutine races with `wg.Wait`, which can return before the goroutine has started."),
}
# staticcheck's SA checks that are left out: code -> why.
DROPPED_SA: dict[str, str] = {
    "SA1019": "a deprecated name still works; that's for the project to plan, not a bug",
    "SA5009": "go vet's printf reports the same",
}
_POSITION = re.compile(r"^(?P<path>.+?):(?P<line>\d+)(?::\d+)?$")


def available() -> bool:
    return find_program("go") is not None


def wanted(file: FileDiff) -> bool:
    """Go code outside the tests and outside `vendor/`, which is other
    projects' code, copied in."""
    return extension(file.path) == ".go" and not is_test_path(file.path) and "vendor" not in file.path.split("/")[:-1]


def check(files: list[FileDiff], worktree: tools.Worktree | None, notes: list[str]) -> list[Finding]:
    return tools.check(
        files, worktree, notes, name="go vet", installed=available(), install="`go` isn't on PATH.",
        wanted=wanted, marker="go.mod", max_roots=MAX_MODULES, timeout=TIMEOUT_SECONDS, run_root=run_module,
    )


def run_module(worktree: str, root: str, paths: list[str], seconds: float) -> tuple[list[tools.Issue], str]:
    """go vet, then staticcheck if it's installed and time is left, on the
    packages holding `paths`, from the module at `root`."""
    folder = os.path.join(worktree, *root.split("/")) if root else worktree
    folders = {posixpath.relpath(posixpath.dirname(path) or ".", root or ".") for path in paths}
    packages = sorted("." if rel == "." else "./" + rel for rel in folders)
    started = time.monotonic()
    issues, problem = vet(worktree, folder, packages, seconds)
    left = seconds - (time.monotonic() - started)
    if find_program("staticcheck") is not None and not problem and left > 1:
        found, problem = staticcheck(worktree, folder, packages, left)
        issues += found
    return issues, problem


def vet(worktree: str, folder: str, packages: list[str], seconds: float) -> tuple[list[tools.Issue], str]:
    program = find_program("go")
    if program is None:
        return [], "go vet didn't run: `go` isn't on PATH any more."
    result = tools.run([program, "vet", "-json", *packages], cwd=folder, timeout=seconds, name="go vet", env=tools.build_env())
    # -json writes to stdout from Go 1.24 or so, to stderr before; both are read.
    reports = _json_objects(result.stdout) + _json_objects(result.stderr)
    issues = []
    for report in reports:
        for analyzers in report.values():
            if not isinstance(analyzers, dict):
                continue
            for analyzer, diagnostics in analyzers.items():
                if analyzer not in ANALYZERS or not isinstance(diagnostics, list):
                    continue
                for diagnostic in diagnostics:
                    issue = _vet_issue(worktree, folder, analyzer, diagnostic)
                    if issue is not None:
                        issues.append(issue)
    problem = ""
    if result.returncode != 0:
        # What isn't the JSON report, which went to stderr too before Go 1.24 or so.
        errors = "\n".join(line for line in result.stderr.splitlines() if not line.lstrip().startswith(("{", "}", '"', "]", "[")))
        problem = (f"go vet couldn't build every package of {_module(worktree, folder)} "
                   f"({tools.code_span(tools.first_line(errors)) if tools.first_line(errors) else f'exit code {result.returncode}'}), "
                   "so its findings there are missing.")
    return issues, problem


def staticcheck(worktree: str, folder: str, packages: list[str], seconds: float) -> tuple[list[tools.Issue], str]:
    program = find_program("staticcheck")
    if program is None:
        return [], ""
    checks = ",".join(["SA*", *(f"-{code}" for code in DROPPED_SA)])
    result = tools.run([program, "-f", "json", "-checks", checks, "-tests=false", "-show-ignored", *packages],
                       cwd=folder, timeout=seconds, name="staticcheck", env=tools.build_env())
    issues, compile_error = [], ""
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "")
        if code == "compile":
            compile_error = compile_error or str(item.get("message") or "")
            continue
        location = item.get("location") or {}
        end = item.get("end") or {}
        path = tools.relative(worktree, str(location.get("file") or "")) if location.get("file") else None
        line_number = location.get("line")
        if not code.startswith("SA") or code in DROPPED_SA or path is None or not isinstance(line_number, int):
            continue
        message = str(item.get("message") or "")
        detail = f"staticcheck reports {code}: {_quote(message)}."
        if item.get("severity") == "ignored":
            detail += " A `//lint:ignore` comment asks staticcheck to skip it. Stratum reports it anyway, since the line is new in this PR."
        issues.append(tools.Issue(
            path=path, line=line_number, end_line=end.get("line") if isinstance(end.get("line"), int) else line_number,
            rule=f"staticcheck/{code}", title=tools.title("staticcheck", code, message), detail=detail, severity="medium",
            suggestion=f"See https://staticcheck.dev/docs/checks/#{code} for what it means and how to fix it.",
        ))
    if compile_error:
        problem = (f"staticcheck couldn't build every package of {_module(worktree, folder)} "
                   f"({tools.code_span(tools.first_line(compile_error))}), so its findings there are missing.")
    elif result.returncode > 1 or (not issues and not result.stdout.strip() and result.stderr.strip()):
        # 1 is staticcheck finding something; past that, or nothing on stdout and an error on stderr (built with
        # an older Go than go.mod asks for, say), it didn't run.
        reason = tools.first_line(result.stderr)
        problem = (f"staticcheck didn't run on {_module(worktree, folder)} ({tools.code_span(reason) if reason else f'exit code {result.returncode}'}), "
                   "so its findings are missing.")
    else:
        problem = ""
    return issues, problem


def _vet_issue(worktree: str, folder: str, analyzer: str, diagnostic: Any) -> tools.Issue | None:
    if not isinstance(diagnostic, dict):
        return None
    start = _POSITION.match(str(diagnostic.get("posn") or ""))
    if start is None:
        return None
    path = tools.relative(worktree, start["path"] if os.path.isabs(start["path"]) else os.path.join(folder, start["path"]))
    if path is None:
        return None
    end = _POSITION.match(str(diagnostic.get("end") or ""))
    line = int(start["line"])
    severity, why = ANALYZERS[analyzer]
    message = str(diagnostic.get("message") or "")
    return tools.Issue(
        path=path, line=line, end_line=int(end["line"]) if end and end["path"] == start["path"] else line,
        rule=f"go-vet/{analyzer}", title=tools.title("go vet", analyzer, message),
        detail=f"go vet's {analyzer} check reports {_quote(message)}. {why}", severity=severity,
        suggestion=f"See https://pkg.go.dev/golang.org/x/tools/go/analysis/passes/{analyzer} for what it checks.",
    )


def _json_objects(text: str) -> list[dict]:
    """The JSON objects in go vet's output, which puts `# package` lines
    between them."""
    decoder = json.JSONDecoder()
    found, at = [], 0
    while True:
        at = text.find("{", at)
        if at < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, at)
        except ValueError:
            at += 1
            continue
        if isinstance(value, dict):
            found.append(value)
        at = end


def _module(worktree: str, folder: str) -> str:
    rel = tools.relative(worktree, folder)
    return f"the module in `{rel}`" if rel and rel != "." else "the module"


def _quote(message: str) -> str:
    return "`" + message.replace("`", "'").replace("\n", " ").strip()[:300].rstrip(".") + "`"
