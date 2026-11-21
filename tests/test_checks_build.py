"""go vet (checks.go_vet) and Clippy (checks.clippy): the checks that build
the PR's code, in a checkout of its head, and so run it."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fakes import FakeGitHub

from stratum_open import cli
from stratum_open.action import _context
from stratum_open.checks import clippy, go_vet, run_checks, tools
from stratum_open.checks.tools import Worktree
from stratum_open.diff import parse_diff
from stratum_open.engine import analyze
from stratum_open.policy import Policy, parse_policy
from stratum_open.runner import Context, review_pull, worktree_for

GO = parse_policy("checks:\n  go_vet: true\n")
RUST = parse_policy("checks:\n  clippy: true\n")
BOTH = parse_policy("checks:\n  go_vet: true\n  clippy: true\n")
has_go = go_vet.available()


def _has_clippy() -> bool:
    if not clippy.available():
        return False
    try:
        return subprocess.run(["cargo", "clippy", "--version"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


has_clippy = _has_clippy()
needs_go = pytest.mark.skipif(not has_go, reason="go isn't on PATH")
needs_clippy = pytest.mark.skipif(not has_clippy, reason="cargo clippy isn't installed")

GO_FILE = 'package m\n\nimport "fmt"\n\nfunc F(s string) {\n\tfmt.Println(s)\n\tfmt.Printf("%d\\n", s)\n}\n'
RUST_FILE = "pub fn f(a: i32) -> bool {\n    let b = a + 1;\n    a == a && b > 0\n}\n"


def new_file(path, text):
    lines = text.rstrip("\n").split("\n")
    head = [f"diff --git a/{path} b/{path}", "new file mode 100644", "--- /dev/null", f"+++ b/{path}", f"@@ -0,0 +1,{len(lines)} @@"]
    return "\n".join(head + [f"+{line}" for line in lines]) + "\n"


def edit(path, text, added):
    """A diff that adds only line `added` of `text`; the other lines were there."""
    lines = text.rstrip("\n").split("\n")
    body = [("+" if n == added else " ") + line for n, line in enumerate(lines, start=1)]
    head = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", f"@@ -1,{len(lines) - 1} +1,{len(lines)} @@"]
    return "\n".join(head + body) + "\n"


def write(root: Path, files: dict[str, str]) -> Path:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8", newline="\n")
    return root


def go_module(root: Path, path="a.go", text=GO_FILE) -> Path:
    return write(root, {"go.mod": "module example.com/m\n\ngo 1.21\n", path: text})


def rust_crate(root: Path, path="src/lib.rs", text=RUST_FILE) -> Path:
    return write(root, {"Cargo.toml": '[package]\nname = "c"\nversion = "0.1.0"\nedition = "2021"\n', path: text})


def vet_report(root: Path, *diagnostics):
    """go vet -json's report: (analyzer, path, line, message) each."""
    by_analyzer = {}
    for analyzer, path, line, message in diagnostics:
        by_analyzer.setdefault(analyzer, []).append({"posn": f"{root / path}:{line}:2", "message": message})
    return "# example.com/m\n" + json.dumps({"example.com/m": by_analyzer}, indent="\t")


def fake_run(monkeypatch, *results, seen=None):
    """tools.run, answering with each (exit code, stdout, stderr) in turn."""
    answers = list(results)

    def run(command, *, cwd, timeout, name, env=None):
        if seen is not None:
            seen.append({"command": command, "cwd": cwd, "timeout": timeout, "env": env})
        code, stdout, stderr = answers.pop(0)
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(tools, "run", run)


def too_slow(monkeypatch):
    """Every tool runs past its time limit."""

    class Slow:
        def __init__(self, command, **kwargs):
            self.command, self.pid, self.returncode, self.calls = command, 0, None, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(self.command, timeout)
            return "", ""

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(tools.subprocess, "Popen", Slow)
    monkeypatch.setattr(tools, "stop_tree", lambda process: process.kill())


def installed(monkeypatch, *programs):
    """Only these programs are on PATH, as far as the checks can tell."""
    where = {name: f"/usr/bin/{name}" for name in programs}
    monkeypatch.setattr(go_vet, "find_program", where.get)
    monkeypatch.setattr(clippy, "find_program", where.get)


# ── go vet, with its output faked ────────────────────────────────────────


def test_go_vet_reports_on_added_lines_only(monkeypatch, tmp_path):
    installed(monkeypatch, "go")
    root = go_module(tmp_path)
    seen = []
    fake_run(monkeypatch, (0, vet_report(root, ("printf", "a.go", 7, "fmt.Printf format %d has arg s of wrong type string"),
                                         ("printf", "a.go", 6, "on a line the PR didn't add"),
                                         ("composites", "a.go", 7, "unkeyed fields: style, never kept")), ""), seen=seen)
    notes = []
    findings = go_vet.check(parse_diff(edit("a.go", GO_FILE, 7)), Worktree(str(root)), notes)
    assert [(f.rule, f.path, f.line_start, f.severity) for f in findings] == [("go-vet/printf", "a.go", 7, "medium")]
    assert findings[0].source == "check" and "`fmt.Printf format %d has arg s of wrong type string`" in findings[0].detail
    assert notes == []
    assert seen[0]["command"][1:] == ["vet", "-json", "."] and seen[0]["cwd"] == str(root)


def test_go_vet_runs_on_the_changed_packages_from_their_module_root(monkeypatch, tmp_path):
    installed(monkeypatch, "go")
    write(tmp_path, {"svc/go.mod": "module example.com/svc\n", "svc/api/h.go": "package api\n", "svc/db/q.go": "package db\n",
                     "tools/x.go": "package x\n", "svc/api/h_test.go": "package api\n"})
    seen = []
    fake_run(monkeypatch, (0, "", ""), seen=seen)
    notes = []
    diff = "".join(new_file(p, "package p\n") for p in ("svc/api/h.go", "svc/db/q.go", "svc/api/h_test.go", "tools/x.go"))
    go_vet.check(parse_diff(diff), Worktree(str(tmp_path)), notes)
    assert [s["command"][1:] for s in seen] == [["vet", "-json", "./api", "./db"]] and seen[0]["cwd"] == str(tmp_path / "svc")
    assert notes == ["Not read by go vet, with no `go.mod` above it: `tools/x.go`"]


def test_without_go_it_does_nothing_and_says_so(monkeypatch, tmp_path):
    installed(monkeypatch)
    notes = []
    assert go_vet.check(parse_diff(new_file("a.go", GO_FILE)), Worktree(str(go_module(tmp_path))), notes) == []
    assert notes == ["go vet didn't run: `go` isn't on PATH."]
    # A PR with no Go in it says nothing.
    notes = []
    assert go_vet.check(parse_diff(new_file("a.py", "x = 1\n")), Worktree(str(tmp_path)), notes) == [] and notes == []


def test_go_vet_running_out_of_time_is_a_coverage_line(monkeypatch, tmp_path):
    installed(monkeypatch, "go")
    too_slow(monkeypatch)
    monkeypatch.setattr(go_vet, "TIMEOUT_SECONDS", 7)
    notes = []
    assert go_vet.check(parse_diff(new_file("a.go", GO_FILE)), Worktree(str(go_module(tmp_path))), notes) == []
    assert notes == ["go vet took longer than 7 s, so its findings are missing from this review."]


def test_a_package_that_doesnt_build_is_a_coverage_line_not_a_finding(monkeypatch, tmp_path):
    installed(monkeypatch, "go")
    root = go_module(tmp_path)
    fake_run(monkeypatch, (1, "", "# example.com/m\nvet: a.go:7:2: undefined: Printz\n"))
    notes = []
    assert go_vet.check(parse_diff(new_file("a.go", GO_FILE)), Worktree(str(root)), notes) == []
    assert notes == ["go vet couldn't build every package of the module (`vet: a.go:7:2: undefined: Printz`), so its findings there are missing."]


def test_go_vet_needs_a_worktree_it_may_build(monkeypatch, tmp_path):
    installed(monkeypatch, "go")
    fake_run(monkeypatch)  # any run would fail: none may happen
    files = parse_diff(new_file("a.go", GO_FILE))
    notes = []
    assert go_vet.check(files, None, notes) == []
    assert notes[0].startswith("go vet didn't run: it builds the code, and this review has no checkout of the PR's head")
    notes = []
    assert go_vet.check(files, Worktree(str(tmp_path), "the PR comes from a fork."), notes) == []
    assert notes == ["go vet didn't run: the PR comes from a fork."]


def test_staticcheck_adds_its_sa_checks_when_its_installed(monkeypatch, tmp_path):
    installed(monkeypatch, "go", "staticcheck")
    root = go_module(tmp_path)
    lines = [
        {"code": "SA4017", "severity": "error", "location": {"file": str(root / "a.go"), "line": 7}, "end": {"line": 7},
         "message": "Sprintf doesn't have side effects and its return value is ignored"},
        {"code": "SA4017", "severity": "ignored", "location": {"file": str(root / "a.go"), "line": 7}, "end": {"line": 7}, "message": "ignored one"},
        {"code": "SA1019", "severity": "error", "location": {"file": str(root / "a.go"), "line": 7}, "message": "deprecated: dropped"},
        {"code": "ST1003", "severity": "error", "location": {"file": str(root / "a.go"), "line": 7}, "message": "style: never kept"},
        {"code": "SA4006", "severity": "error", "location": {"file": str(root / "a.go"), "line": 6}, "message": "an unchanged line"},
    ]
    seen = []
    fake_run(monkeypatch, (0, "", ""), (1, "\n".join(json.dumps(line) for line in lines), ""), seen=seen)
    findings = go_vet.check(parse_diff(edit("a.go", GO_FILE, 7)), Worktree(str(root)), [])
    assert [f.rule for f in findings] == ["staticcheck/SA4017", "staticcheck/SA4017"]
    assert "//lint:ignore" in findings[1].detail and "//lint:ignore" not in findings[0].detail
    assert seen[1]["command"][1:] == ["-f", "json", "-checks", "SA*,-SA1019,-SA5009", "-tests=false", "-show-ignored", "."]


def test_go_vet_runs_only_when_the_policy_turns_it_on(monkeypatch, tmp_path):
    installed(monkeypatch, "go")
    root = go_module(tmp_path)
    seen = []
    fake_run(monkeypatch, (0, vet_report(root, ("printf", "a.go", 7, "wrong type")), ""), seen=seen)
    files = parse_diff(new_file("a.go", GO_FILE))
    assert run_checks(files, Policy(), worktree=Worktree(str(root))) == [] and seen == []
    notes = []
    assert [f.rule for f in run_checks(files, GO, worktree=Worktree(str(root)), notes=notes)] == ["go-vet/printf"]
    assert Policy().checks.go_vet is False and Policy().checks.clippy is False
    assert parse_policy("checks:\n  go_vet: yes please\n").errors == ("checks.go_vet must be true or false.",)


def test_the_engine_hands_back_the_coverage_lines(monkeypatch):
    installed(monkeypatch)
    result = analyze(title="t", description="", files=parse_diff(new_file("a.go", GO_FILE) + new_file("src/lib.rs", RUST_FILE)),
                     policy=BOTH, model=None)
    assert result.check_notes == ["go vet didn't run: `go` isn't on PATH.", "Clippy didn't run: `cargo` isn't on PATH."]


# ── Clippy, with its output faked ────────────────────────────────────────


def clippy_message(path, line, code, message="equal expressions as operands to `==`", level="warning"):
    return json.dumps({"reason": "compiler-message", "message": {
        "code": {"code": code} if code else None, "level": level, "message": message,
        "spans": [{"file_name": path, "line_start": line, "line_end": line, "is_primary": True}],
    }})


def test_clippy_keeps_its_correctness_lints_on_added_lines(monkeypatch, tmp_path):
    installed(monkeypatch, "cargo")
    root = rust_crate(tmp_path)
    seen = []
    fake_run(monkeypatch, (0, "\n".join([
        clippy_message("src/lib.rs", 3, "clippy::eq_op"),
        clippy_message("src/lib.rs", 2, "clippy::eq_op", "an unchanged line"),
        clippy_message("src/lib.rs", 3, "clippy::len_zero", "style: never kept"),
        clippy_message("src/lib.rs", 3, "unused_variables", "rustc's own warning: never kept"),
        json.dumps({"reason": "build-finished", "success": True}),
    ]), ""), seen=seen)
    notes = []
    findings = clippy.check(parse_diff(edit("src/lib.rs", RUST_FILE, 3)), Worktree(str(root)), notes)
    assert [(f.rule, f.line_start, f.severity) for f in findings] == [("clippy/eq_op", 3, "high")] and notes == []
    assert findings[0].title == "Clippy eq_op: `equal expressions as operands to '=='`"
    command = seen[0]["command"]
    assert command[1:3] == ["clippy", "--manifest-path"] and "--message-format=json" in command and "--locked" not in command
    assert command[command.index("--") + 1:] == ["-A", "clippy::all", "--force-warn", "clippy::correctness", "--cap-lints", "warn"]


def test_clippy_finds_files_named_from_the_workspace_root(monkeypatch, tmp_path):
    installed(monkeypatch, "cargo")
    write(tmp_path, {"Cargo.toml": '[workspace]\nmembers = ["crates/a"]\n', "Cargo.lock": "", "crates/a/Cargo.toml": "[package]\n"})
    write(tmp_path, {"crates/a/src/lib.rs": RUST_FILE})
    seen = []
    fake_run(monkeypatch, (0, clippy_message("crates/a/src/lib.rs", 3, "clippy::eq_op"), ""), seen=seen)
    findings = clippy.check(parse_diff(new_file("crates/a/src/lib.rs", RUST_FILE)), Worktree(str(tmp_path)), [])
    assert [(f.path, f.line_start) for f in findings] == [("crates/a/src/lib.rs", 3)]
    assert "--locked" in seen[0]["command"] and seen[0]["cwd"] == str(tmp_path / "crates" / "a")


def test_a_crate_that_doesnt_build_is_a_coverage_line_not_a_finding(monkeypatch, tmp_path):
    installed(monkeypatch, "cargo")
    root = rust_crate(tmp_path)
    fake_run(monkeypatch, (101, clippy_message("src/lib.rs", 3, "E0425", "cannot find value `x` in this scope", level="error"),
                           "error: could not compile `c` (lib) due to 1 previous error\n"))
    notes = []
    assert clippy.check(parse_diff(new_file("src/lib.rs", RUST_FILE)), Worktree(str(root)), notes) == []
    assert notes == ["Clippy couldn't build the crate (`cannot find value 'x' in this scope`), so its findings there are missing."]
    # No clippy component, or a lock file that's out of date: cargo says so on stderr.
    fake_run(monkeypatch, (101, "", "error: no such command: `clippy`\n"))
    notes = []
    assert clippy.check(parse_diff(new_file("src/lib.rs", RUST_FILE)), Worktree(str(root)), notes) == []
    assert notes == ["Clippy couldn't build the crate (`error: no such command: 'clippy'`), so its findings there are missing."]


def test_without_cargo_or_in_time_clippy_says_so(monkeypatch, tmp_path):
    files = parse_diff(new_file("src/lib.rs", RUST_FILE))
    installed(monkeypatch)
    notes = []
    assert clippy.check(files, Worktree(str(rust_crate(tmp_path))), notes) == [] and notes == ["Clippy didn't run: `cargo` isn't on PATH."]
    installed(monkeypatch, "cargo")
    too_slow(monkeypatch)
    notes = []
    assert clippy.check(files, Worktree(str(tmp_path)), notes) == []
    assert notes == [f"Clippy took longer than {clippy.TIMEOUT_SECONDS} s, so its findings are missing from this review."]
    notes = []
    assert clippy.check(files, Worktree(str(tmp_path), "the PR comes from a fork."), notes) == []
    assert notes == ["Clippy didn't run: the PR comes from a fork."]


def test_a_cargo_lock_clippy_wrote_is_removed(monkeypatch, tmp_path):
    installed(monkeypatch, "cargo")
    root = rust_crate(tmp_path)

    def run(command, *, cwd, timeout, name, env=None):
        (root / "Cargo.lock").write_text("written by cargo")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(tools, "run", run)
    clippy.check(parse_diff(new_file("src/lib.rs", RUST_FILE)), Worktree(str(root)), [])
    assert not (root / "Cargo.lock").exists()
    (root / "Cargo.lock").write_text("the project's own")
    clippy.check(parse_diff(new_file("src/lib.rs", RUST_FILE)), Worktree(str(root)), [])
    assert (root / "Cargo.lock").read_text() == "written by cargo"  # one that was there stays (cargo may update it)


def test_past_the_limit_of_crates_the_rest_are_named(monkeypatch, tmp_path):
    installed(monkeypatch, "cargo")
    monkeypatch.setattr(clippy, "MAX_CRATES", 1)
    for name in ("a", "b"):
        rust_crate(tmp_path / name)
    seen = []
    fake_run(monkeypatch, (0, "", ""), seen=seen)
    notes = []
    clippy.check(parse_diff(new_file("a/src/lib.rs", RUST_FILE) + new_file("b/src/lib.rs", RUST_FILE)), Worktree(str(tmp_path)), notes)
    assert len(seen) == 1 and notes == ["Not read by Clippy, past its limit of 1 crates: `b/src/lib.rs`"]


# ── where the code may be built ──────────────────────────────────────────


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def committed(root: Path) -> str:
    git(root, "init", "-q")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "x")
    return git(root, "rev-parse", "HEAD")


def test_a_pr_from_a_fork_is_never_built(tmp_path):
    head = committed(go_module(tmp_path))
    gh = FakeGitHub(diff=new_file("a.go", GO_FILE), head=head)
    ctx = Context(gh=gh, policy=GO, model=None, bot="stratum[bot]", head_checkout=str(tmp_path))
    pull = gh.pull(7)
    pull["head"]["repo"] = {"full_name": "mallory/widget"}
    refused = worktree_for(ctx, pull)
    assert refused is not None and refused.refused.startswith("the PR comes from a fork")
    pull["head"]["repo"] = None  # a fork since deleted
    assert worktree_for(ctx, pull).refused.startswith("the PR comes from a fork")
    pull["head"]["repo"] = {"full_name": "acme/widget"}
    assert worktree_for(ctx, pull) == Worktree(str(tmp_path))


def test_a_checkout_that_isnt_at_the_prs_head_is_refused(tmp_path):
    committed(go_module(tmp_path))
    gh = FakeGitHub(diff=new_file("a.go", GO_FILE), head="b" * 40)
    gh.pull(7)["head"]["repo"] = {"full_name": "acme/widget"}
    ctx = Context(gh=gh, policy=GO, model=None, bot="stratum[bot]", head_checkout=str(tmp_path))
    assert "not at its head bbbbbbb" in worktree_for(ctx, gh.pull(7)).refused


def test_only_a_branch_positively_in_this_repo_is_built(tmp_path):
    head = committed(go_module(tmp_path))
    gh = FakeGitHub(diff=new_file("a.go", GO_FILE), head=head)
    ctx = Context(gh=gh, policy=GO, model=None, bot="stratum[bot]", head_checkout=str(tmp_path))
    pull = gh.pull(7)
    pull["head"].pop("repo", None)  # a pull without the field fails closed
    assert worktree_for(ctx, pull).refused.startswith("the PR comes from a fork")
    pull["head"]["repo"] = {}
    assert worktree_for(ctx, pull).refused.startswith("the PR comes from a fork")
    pull["head"]["repo"] = {"full_name": "ACME/Widget"}
    assert worktree_for(ctx, pull) == Worktree(str(tmp_path))
    # The refusal, posted on the PR, doesn't send anyone off to build it elsewhere.
    pull["head"]["repo"] = {"full_name": "mallory/widget"}
    assert "--local" not in worktree_for(ctx, pull).refused


def test_a_bots_pr_is_built_only_when_the_policy_allows_it(tmp_path):
    head = committed(go_module(tmp_path))
    gh = FakeGitHub(diff=new_file("a.go", GO_FILE), head=head, author="dependabot[bot]")
    pull = gh.pull(7)
    pull["head"]["repo"] = {"full_name": "acme/widget"}
    ctx = Context(gh=gh, policy=GO, model=None, bot="stratum[bot]", head_checkout=str(tmp_path))
    assert "the PR is by a bot (dependabot[bot])" in worktree_for(ctx, pull).refused
    pull["user"] = {"login": "renovate", "type": "Bot"}
    assert "the PR is by a bot (renovate)" in worktree_for(ctx, pull).refused
    # Only a positive match is a person: anything else is refused like a bot.
    for user in (None, {}, {"login": "carol"}, {"login": "acme", "type": "Organization"}, {"login": "Dependabot[BOT]", "type": "bot"},
                 {"login": "helper[bot]", "type": "User"}):
        pull["user"] = user
        assert worktree_for(ctx, pull).refused.startswith("the PR is by a bot"), user
    pull["user"] = {"login": "carol", "type": "User"}
    assert worktree_for(ctx, pull) == Worktree(str(tmp_path))
    pull["user"] = {"login": "renovate", "type": "Bot"}
    allowed = parse_policy("checks:\n  go_vet: true\n  build_bot_prs: true\n")
    assert Policy().checks.build_bot_prs is False and allowed.checks.build_bot_prs is True
    ctx = Context(gh=gh, policy=allowed, model=None, bot="stratum[bot]", head_checkout=str(tmp_path))
    assert worktree_for(ctx, pull) == Worktree(str(tmp_path))


def test_the_early_post_refuses_a_fork_too(monkeypatch, tmp_path):
    from test_early_checks import GitHub as EarlyGitHub

    from stratum_open.runner import post_early

    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    installed(monkeypatch, "go")
    fake_run(monkeypatch)  # nothing may be built
    head = committed(go_module(tmp_path))
    gh = EarlyGitHub(diff=new_file("a.go", GO_FILE), head=head)
    gh.pull(7)["head"]["repo"] = {"full_name": "mallory/widget"}
    policy = parse_policy("review:\n  early_checks: true\nchecks:\n  go_vet: true\n")
    post_early(Context(gh=gh, policy=policy, model=None, bot="stratum[bot]", head_checkout=str(tmp_path), log=lambda line: None), 7)
    assert "go vet didn't run: the PR comes from a fork" in gh.issue_comments_[7][-1]["body"]


def test_a_build_never_gets_the_jobs_tokens(monkeypatch, tmp_path):
    for key, value in {"STRATUM_GITHUB_TOKEN": "s1", "STRATUM_MODEL_API_KEY": "s2", "GITHUB_TOKEN": "s3", "GH_TOKEN": "s4",
                       "ACTIONS_RUNTIME_TOKEN": "s5", "INPUT_GITHUB-TOKEN": "s6", "GOPATH": "/go", "HOME": "/home/x",
                       "GITHUB_ENV": "s7", "GITHUB_PATH": "s8", "GITHUB_OUTPUT": "s9", "GITHUB_REPOSITORY": "acme/widget"}.items():
        monkeypatch.setenv(key, value)
    env = tools.build_env()
    assert not {"s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"} & set(env.values())
    assert env["GITHUB_REPOSITORY"] == "acme/widget"  # what isn't a way to reach the job's token stays
    assert env["GOPATH"] == "/go" and env["HOME"] == "/home/x"
    seen = []
    installed(monkeypatch, "go", "staticcheck", "cargo")
    fake_run(monkeypatch, (0, "", ""), (0, "", ""), (0, "", ""), seen=seen)
    go_vet.check(parse_diff(new_file("a.go", GO_FILE)), Worktree(str(go_module(tmp_path / "g"))), [])
    clippy.check(parse_diff(new_file("src/lib.rs", RUST_FILE)), Worktree(str(rust_crate(tmp_path / "r"))), [])
    assert len(seen) == 3 and all(s["env"] is not None and "s1" not in s["env"].values() and "GOPATH" in s["env"] for s in seen)


def test_failure_reasons_from_the_tools_stay_inside_a_code_span(monkeypatch, tmp_path):
    installed(monkeypatch, "cargo")
    root = rust_crate(tmp_path / "r")
    fake_run(monkeypatch, (101, clippy_message("src/lib.rs", 1, None, "@acme/team see [x](https://e.example) `now`", level="error"), ""))
    notes = []
    clippy.check(parse_diff(new_file("src/lib.rs", RUST_FILE)), Worktree(str(root)), notes)
    assert notes == ["Clippy couldn't build the crate (`@acme/team see [x](https://e.example) 'now'`), so its findings there are missing."]
    installed(monkeypatch, "go")
    fake_run(monkeypatch, (1, "", "vet: a.go:1:1: @acme/team [x](https://e.example)\n"))
    notes = []
    go_vet.check(parse_diff(new_file("a.go", GO_FILE)), Worktree(str(go_module(tmp_path / "g"))), notes)
    assert "(`vet: a.go:1:1: @acme/team [x](https://e.example)`)" in notes[0]


def test_staticcheck_that_doesnt_run_says_so(monkeypatch, tmp_path):
    installed(monkeypatch, "go", "staticcheck")
    root = go_module(tmp_path)
    files = parse_diff(new_file("a.go", GO_FILE))
    for code, stdout, stderr in ((2, "", ""), (1, "", "staticcheck: module requires at least go1.27\n")):
        fake_run(monkeypatch, (0, "", ""), (code, stdout, stderr))
        notes = []
        go_vet.check(files, Worktree(str(root)), notes)
        assert len(notes) == 1 and notes[0].startswith("staticcheck didn't run on the module (") and notes[0].endswith("so its findings are missing.")
    assert "`staticcheck: module requires at least go1.27`" in notes[0]
    fake_run(monkeypatch, (0, "", ""), (0, "", ""))  # ran and found nothing
    notes = []
    go_vet.check(files, Worktree(str(root)), notes)
    assert notes == []


class FakeProcess:
    """A Popen whose communicate() answers, call by call, from `steps`:
    an exception to raise, or (stdout, stderr)."""

    def __init__(self, steps):
        self.steps, self.pid, self.returncode, self.waits, self.killed = list(steps), 4242, None, [], False

    def __call__(self, command, **kwargs):
        self.command = command
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def kill(self):
        self.killed = True


def test_ctrl_c_stops_the_tool_and_what_it_started(monkeypatch, tmp_path):
    process = FakeProcess([KeyboardInterrupt(), ("", "")])
    stopped = []
    monkeypatch.setattr(tools.subprocess, "Popen", process)
    monkeypatch.setattr(tools, "stop_tree", stopped.append)
    with pytest.raises(KeyboardInterrupt):
        tools.run(["go", "vet"], cwd=str(tmp_path), timeout=30, name="go vet")
    assert stopped == [process] and process.waits == [30, tools.KILL_WAIT_SECONDS]


def test_the_wait_after_a_kill_has_a_limit(monkeypatch, tmp_path):
    # The tool times out, and after the kill its pipes stay open (a process that left the group holds them).
    process = FakeProcess([subprocess.TimeoutExpired("go", 5), subprocess.TimeoutExpired("go", 10)])
    monkeypatch.setattr(tools.subprocess, "Popen", process)
    monkeypatch.setattr(tools, "stop_tree", lambda p: None)
    with pytest.raises(tools.Skipped, match="took longer than 5 s"):
        tools.run(["go", "vet"], cwd=str(tmp_path), timeout=5, name="go vet")
    assert process.waits == [5, tools.KILL_WAIT_SECONDS]


def test_taskkill_is_run_by_its_full_path(monkeypatch):
    commands = []
    monkeypatch.setattr(tools.os, "name", "nt")
    monkeypatch.setenv("SystemRoot", os.path.join(os.sep, "Win"))
    monkeypatch.setattr(tools.subprocess, "run", lambda command, **kwargs: commands.append(command))
    process = FakeProcess([])
    tools.stop_tree(process)
    assert commands[0][0] == os.path.join(os.sep, "Win", "System32", "taskkill.exe")
    assert commands[0][1:] == ["/F", "/T", "/PID", "4242"] and process.killed


def test_review_local_says_when_it_cant_read_git_status(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_git_or_none", lambda *args: None)
    refused = cli._local_worktree(GO, parse_diff(new_file("a.go", GO_FILE)), str(tmp_path)).refused
    assert refused == "Stratum couldn't read `git status`, so it can't tell whether the checkout is the HEAD it reviews."


def test_vendored_code_is_left_alone():
    def one(path):
        return parse_diff(new_file(path, "x\n"))[0]

    assert not go_vet.wanted(one("vendor/github.com/x/y/y.go")) and not go_vet.wanted(one("svc/vendor/golang.org/x/z.go"))
    assert not clippy.wanted(one("vendor/serde/src/lib.rs"))
    assert go_vet.wanted(one("pkg/vendors/v.go")) and go_vet.wanted(one("vendor.go")) and clippy.wanted(one("src/vendor.rs"))


def test_no_checkout_or_no_check_that_builds_means_no_worktree(tmp_path):
    gh = FakeGitHub(diff=new_file("a.go", GO_FILE))
    assert worktree_for(Context(gh=gh, policy=GO, model=None, bot="b"), gh.pull(7)) is None
    assert worktree_for(Context(gh=gh, policy=Policy(), model=None, bot="b", head_checkout=str(tmp_path)), gh.pull(7)) is None


def test_the_action_builds_nothing_unless_the_workflow_passes_a_checkout(tmp_path):
    gh = FakeGitHub(diff="")
    assert _context({}, gh, GO, None, "main").head_checkout is None
    assert _context({"STRATUM_HEAD_CHECKOUT": "  "}, gh, GO, None, "main").head_checkout is None
    assert _context({"STRATUM_HEAD_CHECKOUT": str(tmp_path)}, gh, GO, None, "main").head_checkout == os.path.abspath(str(tmp_path))


def test_the_summary_says_why_go_vet_didnt_run_on_a_fork(monkeypatch, tmp_path):
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    installed(monkeypatch, "go")
    fake_run(monkeypatch)  # nothing may be built
    head = committed(go_module(tmp_path))
    gh = FakeGitHub(diff=new_file("a.go", GO_FILE), head=head)
    gh.pull(7)["head"]["repo"] = {"full_name": "mallory/widget"}
    review_pull(Context(gh=gh, policy=GO, model=None, bot="stratum[bot]", head_checkout=str(tmp_path), log=lambda line: None), 7)
    assert "go vet didn't run: the PR comes from a fork" in gh.issue_comments_[7][-1]["body"]


def test_a_pr_from_this_repo_is_built_and_reported(monkeypatch, tmp_path):
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    installed(monkeypatch, "go")
    root = go_module(tmp_path)
    head = committed(root)
    fake_run(monkeypatch, (0, vet_report(root, ("printf", "a.go", 7, "fmt.Printf format %d has arg s of wrong type string")), ""))
    gh = FakeGitHub(diff=new_file("a.go", GO_FILE), head=head)
    gh.pull(7)["head"]["repo"] = {"full_name": "acme/widget"}
    outcome = review_pull(Context(gh=gh, policy=GO, model=None, bot="stratum[bot]", head_checkout=str(tmp_path), log=lambda line: None), 7)
    assert "go-vet/printf" in {f.rule for f in outcome.findings}


def test_review_local_refuses_a_checkout_with_uncommitted_changes(tmp_path, capsys):
    root = write(go_module(tmp_path), {"b.go": "package m\n\nvar value = 1\n"})
    committed(root)
    files = parse_diff(new_file("a.go", GO_FILE))
    assert cli._local_worktree(GO, files, str(root)) == Worktree(str(root))
    assert "Building this checkout for go vet, which runs its code" in capsys.readouterr().err
    assert cli._local_worktree(Policy(), files, str(root)) is None
    # A file the PR doesn't change still goes into the build, so its change counts too.
    (root / "b.go").write_text('package m\n\nvar value = "one"\n', encoding="utf-8")
    assert "`b.go` has changes that aren't committed" in cli._local_worktree(GO, files, str(root)).refused
    git(root, "checkout", "--", "b.go")
    (root / "c.go").write_text("package m\n", encoding="utf-8")  # one git doesn't know of yet
    assert "`c.go` has changes that aren't committed" in cli._local_worktree(GO, files, str(root)).refused
    assert capsys.readouterr().err == ""  # nothing built, so no warning


def test_review_staged_builds_nothing(tmp_path, capsys):
    root = go_module(tmp_path)
    committed(root)
    refused = cli._local_worktree(GO, parse_diff(new_file("a.go", GO_FILE)), str(root), staged=True).refused
    assert refused.startswith("a review of what's staged doesn't build the code")
    assert capsys.readouterr().err == ""


def test_a_tool_past_its_time_limit_is_stopped_with_what_it_started(tmp_path):
    marker = tmp_path / "child-still-running"
    child = f"import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('x')"
    parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
    with pytest.raises(tools.Skipped, match="took longer than 1 s"):
        tools.run([sys.executable, "-c", parent], cwd=str(tmp_path), timeout=1, name="the tool")
    time.sleep(4)
    assert not marker.exists()
    assert tools.run([sys.executable, "-c", "print('ok')"], cwd=str(tmp_path), timeout=30, name="x").stdout.strip() == "ok"


# ── the real tools ───────────────────────────────────────────────────────


@needs_go
def test_real_go_vet_reports_a_printf_mistake_on_an_added_line(tmp_path):
    root = go_module(tmp_path)
    notes = []
    findings = go_vet.check(parse_diff(edit("a.go", GO_FILE, 7)), Worktree(str(root)), notes)
    assert ("go-vet/printf", 7) in [(f.rule, f.line_start) for f in findings], notes
    # The same mistake on a line the PR didn't add: nothing.
    assert go_vet.check(parse_diff(edit("a.go", GO_FILE, 6)), Worktree(str(root)), []) == []


@needs_go
def test_real_go_vet_on_code_that_doesnt_build(tmp_path):
    root = go_module(tmp_path, text="package m\n\nfunc F() int {\n\treturn missing\n}\n")
    notes = []
    assert go_vet.check(parse_diff(new_file("a.go", "package m\n\nfunc F() int {\n\treturn missing\n}\n")), Worktree(str(root)), notes) == []
    assert len(notes) == 1 and notes[0].startswith("go vet couldn't build every package of the module (") and "missing" in notes[0]


@needs_clippy
def test_real_clippy_reports_a_correctness_lint_on_an_added_line(tmp_path):
    root = rust_crate(tmp_path, text="#![allow(clippy::eq_op)]\n" + RUST_FILE)
    notes = []
    findings = clippy.check(parse_diff(edit("src/lib.rs", "#![allow(clippy::eq_op)]\n" + RUST_FILE, 4)), Worktree(str(root)), notes)
    # The crate's own `allow` doesn't hide it.
    assert [(f.rule, f.line_start) for f in findings] == [("clippy/eq_op", 4)], notes
    assert not (root / "Cargo.lock").exists()


@needs_clippy
def test_real_clippy_on_code_that_doesnt_build(tmp_path):
    text = "pub fn f() -> i32 {\n    missing\n}\n"
    root = rust_crate(tmp_path, text=text)
    notes = []
    assert clippy.check(parse_diff(new_file("src/lib.rs", text)), Worktree(str(root)), notes) == []
    assert len(notes) == 1 and notes[0].startswith("Clippy couldn't build the crate (`cannot find value 'missing'")
