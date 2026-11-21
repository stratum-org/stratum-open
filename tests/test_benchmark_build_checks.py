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


def test_run_writes_every_issue_and_the_build_times_and_keeps_the_marks(monkeypatch, tmp_path):
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
    # The marks stay when it's run again.
    rows[1]["verdict"], rows[1]["note"] = "wrong", "a style matter"
    bench._write(out / "issues.csv", build_checks.ISSUE_COLUMNS, rows)
    fake_run(monkeypatch, (0, output, ""))
    build_checks._run(prs, Path("repos"), out, 60)
    assert [(r["verdict"], r["note"]) for r in read(out / "issues.csv")] == [("", ""), ("wrong", "a style matter")]


def read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_only_the_projects_asked_for_are_run():
    lists = build_checks.PRS
    every = build_checks.picked(lists, [])
    assert {item["repo"] for item in every} >= {"gin-gonic/gin", "nushell/nushell"} and len(every) == 120
    assert build_checks.picked(lists, ["all"]) == every
    assert {item["repo"] for item in build_checks.picked(lists, ["cli/cli"])} == {"cli/cli"}
    with pytest.raises(SystemExit, match="Not in the lists: acme/widget"):
        build_checks.picked(lists, ["acme/widget"])


# ── the summary ──────────────────────────────────────────────────────────


def pr(repo, number, order, seconds, notes=(), lines=None):
    return {"repo": repo, "pr": number, "head": "a" * 40, "order": order, "error": "", "seconds": seconds,
            "lines": lines or {"go vet": 1, "Clippy": 1}, "notes": list(notes), "toolchains": {}}


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
        ("rust", [pr("tokio-rs/tokio", 9, 0, {"Clippy": 700.0}), pr("tokio-rs/tokio", 8, 1, {"Clippy": 30.0})],
         [issue("tokio-rs/tokio#9", "clippy", "eq_op", "yes", "wrong")]),
    ):
        (out / "raw" / name).mkdir(parents=True)
        (out / "raw" / name / "prs.json").write_text(json.dumps(prs))
        bench._write(out / "raw" / name / "issues.csv", build_checks.ISSUE_COLUMNS, rows)
    build_checks._score(out)
    text = (out / "summary.md").read_text(encoding="utf-8")
    assert "5 merged PRs from 2 projects" in text
    assert "| `checks.go_vet` | 3 | 3 | 1 | 1 | 0 | 0 | 1 | 0 (over 300 s) |" in text
    assert "| `checks.clippy` | 2 | 2 | 1 | 0 | 1 | 0 | 0 | 1 (over 600 s) |" in text
    assert "| gin-gonic/gin | go vet | 3 | 200 s | 30 s | 200 s | 0 |" in text
    assert "| tokio-rs/tokio | Clippy | 2 | 700 s | 30 s | 700 s | 1 |" in text
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
