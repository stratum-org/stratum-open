"""How the go vet and Clippy checks do on merged PRs.

Both checks are off unless `checks.go_vet` and `checks.clippy` are on, and
they build the PR's code, so they need it checked out at its head, with its
dependencies. This clones each project of clean-go.json and clean-rust.json
once, checks out each PR's head (`pull/N/head`), runs both checks on it as a
review would (on a `Worktree` of it), and writes down every issue go vet,
staticcheck and Clippy report on a line the PR adds, kept by the checks or
not, with how long each build took.

    python benchmark/build_checks.py fetch
    python benchmark/build_checks.py run --repos ../repos --out benchmark/results/<name>/raw/<project> --repo <owner/name>
    # mark the verdict column of issues.csv: right | wrong | unsure
    python benchmark/build_checks.py score benchmark/results/<name>

Building a project runs its code (build scripts, procedural macros, cgo, and
whatever their dependencies do), so run it only where that's fine. The
**Build-check benchmark** workflow runs `run` on GitHub's runners, one job
per project, inside a container made from benchmark/Dockerfile (Go,
staticcheck, Rust with Clippy, Python), away from the runner and its
credentials. It needs Linux, `go` and `staticcheck` for the Go projects,
and `cargo clippy` for the Rust ones.

`fetch` caches each PR's diff in benchmark/.cache. It's the one step that
needs a GitHub token, so `run` can go without one.

`run` checks the PRs out one at a time, in the order of the lists (newest
first), in one clone per project. Each PR keeps what the ones before it
downloaded and built (Go's module and build caches, one cargo target folder
per project, outside the clone), as a workflow that caches them would. So
the first PR of each project is built with nothing cached, as on a fresh
runner. A toolchain the project names (go.mod's `toolchain` line, a
rust-toolchain file) is installed before the build is timed. The checks run
with a time limit of `--timeout` (an hour) instead of their own (300 s for go
vet and staticcheck, 600 s for Clippy), so a slow build is measured rather
than cut off; `score` counts the builds that would have run past a review's
limit.

The programs are asked for more than the checks ask for, from the same build,
so what the checks leave out is measured too: staticcheck runs with every SA
check, DROPPED_SA's included, and Clippy with its `suspicious` group as well
as `correctness`. The checks' own reading of the output still leaves those
out, so kept.csv is what a review would get. It writes:

- issues.csv: one row per issue on a line the PR adds, in a file the check
  read, with whether the check keeps it. It keeps the marks when it's
  repeated.
- kept.csv: the checks' findings, as a review gets them.
- prs.json: for each PR, the files and added lines each check read (those
  under a module or crate it ran on), the toolchains, the seconds each
  program took, and the checks' coverage lines (a build that failed, say).
  Each program run is there too, with its exit code, the first line of its
  error output, and what it built: the crates cargo built or found built
  (its `compiler-artifact` messages) and how many it compiled then, or the
  packages go vet reported on. Then the free disk space, and the size of the
  project's clone and target folder, which it also prints after each project.

`score` reads every prs.json under the folder, with the issues.csv beside it,
and writes summary.md: what each check kept, with the marks, the build times
per project, every issue by program and code, kept or not, what didn't run,
and the folder's notes.md. A program run that built nothing counts as a build
that didn't happen, whatever its exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench import _fetch, _write  # noqa: E402
from shell_checks import FINDING_COLUMNS, ISSUE_COLUMNS, VERDICTS, _cell, _key, _read  # noqa: E402
from stratum_open.checks import clippy, go_vet, tools  # noqa: E402
from stratum_open.checks.common import find_program  # noqa: E402
from stratum_open.diff import FileDiff, parse_diff  # noqa: E402

HERE = Path(__file__).resolve().parent
PRS = [str(HERE / "planted" / "clean-go.json"), str(HERE / "planted" / "clean-rust.json")]
SOURCE = "https://github.com/{repo}.git"
TIMEOUT_SECONDS = 3600
SETUP_SECONDS = 900
MB = 1_000_000
# Each check by the name its coverage lines use: its module, its policy key, the programs it runs (as tools.run names
# them, each with the name a row of issues.csv gives it), and the file at a module's or crate's root.
CHECKS = {
    "go vet": (go_vet, "checks.go_vet", {"go vet": "go vet", "staticcheck": "staticcheck"}, "go.mod"),
    "Clippy": (clippy, "checks.clippy", {"Clippy": "clippy"}, "Cargo.toml"),
}
# Clippy's lint group asked for besides `correctness`, whose lints the check doesn't keep.
CLIPPY_ALSO = "clippy::suspicious"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("fetch", "run"):
        command = sub.add_parser(name)
        command.add_argument("--prs", nargs="+", default=PRS)
        command.add_argument("--repo", nargs="*", default=[], help="only these projects (owner/name); all when left out or `all`")
    run = sub.choices["run"]
    run.add_argument("--repos", required=True, help="a folder for one clone per project, outside this repository")
    run.add_argument("--out", required=True)
    run.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS, help="seconds each check may take on a PR")
    score = sub.add_parser("score")
    score.add_argument("out")
    args = parser.parse_args()
    if args.command == "score":
        return _score(Path(args.out))
    prs = picked(args.prs, args.repo)
    if args.command == "fetch":
        for item in prs:
            _fetch(item["repo"], int(item["pr"]))
        print(f"Fetched {len(prs)} PR(s)", flush=True)
        return 0
    return _run(prs, Path(args.repos), Path(args.out), args.timeout)


def picked(paths: list[str], repos: list[str]) -> list[dict]:
    prs = [item for path in paths for item in json.loads(Path(path).read_text(encoding="utf-8"))]
    wanted = set(repos) - {"all"}
    unknown = wanted - {item["repo"] for item in prs}
    if unknown:
        raise SystemExit("Not in the lists: " + ", ".join(sorted(unknown)))
    return [item for item in prs if not wanted or item["repo"] in wanted]


# ── the clones ───────────────────────────────────────────────────────────


def _git(where: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(where), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def clone(repos: Path, repo: str) -> Path:
    """The project's clone, made once, without file contents until a
    checkout needs them."""
    where = repos / repo.replace("/", "__")
    if not (where / ".git").exists():
        repos.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", SOURCE.format(repo=repo), str(where)], check=True)
    return where


def fetch_heads(where: Path, numbers: list[int]) -> None:
    """Each PR's head, `pull/N/head`, in one fetch, or one at a time when a
    ref is missing."""
    refs = [f"+refs/pull/{n}/head:refs/remotes/origin/pull/{n}" for n in numbers]
    if refs and _git(where, "fetch", "--quiet", "origin", *refs).returncode != 0:
        for ref in refs:
            _git(where, "fetch", "--quiet", "origin", ref)


def check_out(where: Path, head: str) -> bool:
    """The clone at `head`, with nothing left from the PR before it (the
    build output goes outside the clone). False when it can't be had."""
    if _git(where, "cat-file", "-e", f"{head}^{{commit}}").returncode != 0:
        _git(where, "fetch", "--quiet", "origin", head)
    if _git(where, "checkout", "--quiet", "--force", "--detach", head).returncode != 0:
        return False
    _git(where, "clean", "-ffdxq")
    return _git(where, "rev-parse", "HEAD").stdout.strip() == head


def prepare(worktree: str, files: list[FileDiff]) -> tuple[dict[str, str], str, float]:
    """The toolchains this checkout builds with, each installed first when
    the project names one, so its download isn't timed as the build: their
    versions, what went wrong, and the seconds it took."""
    steps = []
    if any(go_vet.wanted(file) for file in files):
        steps += [("go", ["go", "version"]), ("staticcheck", ["staticcheck", "-version"])]
    if any(clippy.wanted(file) for file in files):
        steps += [("", ["rustup", "component", "add", "clippy"]), ("clippy", ["cargo", "clippy", "--version"])]
    versions, errors, started = {}, [], time.monotonic()
    for key, command in steps:
        program = find_program(command[0])
        if program is None:
            continue
        try:
            done = subprocess.run([program, *command[1:]], cwd=worktree, env=tools.build_env(), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=SETUP_SECONDS)
        except subprocess.TimeoutExpired:
            errors.append(f"`{' '.join(command)}` took longer than {SETUP_SECONDS} s")
            continue
        if done.returncode != 0:
            errors.append(f"`{' '.join(command)}`: {tools.first_line(done.stderr or done.stdout)}")
        elif key:
            versions[key] = (done.stdout.strip().splitlines() or [""])[0]
    return versions, "; ".join(errors), round(time.monotonic() - started, 1)


# ── running the checks ───────────────────────────────────────────────────


@contextmanager
def recording(calls: list[dict], timeout: float) -> Iterator[None]:
    """While it lasts, each program the checks run is written down in
    `calls` (its folder, exit code, output and seconds), it's asked for
    more than the check asks for (`widened`), and both checks may take
    `timeout` seconds."""
    real = tools.run

    def run(command: list[str], *, cwd: str, timeout: float, name: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        call = {"name": name, "cwd": cwd, "code": None, "stdout": "", "stderr": "", "seconds": 0.0, "timed_out": False}
        calls.append(call)
        started = time.monotonic()
        try:
            result = real(widened(command, name), cwd=cwd, timeout=timeout, name=name, env=env)
        except tools.Skipped:
            call["timed_out"] = True
            raise
        finally:
            call["seconds"] = round(time.monotonic() - started, 1)
        call.update(code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        return result

    saved = tools.run, go_vet.TIMEOUT_SECONDS, clippy.TIMEOUT_SECONDS
    tools.run, go_vet.TIMEOUT_SECONDS, clippy.TIMEOUT_SECONDS = run, timeout, timeout
    try:
        yield
    finally:
        tools.run, go_vet.TIMEOUT_SECONDS, clippy.TIMEOUT_SECONDS = saved


def widened(command: list[str], name: str) -> list[str]:
    """A program's command asking for what the check leaves out as well:
    staticcheck's every SA check, DROPPED_SA's included, and Clippy's
    `suspicious` lints next to its `correctness` ones."""
    if name == "staticcheck" and "-checks" in command:
        at = command.index("-checks") + 1
        return [*command[:at], "SA*", *command[at + 1:]]
    if name == "Clippy" and "clippy::correctness" in command:
        at = command.index("clippy::correctness") + 1
        return [*command[:at], "--force-warn", CLIPPY_ALSO, *command[at:]]
    return command


def measure(worktree: str, files: list[FileDiff], timeout: float = TIMEOUT_SECONDS) -> dict:
    """Both checks on the PR's checkout, as a review runs them but with
    `timeout` seconds each: their findings and coverage lines, every program
    they ran, and the seconds each check took (for those that ran one)."""
    out: dict = {"findings": [], "notes": [], "calls": [], "seconds": {}}
    for name, (module, _, _, _) in CHECKS.items():
        calls: list[dict] = []
        with recording(calls, timeout):
            started = time.monotonic()
            out["findings"] += module.check(files, tools.Worktree(worktree), out["notes"])
            seconds = time.monotonic() - started
        if calls:
            out["seconds"][name] = round(seconds, 1)
        out["calls"] += calls
    return out


def issues(worktree: str, files: list[FileDiff], calls: list[dict]) -> list[dict]:
    """A row for every issue the programs reported on a line the PR adds,
    in a file its check reads, whether the check keeps it or not."""
    rows = []
    for call in calls:
        check = next(name for name, (_, _, programs, _) in CHECKS.items() if call["name"] in programs)
        module, _, programs, _ = CHECKS[check]
        added = {file.path: dict(file.added()) for file in files
                 if file.status != "deleted" and not file.binary and module.wanted(file)}
        for code, level, paths, line, end, message, kept in reported(worktree, call):
            path = next((p for p in paths if p in added), None)
            at = tools.first_added(added[path], line, end) if path else None
            if at is None:
                continue
            rows.append({"tool": programs[call["name"]], "code": code, "level": level, "path": path, "line": at,
                         "text": added[path][at].strip()[:200], "message": " ".join(message.split())[:300],
                         "kept": "yes" if kept else "no", "verdict": "", "note": ""})
    return rows


def runs(worktree: str, calls: list[dict]) -> list[dict]:
    """Each program run, as prs.json keeps it: the program, the folder it ran
    in, its exit code (None when it ran out of time), the first line of its
    error output, its seconds, and what it built (`built`)."""
    return [{"program": call["name"], "folder": tools.relative(worktree, call["cwd"]) or ".", "code": call["code"],
             "stderr": error_line(call["stderr"]), "seconds": call["seconds"], "timed_out": call["timed_out"], **built(call)}
            for call in calls]


def error_line(text: str) -> str:
    """The first line of a program's error output, leaving out what isn't an
    error there: go vet's `# package` lines, and the JSON report it wrote to
    stderr before Go 1.24 or so."""
    return tools.first_line("\n".join(line for line in text.splitlines() if not line.lstrip().startswith(("{", "}", '"', "[", "]"))))


def built(call: dict) -> dict:
    """What a program run built, as its output tells: for Clippy, the crates
    cargo built or found built (`compiler-artifact`) and how many it compiled
    then; for go vet, the packages it reported on, `{}` for one with nothing
    to say. Nothing for staticcheck, whose output doesn't tell."""
    if call["name"] == "Clippy":
        artifacts = [item for item in _json_lines(call["stdout"]) if item.get("reason") == "compiler-artifact"]
        return {"built": len(artifacts), "compiled": sum(1 for item in artifacts if not item.get("fresh"))}
    if call["name"] == "go vet":
        return {"built": len(go_vet._json_objects(call["stdout"]) + go_vet._json_objects(call["stderr"]))}
    return {}


def _json_lines(text: str) -> list[dict]:
    found = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            found.append(item)
    return found


def built_nothing(run: dict) -> bool:
    """A run that finished and, by its output, built nothing."""
    return run.get("built") == 0 and not run.get("timed_out")


def disk(where: Path) -> dict:
    """Free space where the clone is, and the size of the clone and of its
    cargo target folder, in MB."""
    return {"free_mb": round(shutil.disk_usage(where).free / MB, 1), "clone_mb": round(size(where) / MB, 1),
            "target_mb": round(size(Path(str(where) + ".target")) / MB, 1)}


def size(folder: Path) -> int:
    """The bytes of the files under `folder`, links not followed."""
    total = 0
    for root, _, names in os.walk(folder):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def read_by(worktree: str, files: list[FileDiff], name: str, calls: list[dict]) -> list[FileDiff]:
    """The PR's files check `name` read: those it wants under a module or
    crate where one of its programs ran to the end. A file with no root
    above it, past the limit of roots, or past the time limit wasn't read."""
    module, _, programs, marker = CHECKS[name]
    wanted = [file for file in files if file.status != "deleted" and not file.binary and file.added() and module.wanted(file)]
    ran = {os.path.normcase(os.path.realpath(call["cwd"])) for call in calls if call["name"] in programs and not call["timed_out"]}
    groups = tools.group_by_root(worktree, [file.path for file in wanted], marker)
    read = {path for root, paths in groups.items() if root is not None
            and os.path.normcase(os.path.realpath(os.path.join(worktree, *root.split("/")) if root else worktree)) in ran for path in paths}
    return [file for file in wanted if file.path in read]


def reported(worktree: str, call: dict) -> Iterator[tuple[str, str, list[str], int, int, str, bool]]:
    """What one program run reported: (code, level, the paths it may be,
    first line, last line, message, kept by the check), every code included."""
    if call["name"] == "go vet":
        yield from _vet(worktree, call)
    elif call["name"] == "staticcheck":
        yield from _staticcheck(worktree, call)
    elif call["name"] == "Clippy":
        yield from _clippy(worktree, call)


def _vet(worktree: str, call: dict) -> Iterator[tuple]:
    for report in go_vet._json_objects(call["stdout"]) + go_vet._json_objects(call["stderr"]):
        for analyzers in report.values():
            for analyzer, diagnostics in (analyzers.items() if isinstance(analyzers, dict) else ()):
                for diagnostic in diagnostics if isinstance(diagnostics, list) else ():
                    start = go_vet._POSITION.match(str(diagnostic.get("posn") or "")) if isinstance(diagnostic, dict) else None
                    if start is None:
                        continue
                    end = go_vet._POSITION.match(str(diagnostic.get("end") or ""))
                    line = int(start["line"])
                    path = tools.relative(worktree, os.path.join(call["cwd"], start["path"]))
                    yield (analyzer, "", [path] if path else [], line, int(end["line"]) if end and end["path"] == start["path"] else line,
                           str(diagnostic.get("message") or ""), analyzer in go_vet.ANALYZERS)


def _staticcheck(worktree: str, call: dict) -> Iterator[tuple]:
    for text in call["stdout"].splitlines():
        try:
            item = json.loads(text)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        code, location, end = str(item.get("code") or ""), item.get("location") or {}, item.get("end") or {}
        if code == "compile" or not location.get("file") or not isinstance(location.get("line"), int):
            continue
        path = tools.relative(worktree, os.path.join(call["cwd"], str(location["file"])))
        yield (code, str(item.get("severity") or ""), [path] if path else [], location["line"],
               end["line"] if isinstance(end.get("line"), int) else location["line"], str(item.get("message") or ""),
               code.startswith("SA") and code not in go_vet.DROPPED_SA)


def _clippy(worktree: str, call: dict) -> Iterator[tuple]:
    # File names are relative to the workspace's root: the crate's folder or one above it.
    root = tools.relative(worktree, call["cwd"]) or "."
    parts = [] if root == "." else root.split("/")
    above = [os.path.join(worktree, *parts[:depth]) for depth in range(len(parts), -1, -1)]
    for text in call["stdout"].splitlines():
        try:
            item = json.loads(text)
        except ValueError:
            continue
        message = item.get("message") if isinstance(item, dict) and item.get("reason") == "compiler-message" else None
        code = str(((message.get("code") or {}) if isinstance(message, dict) else {}).get("code") or "")
        if not code.startswith("clippy::"):
            continue
        lint = code[len("clippy::"):]
        for span in message.get("spans") or []:
            if isinstance(span, dict) and span.get("is_primary") and isinstance(span.get("line_start"), int):
                named = str(span.get("file_name") or "")
                paths = [p for p in (tools.relative(worktree, os.path.join(base, named)) for base in above) if p]
                yield (lint, str(message.get("level") or ""), paths, span["line_start"], span.get("line_end") or span["line_start"],
                       str(message.get("message") or ""), lint in clippy.CORRECTNESS)
                break


def _run(prs: list[dict], repos: Path, out: Path, timeout: float) -> int:
    out.mkdir(parents=True, exist_ok=True)
    repos = repos.resolve()  # the checks join the worktree to the paths they hand a program, which runs in it
    marks = {_key(row): row for row in _read(out / "issues.csv")}
    rows, findings, listed = [], [], []

    def save() -> None:  # after each PR, so a job that's stopped keeps what it did
        _write(out / "issues.csv", ISSUE_COLUMNS, rows)
        _write(out / "kept.csv", FINDING_COLUMNS, findings)
        (out / "prs.json").write_text(json.dumps(listed, indent=1) + "\n", encoding="utf-8")

    by_repo: dict[str, list[dict]] = defaultdict(list)
    for item in prs:
        by_repo[item["repo"]].append(item)
    for repo, items in by_repo.items():
        where = clone(repos, repo)
        fetch_heads(where, [int(item["pr"]) for item in items])
        # One target folder per project, outside the clone, so cleaning the clone between PRs keeps the build.
        os.environ["CARGO_TARGET_DIR"] = str(where) + ".target"
        for order, item in enumerate(items):
            number, case = int(item["pr"]), f"{repo}#{item['pr']}"
            _, text = _fetch(repo, number)
            files = parse_diff(text)
            entry = {**item, "order": order, "error": ""}
            if not check_out(where, item["head"]):
                entry["error"] = f"its head {item['head'][:12]} couldn't be checked out"
                listed.append(entry)
                print(f"{case}: {entry['error']}", flush=True)
                save()
                continue
            entry["toolchains"], entry["error"], entry["setup_seconds"] = prepare(str(where), files)
            result = measure(str(where), files, timeout)
            found = issues(str(where), files, result["calls"])
            for row in found:
                row["case"] = case
                old = marks.get(_key(row))
                if old:
                    row["verdict"], row["note"] = old["verdict"], old["note"]
            rows += found
            for finding in result["findings"]:
                findings.append({"case": case, "rule": finding.rule, "severity": finding.severity, "path": finding.path,
                                 "line": finding.line_start or "", "title": finding.title})
            read = {name: read_by(str(where), files, name, result["calls"]) for name in CHECKS}
            programs: dict[str, float] = defaultdict(float)
            for call in result["calls"]:
                programs[call["name"]] += call["seconds"]
            entry.update({
                "files": {name: len(read[name]) for name in CHECKS}, "lines": {name: sum(len(f.added()) for f in read[name]) for name in CHECKS},
                "seconds": result["seconds"], "programs": {name: round(seconds, 1) for name, seconds in programs.items()},
                "timed_out": sorted({call["name"] for call in result["calls"] if call["timed_out"]}),
                "runs": runs(str(where), result["calls"]),
                "notes": result["notes"], "issues": len(found), "findings": len(result["findings"]), "disk": disk(where),
            })
            listed.append(entry)
            print(f"{case}: {len(found)} issue(s) on added lines, {len(result['findings'])} kept; "
                  + ", ".join(f"{name} {seconds:.0f} s" for name, seconds in result["seconds"].items())
                  + "".join(f", {run['program']} built {run['built']}" for run in entry["runs"] if "built" in run)
                  + "".join(f"; {note}" for note in result["notes"]) + (f"; {entry['error']}" if entry["error"] else ""), flush=True)
            save()
        space = disk(where)
        print(f"{repo}: {space['free_mb']:,.0f} MB free; the clone is {space['clone_mb']:,.0f} MB "
              f"and its target folder {space['target_mb']:,.0f} MB", flush=True)
    save()
    return 0


# ── the summary ──────────────────────────────────────────────────────────


def load(out: Path) -> tuple[list[dict], list[dict]]:
    """Every PR and every issue row of the run folders under `out`."""
    prs, rows = [], []
    for listing in sorted(out.rglob("prs.json")):
        prs += json.loads(listing.read_text(encoding="utf-8"))
        rows += _read(listing.parent / "issues.csv")
    return prs, rows


def scores(out: Path) -> list[dict]:
    """Per check, what a run adds up to: the PRs and added lines it read,
    what it kept with the marks, the PRs whose code didn't build, and the
    builds that would have run past a review's time limit."""
    prs, rows = load(out)
    table = []
    for name, (module, setting, programs, _) in CHECKS.items():
        ran = [p for p in prs if name in p.get("seconds", {})]
        kept = [row for row in rows if row["kept"] == "yes" and row["tool"] in programs.values()]
        table.append({
            "run": out.name, "check": name, "setting": setting, "prs": len(ran), "lines": sum(p["lines"][name] for p in ran),
            "findings": len(kept), **{verdict: sum(1 for row in kept if row["verdict"] == verdict) for verdict in VERDICTS},
            "unbuilt": sum(1 for p in ran if unbuilt(p, programs)),
            "limit": module.TIMEOUT_SECONDS, "slow": sum(1 for p in ran if p["seconds"][name] > module.TIMEOUT_SECONDS),
        })
    return table


def unbuilt(pr: dict, programs: dict[str, str]) -> bool:
    """Whether a PR's code didn't build for a check: a coverage line says so,
    or one of the check's programs finished having built nothing."""
    return (any(failed(note, programs) for note in pr["notes"])
            or any(run["program"] in programs and built_nothing(run) for run in pr.get("runs", [])))


def nothing_built(pr: dict) -> list[str]:
    """A line for each of the PR's program runs that built nothing."""
    return [f"{run['program']} built nothing in `{run['folder']}` (exit code {run['code']}"
            + (f", {tools.code_span(run['stderr'])}" if run["stderr"] else "") + ")"
            for run in pr.get("runs", []) if built_nothing(run)]


def failed(note: str, programs: dict[str, str]) -> bool:
    """Whether a coverage line says one of the programs couldn't build the
    code or didn't run on it."""
    return any(note.startswith((f"{program} couldn't build", f"{program} didn't run on")) for program in programs)


def build_times(prs: list[dict]) -> list[dict]:
    """Per project and check: the seconds of its first build, with nothing
    cached, and of the others, which reused what the ones before had built."""
    table = []
    for name, (module, _, _, _) in CHECKS.items():
        by_repo: dict[str, list[dict]] = defaultdict(list)
        for p in prs:
            if name in p.get("seconds", {}):
                by_repo[p["repo"]].append(p)
        for repo, ran in by_repo.items():
            ran.sort(key=lambda p: p["order"])
            rest = [p["seconds"][name] for p in ran[1:]]
            spaces = [p["disk"] for p in ran if p.get("disk")]
            table.append({
                "repo": repo, "check": name, "prs": len(ran), "first": ran[0]["seconds"][name],
                "median": statistics.median(rest) if rest else None, "slowest": max(p["seconds"][name] for p in ran),
                "slow": sum(1 for p in ran if p["seconds"][name] > module.TIMEOUT_SECONDS), "limit": module.TIMEOUT_SECONDS,
                "used": max((s["clone_mb"] + s["target_mb"] for s in spaces), default=None),
                "free": min((s["free_mb"] for s in spaces), default=None),
            })
    return table


def _seconds(value: float | None) -> str:
    return "" if value is None else f"{value:,.0f} s"


def _megabytes(value: float | None) -> str:
    return "" if value is None else f"{value:,.0f} MB"


def _score(out: Path) -> int:
    prs, rows = load(out)
    projects = len({p["repo"] for p in prs})
    lines = [
        "# go vet and Clippy on merged PRs", "",
        f"{len(prs)} merged PRs from {projects} project{'' if projects == 1 else 's'}, each checked out at its head and built, "
        "as a review with the check on would. Each issue the programs reported on a line a PR adds was read and marked: "
        "right when what it says is true of the code, wrong when the code works as meant (a false alarm), unsure otherwise.", "",
        "| Check | PRs it read | Added lines | Findings | Right | Wrong | Unsure | Didn't build | Past the review's limit |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in scores(out):
        lines.append(f"| `{row['setting']}` | {row['prs']} | {row['lines']:,} | {row['findings']} | {row['right']} | {row['wrong']} "
                     f"| {row['unsure']} | {row['unbuilt']} | {row['slow']} (over {row['limit']} s) |")
    lines += [
        "", "## Build times", "",
        "The seconds each check took on a PR, downloading the dependencies included. The first PR of each project was built "
        "with nothing cached, as on a fresh runner; the others reused what the PRs before them had downloaded and built. "
        "The space is the most the project's clone and cargo target folder took after a PR, and the least disk left free.", "",
        "| Project | Check | PRs | First PR | Median of the others | Slowest | Past the review's limit | Clone and target | Free |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in build_times(prs):
        lines.append(f"| {row['repo']} | {row['check']} | {row['prs']} | {_seconds(row['first'])} | {_seconds(row['median'])} "
                     f"| {_seconds(row['slowest'])} | {row['slow']} | {_megabytes(row['used'])} | {_megabytes(row['free'])} |")
    lines += ["", "## Every issue on added lines, kept or not", "", "| Program | Code | Kept | Issues | Right | Wrong | Unsure |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[(row["tool"], row["code"], row["kept"])].append(row)
    for (tool, code, kept), group in sorted(groups.items(), key=lambda item: (item[0][2] != "yes", item[0][0], -len(item[1]), item[0][1])):
        lines.append(f"| {tool} | `{code}` | {kept} | {len(group)} | " + " | ".join(str(sum(1 for r in group if r["verdict"] == v)) for v in VERDICTS) + " |")
    kept = [row for row in rows if row["kept"] == "yes"]
    if kept:
        lines += ["", "What the checks keep:", "", "| Where | Code | Line | Verdict | Why |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {row['case']} `{row['path']}`:{row['line']} | `{row['code']}` | `{_cell(row['text'][:80])}` | {row['verdict']} | {_cell(row['note'])} |"
                  for row in kept]
    missing = [(p, note) for p in prs for note in ([p["error"]] if p.get("error") else []) + p.get("notes", []) + nothing_built(p)]
    if missing:
        lines += ["", "## What didn't run", "", "| PR | Why |", "| --- | --- |"]
        lines += [f"| {p['repo']}#{p['pr']} | {_cell(note)} |" for p, note in missing]
    versions: dict[str, set[str]] = defaultdict(set)
    for p in prs:
        for key, value in p.get("toolchains", {}).items():
            versions[key].add(value)
    if versions:
        lines += ["", "Toolchains: " + "; ".join(f"{key}: " + ", ".join(f"`{v}`" for v in sorted(values)) for key, values in sorted(versions.items())) + "."]
    notes = out / "notes.md"
    if notes.exists():
        lines += ["", notes.read_text(encoding="utf-8").strip()]
    out.mkdir(parents=True, exist_ok=True)  # a run whose jobs all failed has no folder
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
