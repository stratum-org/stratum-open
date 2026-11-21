"""benchmark/build_checks.py: the go vet and Clippy checks on merged PRs,
each checked out at its head and built. The programs' output is faked here,
except in the test that runs the real go vet on a two-line module."""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_checks_build import GO_FILE, RUST_FILE, clippy_message, edit, fake_run, go_module, installed, needs_go, new_file, too_slow, vet_report, write

from stratum_open.checks import clippy, go_vet, tools
from stratum_open.diff import parse_diff

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmark"))

import bench  # noqa: E402
import build_checks  # noqa: E402

PRINTF = 'fmt.Printf("%d\\n", s)'  # line 7 of GO_FILE


def test_every_issue_go_vet_reports_on_an_added_line_is_written_kept_or_not(monkeypatch, tmp_path):
    installed(monkeypatch, "go")
    root = go_module(tmp_path)
    fake_run(monkeypatch, (0, vet_report(root, ("printf", "a.go", 7, "fmt.Printf format %d has arg s of wrong type string"),
                                         ("printf", "a.go", 6, "on a line the PR didn't add"),
                                         ("composites", "a.go", 7, "image.Point struct literal uses unkeyed fields")), ""))
    files = parse_diff(edit("a.go", GO_FILE, 7))
    result = build_checks.measure(str(root), files)
    rows = build_checks.issues(str(root), files, result["calls"])
    assert [(r["tool"], r["code"], r["path"], r["line"], r["text"], r["kept"]) for r in rows] == [
        ("go vet", "printf", "a.go", 7, PRINTF, "yes"), ("go vet", "composites", "a.go", 7, PRINTF, "no")]
    # What it keeps is what the check itself reports.
    assert [f.rule for f in result["findings"]] == ["go-vet/printf"]
    assert list(result["seconds"]) == ["go vet"] and result["notes"] == []


def test_staticcheck_runs_with_every_sa_check_while_the_check_still_leaves_the_dropped_ones_out(monkeypatch, tmp_path):
    installed(monkeypatch, "go", "staticcheck")
    root = go_module(tmp_path)
    reported = [{"code": code, "severity": "error", "location": {"file": str(root / "a.go"), "line": 7}, "end": {"line": 7}, "message": message}
                for code, message in (("SA1019", "fmt.X is deprecated"), ("SA4017", "Sprintf doesn't have side effects"))]
    seen = []
    fake_run(monkeypatch, (0, "", ""), (1, "\n".join(map(json.dumps, reported)), ""), seen=seen)
    files = parse_diff(edit("a.go", GO_FILE, 7))
    result = build_checks.measure(str(root), files)
    command = seen[1]["command"]
    assert command[command.index("-checks") + 1] == "SA*"
    rows = build_checks.issues(str(root), files, result["calls"])
    assert [(r["tool"], r["code"], r["level"], r["kept"]) for r in rows] == [("staticcheck", "SA1019", "error", "no"), ("staticcheck", "SA4017", "error", "yes")]
    assert [f.rule for f in result["findings"]] == ["staticcheck/SA4017"]


def test_clippy_also_reports_its_suspicious_lints_found_by_their_name_from_the_workspace_root(monkeypatch, tmp_path):
    installed(monkeypatch, "cargo")
    write(tmp_path, {"Cargo.toml": '[workspace]\nmembers = ["crates/a"]\n', "Cargo.lock": "", "crates/a/Cargo.toml": "[package]\n",
                     "crates/a/src/lib.rs": RUST_FILE})
    path = "crates/a/src/lib.rs"
    seen = []
    fake_run(monkeypatch, (0, "\n".join([
        clippy_message(path, 3, "clippy::eq_op"),
        clippy_message(path, 3, "clippy::suspicious_else_formatting", "a suspicious lint, which the check doesn't keep"),
        clippy_message(path, 3, "unused_variables", "rustc's own warning, not Clippy's"),
        clippy_message(path, 1, "clippy::eq_op", "a line the PR didn't add"),
    ]), ""), seen=seen)
    files = parse_diff(edit(path, RUST_FILE, 3))
    result = build_checks.measure(str(tmp_path), files)
    command = seen[0]["command"]
    assert command[command.index("--") + 1:] == ["-A", "clippy::all", "--force-warn", "clippy::correctness",
                                                 "--force-warn", "clippy::suspicious", "--cap-lints", "warn"]
    rows = build_checks.issues(str(tmp_path), files, result["calls"])
    assert [(r["tool"], r["code"], r["path"], r["line"], r["kept"]) for r in rows] == [
        ("clippy", "eq_op", path, 3, "yes"), ("clippy", "suspicious_else_formatting", path, 3, "no")]
    assert [f.rule for f in result["findings"]] == ["clippy/eq_op"] and list(result["seconds"]) == ["Clippy"]


def test_only_the_files_under_a_module_a_program_ran_in_count_as_read(monkeypatch, tmp_path):
    installed(monkeypatch, "go")
    monkeypatch.setattr(go_vet, "MAX_MODULES", 1)
    write(tmp_path, {"a/go.mod": "module a\n", "b/go.mod": "module b\n"})
    files = parse_diff(new_file("a/x.go", GO_FILE) + new_file("a/y.go", GO_FILE) + new_file("b/z.go", GO_FILE) + new_file("tools/t.go", GO_FILE))
    fake_run(monkeypatch, (0, "", ""))
    result = build_checks.measure(str(tmp_path), files)
    # b is past the limit of one module, and tools/ has no go.mod above it.
    assert [f.path for f in build_checks.read_by(str(tmp_path), files, "go vet", result["calls"])] == ["a/x.go", "a/y.go"]
    for call in result["calls"]:
        call["timed_out"] = True
    assert build_checks.read_by(str(tmp_path), files, "go vet", result["calls"]) == []
    assert build_checks.read_by(str(tmp_path), files, "Clippy", result["calls"]) == []


def test_the_checks_get_the_runs_time_limit_and_their_own_back_after(monkeypatch, tmp_path):
    installed(monkeypatch, "go", "cargo")
    root = write(go_module(tmp_path), {"Cargo.toml": "[package]\n", "src/lib.rs": RUST_FILE})
    files = parse_diff(new_file("a.go", GO_FILE) + new_file("src/lib.rs", RUST_FILE))
    limits = go_vet.TIMEOUT_SECONDS, clippy.TIMEOUT_SECONDS
    seen = []
    fake_run(monkeypatch, (0, "", ""), (0, "", ""), seen=seen)
    fake = tools.run
    build_checks.measure(str(root), files, timeout=5000)
    assert [s["timeout"] > max(limits) for s in seen] == [True, True]
    assert tools.run is fake and (go_vet.TIMEOUT_SECONDS, clippy.TIMEOUT_SECONDS) == limits


def test_a_program_past_the_runs_time_limit_is_noted_as_the_check_notes_it(monkeypatch, tmp_path):
    installed(monkeypatch, "go", "cargo")
    root = write(go_module(tmp_path), {"Cargo.toml": "[package]\n", "src/lib.rs": RUST_FILE})
    files = parse_diff(new_file("a.go", GO_FILE) + new_file("src/lib.rs", RUST_FILE))
    limits = go_vet.TIMEOUT_SECONDS, clippy.TIMEOUT_SECONDS
    too_slow(monkeypatch)
    real = tools.run
    result = build_checks.measure(str(root), files, timeout=7)
    assert result["notes"] == ["go vet took longer than 7 s, so its findings are missing from this review.",
                               "Clippy took longer than 7 s, so its findings are missing from this review."]
    assert [call["timed_out"] for call in result["calls"]] == [True, True]
    assert tools.run is real and (go_vet.TIMEOUT_SECONDS, clippy.TIMEOUT_SECONDS) == limits


# ── the clones ───────────────────────────────────────────────────────────


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(root: Path, files: dict[str, str]) -> str:
    write(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "x")
    return git(root, "rev-parse", "HEAD")


def origin_with_prs(root: Path, heads: dict[int, dict[str, str]]) -> dict[int, str]:
    """A repository whose PRs' heads are reachable only as `refs/pull/N/head`,
    like a squash-merged PR's on GitHub."""
    root.mkdir()
    git(root, "-c", "init.defaultBranch=main", "init", "-q")
    commit(root, {"go.mod": "module example.com/m\n\ngo 1.21\n"})
    shas = {}
    for number, files in heads.items():
        git(root, "checkout", "-q", "-b", f"pr{number}", "main")
        shas[number] = commit(root, files)
        git(root, "update-ref", f"refs/pull/{number}/head", shas[number])
        git(root, "checkout", "-q", "main")
        git(root, "branch", "-q", "-D", f"pr{number}")
    return shas


def test_each_pr_is_checked_out_at_its_head_with_nothing_left_from_the_one_before(monkeypatch, tmp_path):
    shas = origin_with_prs(tmp_path / "origin", {1: {"a.go": "package one\n"}, 2: {"b.go": "package two\n"}})
    monkeypatch.setattr(build_checks, "SOURCE", str(tmp_path / "origin"))
    where = build_checks.clone(tmp_path / "repos", "acme/widget")
    assert where == tmp_path / "repos" / "acme__widget"
    build_checks.fetch_heads(where, [1, 3, 2])  # 3 has no ref: the others are still fetched
    assert build_checks.check_out(where, shas[1]) and (where / "a.go").read_text() == "package one\n"
    (where / "built.txt").write_text("left by a build")
    assert build_checks.check_out(where, shas[2]) and (where / "b.go").exists()
    assert not (where / "a.go").exists() and not (where / "built.txt").exists()
    assert build_checks.clone(tmp_path / "repos", "acme/widget") == where  # cloned once
    assert not build_checks.check_out(where, "0" * 40)


def test_run_writes_every_issue_and_the_build_times_and_keeps_the_marks(monkeypatch, tmp_path, capsys):
    shas = origin_with_prs(tmp_path / "origin", {5: {"a.go": GO_FILE}})
    monkeypatch.setattr(build_checks, "SOURCE", str(tmp_path / "origin"))
    monkeypatch.setattr(bench, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(build_checks, "prepare", lambda worktree, files: ({"go": "go version go1.26.3 linux/amd64"}, "", 0.5))
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "acme__widget__5.json").write_text(json.dumps({"pull": {"title": "t"}, "diff": edit("a.go", GO_FILE, 7)}))
    installed(monkeypatch, "go")
    worktree = tmp_path / "repos" / "acme__widget"
    output = vet_report(worktree, ("printf", "a.go", 7, "wrong type"), ("composites", "a.go", 7, "unkeyed fields"))
    prs, out = [{"repo": "acme/widget", "pr": 5, "head": shas[5]}], tmp_path / "run"
    seen = []
    fake_run(monkeypatch, (0, output, ""), seen=seen)
    monkeypatch.chdir(tmp_path)
    build_checks._run(prs, Path("repos"), out, 60)  # a relative folder, as in the README's command
    assert Path(seen[0]["cwd"]).is_absolute() and Path(seen[0]["cwd"]).samefile(worktree)
    rows = read(out / "issues.csv")
    assert [(r["case"], r["code"], r["kept"]) for r in rows] == [("acme/widget#5", "printf", "yes"), ("acme/widget#5", "composites", "no")]
    assert [(r["case"], r["rule"], r["line"]) for r in read(out / "kept.csv")] == [("acme/widget#5", "go-vet/printf", "7")]
    listed = json.loads((out / "prs.json").read_text())
    assert listed[0]["files"] == {"go vet": 1, "Clippy": 0} and listed[0]["lines"] == {"go vet": 1, "Clippy": 0}
    assert list(listed[0]["seconds"]) == ["go vet"] and list(listed[0]["programs"]) == ["go vet"]
    assert listed[0]["order"] == 0 and listed[0]["toolchains"] == {"go": "go version go1.26.3 linux/amd64"} and listed[0]["error"] == ""
    # Each program run, with its exit code and what it built, and the disk after the PR.
    assert [(r["program"], r["folder"], r["code"], r["built"], r["timed_out"]) for r in listed[0]["runs"]] == [("go vet", ".", 0, 1, False)]
    assert set(listed[0]["disk"]) == {"free_mb", "clone_mb", "target_mb"} and listed[0]["disk"]["free_mb"] > 0
    printed = capsys.readouterr().out
    assert "acme/widget#5: 2 issue(s) on added lines, 1 kept; go vet 0 s, go vet built 1\n" in printed
    assert "\nacme/widget: " in printed and " MB free; the clone is " in printed
    # The marks stay when it's run again.
    rows[1]["verdict"], rows[1]["note"] = "wrong", "a style matter"
    bench._write(out / "issues.csv", build_checks.ISSUE_COLUMNS, rows)
    fake_run(monkeypatch, (0, output, ""))
    build_checks._run(prs, Path("repos"), out, 60)
    assert [(r["verdict"], r["note"]) for r in read(out / "issues.csv")] == [("", ""), ("wrong", "a style matter")]


def read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def call(name, stdout="", stderr="", code=0, cwd=".", timed_out=False):
    return {"name": name, "cwd": cwd, "code": code, "stdout": stdout, "stderr": stderr, "seconds": 1.5, "timed_out": timed_out}


def test_each_run_keeps_its_exit_code_its_error_and_what_it_built(tmp_path):
    artifact = '{"reason": "compiler-artifact", "package_id": "%s", "fresh": %s}'
    cargo = "\n".join([artifact % ("anstyle", "false"), artifact % ("clap_lex", "true"), artifact % ("clap", "false"),
                       clippy_message("src/lib.rs", 3, "clippy::eq_op"), '{"reason": "build-finished", "success": true}'])
    vet = '{}\n{\n\t"example.com/m/sub": {}\n}\n'
    calls = [call("Clippy", cargo, "warning: unused manifest key\n", cwd=str(tmp_path / "crates" / "a")),
             call("go vet", vet, "# example.com/m/sub\nvet: sub/b.go:5:2: undefined: Printz\n", code=1, cwd=str(tmp_path)),
             call("staticcheck", "", "", cwd=str(tmp_path)),
             call("Clippy", "", "", code=None, cwd=str(tmp_path), timed_out=True)]
    assert build_checks.runs(str(tmp_path), calls) == [
        {"program": "Clippy", "folder": "crates/a", "code": 0, "stderr": "warning: unused manifest key", "seconds": 1.5, "timed_out": False,
         "built": 3, "compiled": 2},
        # go vet's `# package` lines aren't its error.
        {"program": "go vet", "folder": ".", "code": 1, "stderr": "vet: sub/b.go:5:2: undefined: Printz", "seconds": 1.5, "timed_out": False,
         "built": 2},
        {"program": "staticcheck", "folder": ".", "code": 0, "stderr": "", "seconds": 1.5, "timed_out": False},
        {"program": "Clippy", "folder": ".", "code": None, "stderr": "", "seconds": 1.5, "timed_out": True, "built": 0, "compiled": 0},
    ]
    # Only a run that finished having built nothing built nothing.
    assert [build_checks.built_nothing(run) for run in build_checks.runs(str(tmp_path), calls)] == [False, False, False, False]
    assert build_checks.built_nothing(build_checks.runs(str(tmp_path), [call("Clippy", '{"reason": "build-finished"}')])[0])
    # Before Go 1.24 or so the report went to stderr, which isn't the error either.
    old_go = build_checks.runs(str(tmp_path), [call("go vet", "", "# example.com/m\n{}\n", cwd=str(tmp_path))])
    assert (old_go[0]["stderr"], old_go[0]["built"]) == ("", 1)


def test_the_disk_numbers_are_the_free_space_and_the_clone_and_its_target_folder(tmp_path):
    clone = tmp_path / "acme__widget"
    write(clone, {"a.go": "x" * 2_000_000})
    write(tmp_path / "acme__widget.target", {"debug/a.rmeta": "y" * 3_000_000, "debug/b.rmeta": "z" * 500_000})
    space = build_checks.disk(clone)
    assert (space["clone_mb"], space["target_mb"]) == (2.0, 3.5) and space["free_mb"] > 0
    assert build_checks.disk(tmp_path / "acme__widget")["target_mb"] == 3.5
    assert build_checks.size(tmp_path / "nothing here") == 0


def test_only_the_projects_asked_for_are_run():
    lists = build_checks.PRS
    every = build_checks.picked(lists, [])
    assert {item["repo"] for item in every} >= {"gin-gonic/gin", "nushell/nushell"} and len(every) == 120
    assert build_checks.picked(lists, ["all"]) == every
    assert {item["repo"] for item in build_checks.picked(lists, ["cli/cli"])} == {"cli/cli"}
    with pytest.raises(SystemExit, match="Not in the lists: acme/widget"):
        build_checks.picked(lists, ["acme/widget"])


def test_fetch_leaves_out_a_pr_github_no_longer_has(monkeypatch, tmp_path, capsys):
    from stratum_open.github.api import GitHubError

    lists = tmp_path / "prs.json"
    lists.write_text(json.dumps([{"repo": "acme/widget", "pr": n, "head": "a" * 40} for n in (1, 2, 3)]))
    fetched = []

    def fetch(repo, number):
        if number == 2:
            raise GitHubError(404, "Not Found")  # a PR GitHub has deleted
        fetched.append(number)
        return {}, ""

    monkeypatch.setattr(build_checks, "_fetch", fetch)
    monkeypatch.setattr(sys, "argv", ["build_checks.py", "fetch", "--prs", str(lists)])
    assert build_checks.main() == 0 and fetched == [1, 3]
    assert "Fetched 2 PR(s); GitHub has no acme/widget#2 any more" in capsys.readouterr().out
    # Anything else GitHub answers still stops it.
    monkeypatch.setattr(build_checks, "_fetch", lambda repo, number: (_ for _ in ()).throw(GitHubError(500, "Server Error")))
    with pytest.raises(GitHubError):
        build_checks.main()


def test_run_lists_a_pr_whose_diff_wasnt_fetched_and_builds_the_rest(monkeypatch, tmp_path):
    shas = origin_with_prs(tmp_path / "origin", {5: {"a.go": GO_FILE}})
    monkeypatch.setattr(build_checks, "SOURCE", str(tmp_path / "origin"))
    monkeypatch.setattr(bench, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(build_checks, "prepare", lambda worktree, files: ({}, "", 0.0))
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "acme__widget__5.json").write_text(json.dumps({"pull": {"title": "t"}, "diff": edit("a.go", GO_FILE, 7)}))
    installed(monkeypatch, "go")
    fake_run(monkeypatch, (0, "{}\n", ""))
    out = tmp_path / "run"
    build_checks._run([{"repo": "acme/widget", "pr": 4, "head": "b" * 40}, {"repo": "acme/widget", "pr": 5, "head": shas[5]}],
                      tmp_path / "repos", out, 60)
    gone, built = json.loads((out / "prs.json").read_text())
    assert gone["skipped"] and gone["error"].startswith("its diff isn't in the cache: GitHub has no such PR any more")
    assert "skipped" not in built and built["seconds"] and built["runs"][0]["built"] == 1
    build_checks._score(out)
    text = (out / "summary.md").read_text(encoding="utf-8")
    assert text.count("1 merged PRs from 1 project,") == 1 and "1 more couldn't be read" in text
    assert "| acme/widget#4 | its diff isn't in the cache" in text
    # The first PR built is the one with nothing cached, whatever came before it.
    assert "| acme/widget | go vet | 1 | " in text


# ── the summary ──────────────────────────────────────────────────────────


def pr(repo, number, order, seconds, notes=(), lines=None, runs=(), disk=None):
    return {"repo": repo, "pr": number, "head": "a" * 40, "order": order, "error": "", "seconds": seconds,
            "lines": lines or {"go vet": 1, "Clippy": 1}, "notes": list(notes), "toolchains": {}, "runs": list(runs),
            **({"disk": disk} if disk else {})}


def issue(case, tool, code, kept, verdict):
    return {"case": case, "tool": tool, "code": code, "level": "", "path": "a.go", "line": 7, "text": "x", "message": "m",
            "kept": kept, "verdict": verdict, "note": ""}


def test_score_adds_up_the_marks_and_the_build_times_of_each_project(tmp_path):
    out = tmp_path / "run"
    for name, prs, rows in (
        ("go", [pr("gin-gonic/gin", 3, 0, {"go vet": 200.0}),
                pr("gin-gonic/gin", 2, 1, {"go vet": 20.0}, ["go vet couldn't build every package of the module (`x`), so its findings there are missing."]),
                pr("gin-gonic/gin", 1, 2, {"go vet": 40.0})],
         [issue("gin-gonic/gin#3", "go vet", "printf", "yes", "right"), issue("gin-gonic/gin#3", "go vet", "composites", "no", "wrong")]),
        ("rust", [pr("tokio-rs/tokio", 9, 0, {"Clippy": 700.0}, disk={"free_mb": 80_000.0, "clone_mb": 120.0, "target_mb": 2_500.0}),
                  pr("tokio-rs/tokio", 8, 1, {"Clippy": 30.0}, disk={"free_mb": 79_000.0, "clone_mb": 121.0, "target_mb": 2_900.0}),
                  pr("tokio-rs/tokio", 7, 2, {"Clippy": 2.0}, runs=[{"program": "Clippy", "folder": "tokio", "code": 0, "stderr": "",
                                                                  "seconds": 2.0, "timed_out": False, "built": 0, "compiled": 0}])],
         [issue("tokio-rs/tokio#9", "clippy", "eq_op", "yes", "wrong")]),
    ):
        (out / "raw" / name).mkdir(parents=True)
        (out / "raw" / name / "prs.json").write_text(json.dumps(prs))
        bench._write(out / "raw" / name / "issues.csv", build_checks.ISSUE_COLUMNS, rows)
    build_checks._score(out)
    text = (out / "summary.md").read_text(encoding="utf-8")
    assert "6 merged PRs from 2 projects" in text
    assert "| `checks.go_vet` | 3 | 3 | 1 | 1 | 0 | 0 | 1 | 0 (over 300 s) |" in text
    # A Clippy run that built nothing is a build that didn't happen, whatever its exit code.
    assert "| `checks.clippy` | 3 | 3 | 1 | 0 | 1 | 0 | 1 | 1 (over 600 s) |" in text
    assert "| tokio-rs/tokio#7 | Clippy built nothing in `tokio` (exit code 0) |" in text
    assert "| gin-gonic/gin | go vet | 3 | 200 s | 30 s | 200 s | 0 |  |  |" in text
    assert "| tokio-rs/tokio | Clippy | 3 | 700 s | 16 s | 700 s | 1 | 3,021 MB | 79,000 MB |" in text
    assert "| go vet | `composites` | no | 1 | 0 | 1 | 0 |" in text
    assert "| gin-gonic/gin#2 | go vet couldn't build every package" in text


# ── the real go vet ──────────────────────────────────────────────────────

VET_ME = 'package m\n\nimport (\n\t"fmt"\n\t"net"\n)\n\nfunc F(s string) net.TCPAddr {\n\tfmt.Printf("%d\\n", s)\n\treturn net.TCPAddr{nil, 80, ""}\n}\n'


@needs_go
def test_the_real_go_vet_reports_an_analyzer_the_check_leaves_out(monkeypatch, tmp_path):
    real = go_vet.find_program
    monkeypatch.setattr(go_vet, "find_program", lambda name: real(name) if name == "go" else None)  # go vet alone
    root = go_module(tmp_path, text=VET_ME)
    files = parse_diff(new_file("a.go", VET_ME))
    result = build_checks.measure(str(root), files, timeout=600)
    rows = build_checks.issues(str(root), files, result["calls"])
    assert sorted((r["code"], r["line"], r["kept"]) for r in rows) == [("composites", 10, "no"), ("printf", 9, "yes")]
    assert [f.rule for f in result["findings"]] == ["go-vet/printf"]
