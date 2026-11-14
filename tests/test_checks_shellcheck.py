import json
import os
import subprocess
from pathlib import Path

import pytest
from fakes import FakeGitHub

from stratum_open.checks import common, run_checks, shellcheck
from stratum_open.diff import parse_diff
from stratum_open.policy import Checks, Policy, parse_policy
from stratum_open.runner import Context, review_pull

ON = parse_policy("checks:\n  shellcheck: true\n")
needs_shellcheck = pytest.mark.skipif(not shellcheck.available(), reason="ShellCheck isn't on PATH")
WARNING = 2098  # one of the kept warnings: an assignment only the command sees


def diff(path, added=(), context=(), *, start=1):
    head = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}",
            f"@@ -{start},{len(context)} +{start},{len(context) + len(added)} @@"]
    return "\n".join(head + [f" {line}" for line in context] + [f"+{line}" for line in added]) + "\n"


def new_file(path, text):
    lines = text.rstrip("\n").split("\n")
    head = [f"diff --git a/{path} b/{path}", "new file mode 100755", "--- /dev/null", f"+++ b/{path}", f"@@ -0,0 +1,{len(lines)} @@"]
    return "\n".join(head + [f"+{line}" for line in lines]) + "\n"


def moved(old, new):
    return f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"


def issue(line, code=2283, level="error", message="Remove spaces around = to assign (or use [ ] to compare, or quote '=' if literal).", path="run.sh", end=None):
    return {"path": path, "code": code, "level": level, "message": message, "line": line, "end_line": end or line}


def fake_shellcheck(monkeypatch, issues=(), sent=None):
    monkeypatch.setattr(shellcheck, "available", lambda: True)
    monkeypatch.setattr(shellcheck, "run_shellcheck", lambda sources: (sent.append(dict(sources)) if sent is not None else None) or list(issues))


def test_no_contents_means_no_shellcheck():
    assert shellcheck.check(parse_diff(diff("run.sh", ["x = 1"]))) == []


def test_without_shellcheck_on_path_it_is_skipped_quietly(monkeypatch):
    monkeypatch.setattr(shellcheck, "available", lambda: False)
    files = parse_diff(diff("run.sh", ["x = 1"]))
    assert shellcheck.check(files, {"run.sh": "x = 1\n"}) == []
    assert run_checks(files, ON, contents={"run.sh": "x = 1\n"}) == []


def test_it_runs_only_when_the_policy_turns_it_on(monkeypatch):
    sent = []
    fake_shellcheck(monkeypatch, [issue(1)], sent)
    files, contents = parse_diff(diff("run.sh", ["x = 1"])), {"run.sh": "x = 1\n"}
    assert run_checks(files, Policy(), contents=contents) == [] and sent == []
    assert [f.rule for f in run_checks(files, ON, contents=contents)] == ["shellcheck/SC2283"]


def test_a_policy_without_the_key_is_what_it_was():
    assert Policy().checks.shellcheck is False and parse_policy("checks:\n  ruff: true\n").checks.shellcheck is False
    # The new keys come last, so a Checks built by position means what it did.
    assert Checks(True, True, True, True, True, True, True) == Checks(
        bandit_low=True, heuristics=True, ruff=True, floating_promises=True, weakened_tests=True, unknown_names=True, removed_names=True,
        shellcheck=False, actionlint=False,
    )
    assert parse_policy("checks:\n  shellcheck: sometimes\n").errors == ("checks.shellcheck must be true or false.",)


def test_errors_and_the_kept_warnings_on_added_lines_are_reported(monkeypatch):
    fake_shellcheck(monkeypatch, [
        issue(2),  # an error on an added line: kept
        issue(3, code=WARNING, level="warning", message="This expansion will not see the mentioned assignment."),  # a kept warning
        issue(1),  # an unchanged line: dropped
        issue(3, code=2034, level="warning", message="x appears unused. Verify use (or export if used externally)."),  # a warning not kept
        issue(3, code=2086, level="info", message="Double quote to prevent globbing and word splitting."),  # info: never kept
        issue(1, code=1073, message="Couldn't parse this double quoted string.", end=3),  # starts on an unchanged line: kept on the first added
    ])
    files = parse_diff(diff("run.sh", ["x = 1", "a=1 echo $a"], context=["#!/bin/sh"]))
    findings = shellcheck.check(files, {"run.sh": "#!/bin/sh\nx = 1\na=1 echo $a\n"})
    assert [(f.rule, f.severity, f.line_start) for f in findings] == [("shellcheck/SC2283", "high", 2), (f"shellcheck/SC{WARNING}", "medium", 3), ("shellcheck/SC1073", "high", 2)]
    assert all(f.source == "check" and f.category == "bug" and "shellcheck.net/wiki/SC" in f.suggestion for f in findings)
    assert findings[0].title.startswith("ShellCheck SC2283: `Remove spaces around = to assign")
    assert "ShellCheck reports SC2283" in findings[0].detail


def test_the_dropped_errors_and_unlisted_warnings_are_never_kept():
    assert all(shellcheck.kept(code, "error") is None for code in shellcheck.DROPPED)
    assert shellcheck.kept(2283, "error") and all(shellcheck.kept(code, "warning") for code in shellcheck.WARNINGS)
    assert shellcheck.kept(2034, "warning") is None and shellcheck.kept(2086, "info") is None and shellcheck.kept(2086, "style") is None
    assert not set(shellcheck.DROPPED) & set(shellcheck.WARNINGS)


def test_text_from_the_pr_stays_inside_a_code_span(monkeypatch):
    fake_shellcheck(monkeypatch, [issue(1, message="`@mona` [x](http://x) is referenced but not assigned.")])
    [finding] = shellcheck.check(parse_diff(diff("run.sh", ["echo"])), {"run.sh": "echo\n"})
    assert "`'@mona' [x](http://x) is referenced but not assigned`" in finding.detail
    assert finding.title.count("`") == 2 and finding.detail.count("`") == 2


def test_which_files_are_scripts():
    scripts = {
        "bin/install": "#!/usr/bin/env bash\necho hi", "run": "#!/bin/sh -e\necho hi", "ci/build.bash": "echo hi",
        "lib/env.sh": "export A=1", "bin/go": "#!/usr/bin/env -S bash -eu\necho hi", "bin/d": "#! /bin/dash\necho hi",
    }
    others = {
        "bin/tool": "#!/usr/bin/env python3\nprint(1)", "zsh/plugin.sh": "#!/bin/zsh\necho hi", "bin/t.sh": "#!/usr/bin/env bats\n@test x { true; }",
        "tests/run.sh": "echo hi", "README": "words", "bin/shout": "#!/usr/bin/env shout\nhi", "Makefile": "all:\n\techo",
    }
    every = {**scripts, **others}
    files = parse_diff("".join(new_file(path, text) for path, text in every.items()))
    assert sorted(shellcheck.plan(files, every)[0]) == sorted(scripts)
    # Without the text, the diff's first line tells.
    assert sorted(shellcheck.plan(files, {})[1]) == sorted(scripts)


def test_a_file_with_no_extension_is_fetched_unless_its_first_line_says_it_isnt_a_script():
    files = {f.path: f for f in parse_diff(
        new_file("bin/tool", "#!/usr/bin/env python3\nprint(1)") + new_file("bin/run", "#!/bin/bash\necho")
        + diff("bin/old", ["echo hi"], context=["a", "b", "c"], start=20) + diff("lib/a.sh", ["echo"]) + diff("tests/t.sh", ["echo"])
        + diff("Makefile.am", ["x"]) + diff("docs/notes", ["words"], context=["# Notes"], start=1)
    )}
    assert {path for path, f in files.items() if shellcheck.worth_reading(f)} == {"bin/run", "bin/old", "lib/a.sh"}


def test_test_files_deleted_files_and_other_languages_are_not_sent(monkeypatch):
    sent = []
    fake_shellcheck(monkeypatch, sent=sent)
    text = diff("tests/run.sh", ["x = 1"]) + diff("app.py", ["x = 1"]) + diff("ci/run.sh", ["x = 1"])
    shellcheck.check(parse_diff(text), {"tests/run.sh": "x = 1\n", "app.py": "x = 1\n", "ci/run.sh": "x = 1\n"})
    assert [sorted(s) for s in sent] == [["ci/run.sh"]]


def test_at_most_twenty_files_go_to_shellcheck_and_none_too_long(monkeypatch):
    sent = []
    fake_shellcheck(monkeypatch, sent=sent)
    monkeypatch.setattr(shellcheck, "MAX_FILE_CHARS", 100)
    text = "".join(diff(f"ci/s{i}.sh", ["x=1"]) for i in range(25)) + diff("ci/big.sh", ["x=1", "y=2"])
    contents = {**{f"ci/s{i}.sh": "x=1\n" for i in range(25)}, "ci/big.sh": "x=1\ny=2\n" + "# pad\n" * 20}
    shellcheck.check(parse_diff(text), contents)
    assert len(sent[0]) == 20 and "ci/big.sh" not in sent[0]
    read, left_out = shellcheck.plan(parse_diff(text), contents)
    assert len(read) == 20 and len(left_out) == 6 and "ci/big.sh" in left_out


def test_the_plan_names_scripts_shellcheck_has_no_text_for():
    text = diff("ci/a.sh", ["x=1"]) + diff("ci/b.sh", ["y=2"]) + diff("tests/c.sh", ["z=3"])
    assert shellcheck.plan(parse_diff(text), {"ci/a.sh": "x=1\n"}) == (["ci/a.sh"], ["ci/b.sh"])


def test_a_disable_comment_the_pr_adds_is_switched_off_and_one_already_there_is_kept():
    text = "#!/bin/bash\n# shellcheck disable=SC2283\nx = 1\n# shellcheck shell=bash disable=sc2154,all\ny = 2\n# shellcheck disable=SC2283\nz = 3\n"
    rewritten, named = shellcheck.without_disables(text, {2, 3, 4, 5})
    lines = rewritten.split("\n")
    assert lines[1] == "# shellcheck disabled-by-pr=SC2283" and lines[3] == "# shellcheck shell=bash disabled-by-pr=sc2154,all"
    assert lines[5] == "# shellcheck disable=SC2283"  # not an added line
    assert named == {"SC2283", "SC2154", "all"}


def test_a_finding_the_pr_disabled_says_so(monkeypatch):
    fake_shellcheck(monkeypatch, [issue(2), issue(4)])
    added = ["# shellcheck disable=SC2283", "x = 1", "# shellcheck disable=SC2154", "y = 2"]
    files = parse_diff(diff("run.sh", added))
    notes = ["`# shellcheck disable`" in f.detail for f in shellcheck.check(files, {"run.sh": "\n".join(added) + "\n"})]
    assert notes == [True, True]  # the codes the PR names count, wherever its comment is
    fake_shellcheck(monkeypatch, [issue(2)])
    [finding] = shellcheck.check(parse_diff(diff("run.sh", ["# shellcheck disable=SC2154", "x = 1"])), {"run.sh": "# shellcheck disable=SC2154\nx = 1\n"})
    assert "disable" not in finding.detail


def test_a_file_moved_out_of_the_tests_goes_to_shellcheck_with_every_line(monkeypatch):
    sent = []
    fake_shellcheck(monkeypatch, [issue(1, path="ci/run.sh")], sent)
    findings = shellcheck.check(parse_diff(moved("tests/run.sh", "ci/run.sh")), {"ci/run.sh": "x = 1\n"})
    assert [sorted(s) for s in sent] == [["ci/run.sh"]]
    assert [(f.rule, f.line_start) for f in findings] == [("shellcheck/SC2283", 1)]


def test_a_shellcheck_crash_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(shellcheck, "available", lambda: True)

    def crash(sources):
        raise RuntimeError("shellcheck broke")

    monkeypatch.setattr(shellcheck, "run_shellcheck", crash)
    [error] = [f for f in run_checks(parse_diff(diff("run.sh", ["x=1"])), ON, contents={"run.sh": "x=1\n"}) if f.rule == "checks/error"]
    assert "ShellCheck" in error.title and "shellcheck broke" in error.detail and error.severity == "low"


def fake_program(monkeypatch, tmp_path, name="shellcheck"):
    folder = tmp_path / "bin"
    folder.mkdir(exist_ok=True)
    program = folder / (name + (".exe" if os.name == "nt" else ""))
    program.write_text("")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))
    return str(program)


def test_shellcheck_is_started_where_the_pr_cant_steer_it(monkeypatch, tmp_path):
    calls = []
    program = fake_program(monkeypatch, tmp_path)

    def run(command, **kwargs):
        folder = Path(kwargs["cwd"])
        calls.append((command, kwargs, sorted(p.name for p in folder.iterdir()), (folder / "0.bash").read_text(encoding="utf-8")))
        return subprocess.CompletedProcess(command, 0, stdout='{"comments": []}', stderr="")

    monkeypatch.setattr(shellcheck.subprocess, "run", run)
    monkeypatch.setenv("SHELLCHECK_OPTS", "--external-sources")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    assert shellcheck.run_shellcheck({"ci/.shellcheckrc": "x=1\n", "run": "y=2\n"}) == []
    [(command, kwargs, written, text)] = calls
    assert command[0] == program and {"--norc", "--format=json1", "--severity=warning"} <= set(command)
    assert not {"-x", "--external-sources"} & set(command) and command[-2:] == ["0.bash", "1.bash"]
    assert written == ["0.bash", "1.bash"] and text == "x=1\n"
    assert checkout not in Path(kwargs["cwd"]).parents and not Path(kwargs["cwd"]).exists()
    assert kwargs["timeout"] == shellcheck.TIMEOUT_SECONDS and "SHELLCHECK_OPTS" not in kwargs["env"]


def test_a_program_in_the_folder_the_review_runs_from_is_never_taken(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    planted = checkout / ("shellcheck" + (".exe" if os.name == "nt" else ""))
    planted.write_text("")
    planted.chmod(0o755)
    monkeypatch.chdir(checkout)
    for entry in (str(checkout), ".", "", str(checkout / "sub")):
        monkeypatch.setenv("PATH", entry)
        assert common.find_program("shellcheck") is None and shellcheck.available() is False
    real = fake_program(monkeypatch, tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(checkout), str(Path(real).parent)]))
    assert common.find_program("shellcheck") == real


def test_a_shellcheck_that_fails_or_cant_be_read_is_an_error_not_a_clean_result(monkeypatch, tmp_path):
    fake_program(monkeypatch, tmp_path)
    for code, stdout in ((2, ""), (0, "not json"), (1, "[]"), (3, '{"comments": []}')):
        monkeypatch.setattr(shellcheck.subprocess, "run", lambda command, code=code, stdout=stdout, **kwargs: subprocess.CompletedProcess(command, code, stdout=stdout, stderr="boom"))
        with pytest.raises(shellcheck.ShellCheckError, match=f"exit code {code}"):
            shellcheck.run_shellcheck({"run.sh": "x=1\n"})
    [error] = [f for f in run_checks(parse_diff(diff("run.sh", ["x=1"])), ON, contents={"run.sh": "x=1\n"}) if "ShellCheck" in f.title]
    assert error.rule == "checks/error" and "ShellCheck check didn't run" in error.title and "boom" not in error.detail


def test_shellcheck_running_out_of_time_is_reported(monkeypatch, tmp_path):
    fake_program(monkeypatch, tmp_path)

    def too_slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(shellcheck.subprocess, "run", too_slow)
    [error] = [f for f in run_checks(parse_diff(diff("run.sh", ["x=1"])), ON, contents={"run.sh": "x=1\n"}) if "ShellCheck" in f.title]
    assert error.rule == "checks/error" and "ShellCheck took longer than 60 s" in error.detail


def test_reports_from_shellcheck_that_make_no_sense_are_left_out(monkeypatch, tmp_path):
    fake_program(monkeypatch, tmp_path)
    report = {"comments": [
        {"file": "0.bash", "line": 2, "endLine": 3, "level": "error", "code": 2283, "message": "Remove spaces"},
        {"file": "9.bash", "line": 1, "endLine": 1, "level": "error", "code": 2283, "message": "not one of ours"},
        {"file": "0.bash", "line": "x", "level": "error", "code": 2283, "message": "no line"},
        {"file": "0.bash", "line": 1, "level": "error", "code": "SC2283", "message": "a code that isn't a number"},
        "not an issue",
    ]}
    monkeypatch.setattr(shellcheck.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout=json.dumps(report), stderr=""))
    assert shellcheck.run_shellcheck({"run.sh": "x=1\n"}) == [
        {"path": "run.sh", "code": 2283, "level": "error", "message": "Remove spaces", "line": 2, "end_line": 3},
    ]


def test_with_the_key_on_the_prs_scripts_are_fetched_for_shellcheck(monkeypatch):
    sent = []
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    fake_shellcheck(monkeypatch, [issue(1, path="ci/run.sh")], sent)
    gh = FakeGitHub(diff=diff("ci/run.sh", ["x = 1"]), files={("ci/run.sh", "a" * 40): "x = 1\n"})

    def review(policy):
        return review_pull(Context(gh=gh, policy=policy, model=None, bot="stratum[bot]", log=lambda line: None), 7)

    assert "shellcheck/SC2283" not in {f.rule for f in review(parse_policy(None)).findings} and sent == []
    assert "shellcheck/SC2283" in {f.rule for f in review(ON).findings} and [sorted(s) for s in sent] == [["ci/run.sh"]]


def test_the_summary_names_scripts_shellcheck_didnt_read(monkeypatch):
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    fake_shellcheck(monkeypatch)
    monkeypatch.setattr(shellcheck, "MAX_FILE_CHARS", 20)
    text = diff("ci/small.sh", ["x=1"]) + diff("ci/big.sh", ["y=2"])
    gh = FakeGitHub(diff=text, files={("ci/small.sh", "a" * 40): "x=1\n", ("ci/big.sh", "a" * 40): "y=2\n" + "# pad\n" * 10})
    review_pull(Context(gh=gh, policy=ON, model=None, bot="stratum[bot]", log=lambda line: None), 7)
    assert "Not read by ShellCheck: 1 file over 20 characters (`ci/big.sh`)" in gh.issue_comments_[7][-1]["body"]


# ── the real ShellCheck ──────────────────────────────────────────────────

# Each kept code, with a script that has it on line 2. ShellCheck must report each one there.
PLANTED = {
    2283: "#!/bin/bash\nx = 1\n",
    1073: '#!/bin/bash\necho "${a[@]"\n',
    2259: "#!/bin/bash\necho x | cat < file\n",
    2071: "#!/bin/bash\n[ \"$n\" > 3 ] && echo big\n",
    2168: "#!/bin/bash\nlocal y=1\n",
    2069: "#!/bin/bash\nmake 2>&1 >build.log\n",
    2098: "#!/bin/bash\nname=x echo \"$name\"\n",
    2216: "#!/bin/bash\nfind . -name '*.tmp' | rm\n",
    2252: "#!/bin/bash\nif [ \"$1\" != a ] || [ \"$1\" != b ]; then exit 1; fi\n",
}
# Scripts that work and look like a mistake: nothing the check keeps may fire on them.
# Several are what ShellCheck reported on merged code (benchmark/results/2026-11-14-shell-checks).
WORKING = """#!/bin/bash
set -euo pipefail
name="${1:-world}"
echo "hello $name"
for f in ./*.txt; do [ -e "$f" ] || continue; wc -l "$f"; done
cd /tmp || exit 1
echo $HOME
TERMUX_PKG_REVISION=2
version= os= arch=
tmpdir=$(mktemp -d)
trap "rm -rf $tmpdir" EXIT
shims=(a b)
eval "conda_shim() { case \\"$1\\" in ${shims[@]:-} *) return 1;; esac; }"
prefix_re=x
grep -Ee "^$prefix_re[-.].*$" file || true
for f in $(ls); do echo "$f"; done
run() { command $@; }
"""


def run_real(text, path="ci/run.sh"):
    lines = text.rstrip("\n").split("\n")
    return shellcheck.check(parse_diff(new_file(path, text)), {path: text}) if lines else []


@needs_shellcheck
def test_real_shellcheck_catches_every_planted_case():
    for code, text in PLANTED.items():
        assert (f"shellcheck/SC{code}", 2) in {(f.rule, f.line_start) for f in run_real(text)}, code


@needs_shellcheck
def test_real_shellcheck_stays_quiet_on_working_scripts():
    assert run_real(WORKING) == []


@needs_shellcheck
def test_real_shellcheck_finds_errors_on_added_lines_only():
    text = "#!/bin/bash\nx = 1\necho fine\ny = 2\n"
    files = parse_diff(diff("ci/run.sh", ["y = 2"], context=["#!/bin/bash", "x = 1", "echo fine"]))
    assert [(f.rule, f.line_start) for f in shellcheck.check(files, {"ci/run.sh": text})] == [("shellcheck/SC2283", 4)]


@needs_shellcheck
def test_real_shellcheck_ignores_a_disable_the_pr_adds_and_keeps_one_already_there():
    text = "#!/bin/bash\n# shellcheck disable=SC2283\nx = 1\n# shellcheck disable=SC2283\ny = 2\n"
    files = parse_diff("diff --git a/ci/run.sh b/ci/run.sh\n--- a/ci/run.sh\n+++ b/ci/run.sh\n@@ -1,2 +1,5 @@\n #!/bin/bash\n"
                       "+# shellcheck disable=SC2283\n+x = 1\n # shellcheck disable=SC2283\n+y = 2\n")
    findings = shellcheck.check(files, {"ci/run.sh": text})
    assert [(f.rule, f.line_start) for f in findings] == [("shellcheck/SC2283", 3)] and "disable" in findings[0].detail


@needs_shellcheck
def test_real_shellcheck_reads_no_config_and_follows_no_source(monkeypatch, tmp_path):
    (tmp_path / ".shellcheckrc").write_text("disable=SC2283\n")
    (tmp_path / "lib.sh").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SHELLCHECK_OPTS", "--exclude=SC2283")
    findings = run_real("#!/bin/bash\nx = 1\nsource ./lib.sh\n")
    assert [(f.rule, f.line_start) for f in findings] == [("shellcheck/SC2283", 2)]


@needs_shellcheck
def test_real_shellcheck_leaves_scripts_for_other_shells_alone():
    assert run_real("#!/bin/zsh\necho ${(j:,:)a}\nx = 1\n", "zsh/plugin.sh") == []
    assert [f.rule for f in run_real("export A=1\nlocal x\n", "lib/env.sh")] == ["shellcheck/SC2168"]  # read as bash, with no SC2148
