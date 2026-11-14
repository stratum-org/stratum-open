import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from fakes import FakeGitHub

from stratum_open.checks import ruff, run_checks
from stratum_open.diff import parse_diff
from stratum_open.policy import Checks, Policy, parse_policy
from stratum_open.runner import Context, review_pull

ON = parse_policy("checks:\n  ruff: true\n")
RISKS = Path(__file__).parent.parent / "benchmark" / "planted" / "risks.yml"
needs_ruff = pytest.mark.skipif(not ruff.available(), reason="Ruff isn't installed")


def diff(path, added=(), context=(), *, start=1):
    head = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}",
            f"@@ -{start},{len(context)} +{start},{len(context) + len(added)} @@"]
    return "\n".join(head + [f" {line}" for line in context] + [f"+{line}" for line in added]) + "\n"


def moved(old, new):
    return f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"


def issue(line, code="F821", message="Undefined name `totl`", path="app.py", end=None):
    return {"path": path, "code": code, "message": message, "line": line, "end_line": end or line}


def fake_ruff(monkeypatch, issues=(), sent=None):
    monkeypatch.setattr(ruff, "available", lambda: True)
    monkeypatch.setattr(ruff, "run_ruff", lambda sources: (sent.append(sorted(sources)) if sent is not None else None) or list(issues))


def planted_cases():
    return [case for case in yaml.safe_load(RISKS.read_text(encoding="utf-8")) if str(case["rule"]).startswith("ruff/")]


def test_no_contents_means_no_ruff():
    assert ruff.check(parse_diff(diff("app.py", ["x = totl"]))) == []


def test_without_ruff_installed_it_is_skipped_quietly(monkeypatch):
    monkeypatch.setattr(ruff, "available", lambda: False)
    files = parse_diff(diff("app.py", ["x = totl"]))
    assert ruff.check(files, {"app.py": "x = totl\n"}) == []
    assert run_checks(files, ON, contents={"app.py": "x = totl\n"}) == []


def test_it_runs_only_when_the_policy_turns_it_on(monkeypatch):
    sent = []
    fake_ruff(monkeypatch, [issue(1)], sent)
    files, contents = parse_diff(diff("app.py", ["x = totl"])), {"app.py": "x = totl\n"}
    assert run_checks(files, Policy(), contents=contents) == [] and sent == []
    assert [f.rule for f in run_checks(files, ON, contents=contents)] == ["ruff/F821"]


def test_a_policy_without_the_key_is_what_it_was():
    assert Policy().checks.ruff is False and parse_policy("checks:\n  heuristics: true\n").checks.ruff is False
    assert Checks(True, True) == Checks(bandit_low=True, heuristics=True, ruff=False)  # the new key comes last
    assert parse_policy("checks:\n  ruff: yes please\n").errors == ("checks.ruff must be true or false.",)


def test_only_issues_on_added_lines_from_the_rule_list_are_kept(monkeypatch):
    fake_ruff(monkeypatch, [
        issue(2),  # added: kept
        issue(3, code="F632", message="Use `==` to compare constant literals"),  # added: kept
        issue(1),  # an unchanged line: dropped
        issue(2, code="F401", message="`os` imported but unused"),  # not a rule the check runs: dropped
        issue(4, code="invalid-syntax", message="Expected an expression"),  # nor is a syntax error
        issue(1, code="F601", message='Dictionary key literal `"a"` repeated', end=3),  # starts on an unchanged line: kept on the first added
    ])
    files = parse_diff(diff("app.py", ["b = totl", "c = b is 'x'", "d ="], context=["a = {"]))
    findings = ruff.check(files, {"app.py": "a = {\nb = totl\nc = b is 'x'\nd =\n"})
    assert [(f.rule, f.severity, f.line_start) for f in findings] == [("ruff/F821", "high", 2), ("ruff/F632", "medium", 3), ("ruff/F601", "medium", 2)]
    assert all(f.source == "check" and f.category == "bug" and f.suggestion for f in findings)
    assert "raises `NameError`" in findings[0].detail and "Ruff reports this as F821." in findings[0].detail


def test_the_title_names_what_ruff_found_so_two_on_a_line_are_two_findings(monkeypatch):
    fake_ruff(monkeypatch, [issue(1), issue(1, message="Undefined name `other`"), issue(1, message="A wording Ruff may use one day")])
    files = parse_diff(diff("app.py", ["x = totl + other"]))
    titles = [f.title for f in ruff.check(files, {"app.py": "x = totl + other\n"})]
    assert titles == ["Undefined name: `totl`", "Undefined name: `other`", "Undefined name"]


def test_text_from_the_pr_never_leaves_its_code_marks(monkeypatch):
    # The key is the PR's own text. Only what Ruff quotes is shown, cut at the first backtick, and never the rest of its message.
    fake_ruff(monkeypatch, [issue(1, code="F601", message='Dictionary key literal `"a`@mona [x](http://x) "` repeated')])
    line = 'x = {"a`@mona [x](http://x) ": 1, "a`@mona [x](http://x) ": 2}'
    [finding] = ruff.check(parse_diff(diff("app.py", [line])), {"app.py": line + "\n"})
    assert finding.title == 'Dictionary key repeated: `"a`'
    assert "@mona" not in finding.title + finding.detail + finding.suggestion


def test_a_function_defined_twice_is_reported_and_a_name_imported_twice_is_not(monkeypatch):
    again = "Redefinition of unused `load` from line {}: `load` redefined here"
    fake_ruff(monkeypatch, [
        issue(2, code="F811", message=again.format(1)),  # an import over an import
        issue(3, code="F811", message=again.format(2)),  # a function over an import
        issue(5, code="F811", message=again.format(3)),  # a function over a function: the one that loses code
        issue(7, code="F811", message=again.format(5)),
        issue(7, code="F811", message="Redefinition of unused `load`"),  # no line to look at: left alone
    ])
    added = ["from a import load", "from b import load", "def load(path):", "    return path", "    async def load(path):", "        return path", "class load:"]
    findings = ruff.check(parse_diff(diff("app.py", added)), {"app.py": "\n".join(added) + "\n"})
    assert [(f.title, f.line_start) for f in findings] == [("Defined twice: `load`", 5), ("Defined twice: `load`", 7)]


def test_a_file_that_makes_names_as_it_runs_gets_no_undefined_names(monkeypatch):
    undefined, unlisted = "Undefined name `made_up`", "Undefined name `made_up` in `__all__`"
    fake_ruff(monkeypatch, [issue(2, message=undefined, path=path) for path in ("a.py", "b.py", "c.py")]
              + [issue(3, code="F822", message=unlisted, path=path) for path in ("a.py", "b.py", "c.py")])
    files = {
        "a.py": ["globals()['made_up'] = 1", "print(made_up)", "__all__ = ['made_up']"],
        "b.py": ["setattr(sys.modules[__name__], 'made_up', 1)", "print(made_up)", "__all__ = ['made_up']"],
        "c.py": ["names = globals()", "print(made_up)", "__all__ = ['made_up']"],  # reading globals() defines nothing
    }
    text = "".join(diff(path, added) for path, added in files.items())
    findings = ruff.check(parse_diff(text), {path: "\n".join(added) + "\n" for path, added in files.items()})
    assert [(f.path, f.rule) for f in findings] == [("c.py", "ruff/F821"), ("c.py", "ruff/F822")]


def test_a_module_with_its_own_getattr_can_list_any_name_in_all(monkeypatch):
    fake_ruff(monkeypatch, [issue(1, code="F822", message="Undefined name `lazy` in `__all__`"), issue(1, code="F822", message="Undefined name `lazy` in `__all__`", path="other.py")])
    lazy = ["__all__ = ['lazy']", "def __getattr__(name):", "    return 1"]
    plain = ["__all__ = ['lazy']", "class Box:", "    def __getattr__(self, name):", "        return 1"]
    findings = ruff.check(parse_diff(diff("app.py", lazy) + diff("other.py", plain)), {"app.py": "\n".join(lazy) + "\n", "other.py": "\n".join(plain) + "\n"})
    assert [(f.path, f.title) for f in findings] == [("other.py", "Undefined name in `__all__`: `lazy`")]


def test_an_init_that_returns_another_inits_result_is_fine(monkeypatch):
    fake_ruff(monkeypatch, [issue(3, code="PLE0101", message="Explicit return in `__init__`", end=5), issue(8, code="PLE0101", message="Explicit return in `__init__`")])
    added = ["class A(Base):", "    def __init__(self):", "        return super(", "        ).__init__(", "        )", "class B:", "    def __init__(self):", "        return self"]
    findings = ruff.check(parse_diff(diff("app.py", added)), {"app.py": "\n".join(added) + "\n"})
    assert [(f.title, f.line_start) for f in findings] == [("`__init__` returns a value", 8)]


def test_a_submodule_a_packages_init_imports_from_is_not_an_undefined_name(monkeypatch):
    fake_ruff(monkeypatch, [issue(2, message="Undefined name `_api`", path="pkg/__init__.py"), issue(3, message="Undefined name `_sys`", path="pkg/__init__.py"),
                            issue(2, message="Undefined name `_api`", path="pkg/tools.py")])
    added = ["from ._api import inject", "del _api", "del _sys"]
    text = diff("pkg/__init__.py", added) + diff("pkg/tools.py", added)
    findings = ruff.check(parse_diff(text), {"pkg/__init__.py": "\n".join(added) + "\n", "pkg/tools.py": "\n".join(added) + "\n"})
    # Only a package's `__init__.py` gets the submodule's name from that import.
    assert [(f.path, f.title) for f in findings] == [("pkg/__init__.py", "Undefined name: `_sys`"), ("pkg/tools.py", "Undefined name: `_api`")]


def test_test_files_deleted_files_and_other_languages_are_not_sent(monkeypatch):
    sent = []
    fake_ruff(monkeypatch, sent=sent)
    text = diff("tests/test_app.py", ["x = 1"]) + diff("app.js", ["x = 1"]) + diff("src/app.py", ["x = 1"])
    ruff.check(parse_diff(text), {"tests/test_app.py": "x = 1\n", "app.js": "x = 1\n", "src/app.py": "x = 1\n"})
    assert sent == [["src/app.py"]]


def test_at_most_twenty_files_go_to_ruff_and_none_too_long(monkeypatch):
    sent = []
    fake_ruff(monkeypatch, sent=sent)
    monkeypatch.setattr(ruff, "MAX_FILE_CHARS", 100)
    text = "".join(diff(f"pkg/m{i}.py", ["x = 1"]) for i in range(25)) + diff("pkg/big.py", ["x = 1", "y = 2"])
    contents = {**{f"pkg/m{i}.py": "x = 1\n" for i in range(25)}, "pkg/big.py": "x = 1\ny = 2\n" + "# pad\n" * 20}
    ruff.check(parse_diff(text), contents)
    assert len(sent[0]) == 20 and "pkg/big.py" not in sent[0]
    read, left_out = ruff.plan(parse_diff(text), contents)
    assert len(read) == 20 and len(left_out) == 6 and "pkg/big.py" in left_out


def test_the_plan_names_python_files_ruff_has_no_text_for():
    text = diff("app/a.py", ["x = 1"]) + diff("app/b.py", ["y = 2"]) + diff("tests/test_a.py", ["z = 3"])
    assert ruff.plan(parse_diff(text), {"app/a.py": "x = 1\n"}) == (["app/a.py"], ["app/b.py"])


def test_a_ruff_crash_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(ruff, "available", lambda: True)

    def crash(sources):
        raise RuntimeError("ruff broke")

    monkeypatch.setattr(ruff, "run_ruff", crash)
    findings = run_checks(parse_diff(diff("app.py", ["x = 1"])), ON, contents={"app.py": "x = 1\n"})
    (error,) = [f for f in findings if f.rule == "checks/error"]
    assert "Ruff" in error.title and "ruff broke" in error.detail and error.severity == "low"


def test_a_finding_on_a_line_the_pr_marks_noqa_says_so(monkeypatch):
    fake_ruff(monkeypatch, [issue(1), issue(2), issue(3), issue(4)])
    added = ["a = totl  # noqa", "b = totl  #noqa: F821", "c = totl  # ruff: ignore[F821]", "d = totl"]
    notes = ["`# noqa`" in f.detail for f in ruff.check(parse_diff(diff("app.py", added)), {"app.py": "\n".join(added) + "\n"})]
    assert notes == [True, True, True, False]


def test_a_file_moved_out_of_the_tests_goes_to_ruff_with_every_line(monkeypatch):
    sent = []
    fake_ruff(monkeypatch, [issue(1, path="app/helper.py")], sent)
    findings = ruff.check(parse_diff(moved("tests/helper.py", "app/helper.py")), {"app/helper.py": "x = totl\n"})
    assert sent == [["app/helper.py"]]
    assert [(f.rule, f.line_start) for f in findings] == [("ruff/F821", 1)]


def test_a_file_moved_between_folders_of_code_isnt_sent_again(monkeypatch):
    sent = []
    fake_ruff(monkeypatch, sent=sent)
    assert ruff.check(parse_diff(moved("lib/helper.py", "app/helper.py")), {"app/helper.py": "x = totl\n"}) == []
    assert sent == []


def test_ruff_is_started_where_the_pr_cant_steer_it(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        folder = Path(kwargs["cwd"])
        calls.append((command, kwargs, sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())))
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr(ruff.subprocess, "run", run)
    monkeypatch.setenv("RUFF_OUTPUT_FILE", str(tmp_path / "elsewhere.json"))
    monkeypatch.chdir(tmp_path)
    assert ruff.run_ruff({"pkg/__init__.py": "x = 1\n", "ruff.toml": "y = 2\n", "pkg/pyproject.toml": "z = 3\n"}) == []
    [(command, kwargs, written)] = calls
    assert command[:5] == [sys.executable, "-P", "-m", "ruff", "check"]  # -P: no `ruff.py` from the folder it starts in
    assert {"--isolated", "--no-cache", "--no-fix", "--ignore-noqa"} <= set(command) and "--fix" not in command
    assert command[command.index("--select") + 1] == ",".join(ruff.RULES) and command[command.index("--target-version") + 1] == "py314"
    # The files are written under names of ours, outside the folder the review runs from, and Ruff starts there.
    assert written == ["0/__init__.py", "1/module.py", "2/module.py"]
    assert [Path(arg).as_posix() for arg in command[-3:]] == written
    assert tmp_path not in Path(kwargs["cwd"]).parents and Path(kwargs["cwd"]) != tmp_path and not Path(kwargs["cwd"]).exists()
    assert kwargs["timeout"] == ruff.TIMEOUT_SECONDS and not any(key.upper().startswith("RUFF_") for key in kwargs["env"])


def test_a_ruff_that_fails_or_cant_be_read_is_an_error_not_a_clean_result(monkeypatch):
    monkeypatch.setattr(ruff, "available", lambda: True)
    for code, stdout in ((2, ""), (0, "not json"), (0, '{"results": []}'), (1, "[]")):
        monkeypatch.setattr(ruff.subprocess, "run", lambda command, code=code, stdout=stdout, **kwargs: subprocess.CompletedProcess(command, code, stdout=stdout, stderr="boom"))
        with pytest.raises(ruff.RuffError, match=f"exit code {code}"):
            ruff.run_ruff({"app.py": "x = 1\n"})
    # Bandit starts the same way, so it fails here too: the Ruff error is the one looked at.
    [error] = [f for f in run_checks(parse_diff(diff("app.py", ["x = 1"])), ON, contents={"app.py": "x = 1\n"}) if "Ruff" in f.title]
    assert error.rule == "checks/error" and "Ruff check didn't run" in error.title and "boom" not in error.detail


def test_ruff_running_out_of_time_is_reported(monkeypatch):
    monkeypatch.setattr(ruff, "available", lambda: True)

    def too_slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(ruff.subprocess, "run", too_slow)
    [error] = [f for f in run_checks(parse_diff(diff("app.py", ["x = 1"])), ON, contents={"app.py": "x = 1\n"}) if "Ruff" in f.title]
    assert error.rule == "checks/error" and "Ruff took longer than 15 s" in error.detail


def test_reports_from_ruff_that_make_no_sense_are_left_out(monkeypatch):
    report = [
        {"code": "F821", "message": "Undefined name `a`", "filename": os.path.join("x", "0", "module.py"), "location": {"row": 2, "column": 5}, "end_location": {"row": 2, "column": 6}},
        {"code": "F821", "message": "Undefined name `b`", "filename": os.path.join("x", "9", "module.py"), "location": {"row": 1, "column": 1}, "end_location": {"row": 1, "column": 2}},
        {"code": "B015", "message": "Pointless comparison", "filename": os.path.join("x", "0", "module.py"), "location": {"row": 3, "column": 1}, "end_location": {"row": 4, "column": 1}},
        {"code": None, "message": "no code", "filename": os.path.join("x", "0", "module.py"), "location": {"row": 1, "column": 1}, "end_location": {"row": 1, "column": 2}},
        {"code": "F821", "message": "no line", "filename": os.path.join("x", "0", "module.py")},
        "not an issue",
    ]
    monkeypatch.setattr(ruff.subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=json.dumps(report), stderr=""))
    assert ruff.run_ruff({"app.py": "x = 1\n"}) == [
        {"path": "app.py", "code": "F821", "message": "Undefined name `a`", "line": 2, "end_line": 2},
        {"path": "app.py", "code": "B015", "message": "Pointless comparison", "line": 3, "end_line": 3},  # it ends where line 4 starts
    ]


def test_with_the_key_on_the_prs_python_is_fetched_for_ruff(monkeypatch):
    sent = []
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    fake_ruff(monkeypatch, [issue(1, path="app/total.py")], sent)
    gh = FakeGitHub(diff=diff("app/total.py", ["x = totl"]), files={("app/total.py", "a" * 40): "x = totl\n"})

    def review(policy):
        return review_pull(Context(gh=gh, policy=policy, model=None, bot="stratum[bot]", log=lambda line: None), 7)

    assert "ruff/F821" not in {f.rule for f in review(parse_policy(None)).findings} and sent == []
    assert "ruff/F821" in {f.rule for f in review(ON).findings} and sent == [["app/total.py"]]


def test_the_summary_names_files_ruff_didnt_read(monkeypatch):
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    fake_ruff(monkeypatch)
    monkeypatch.setattr(ruff, "MAX_FILE_CHARS", 20)
    text = diff("app/small.py", ["x = 1"]) + diff("app/big.py", ["y = 2"])
    gh = FakeGitHub(diff=text, files={("app/small.py", "a" * 40): "x = 1\n", ("app/big.py", "a" * 40): "y = 2\n" + "# pad\n" * 10})
    review_pull(Context(gh=gh, policy=ON, model=None, bot="stratum[bot]", log=lambda line: None), 7)
    assert "Not read by Ruff: 1 file over 20 characters (`app/big.py`)" in gh.issue_comments_[7][-1]["body"]


def test_every_rule_has_one_planted_case():
    assert sorted(case["rule"] for case in planted_cases()) == sorted(f"ruff/{code}" for code in ruff.RULES)


@needs_ruff
def test_the_installed_ruff_knows_every_rule():
    listed = subprocess.run([sys.executable, "-P", "-m", "ruff", "rule", "--all", "--output-format", "json"], capture_output=True, text=True, encoding="utf-8")
    known = {rule["code"] for rule in json.loads(listed.stdout) if not rule.get("preview")}
    assert set(ruff.RULES) <= known


@needs_ruff
def test_real_ruff_catches_every_planted_case():
    cases = planted_cases()
    for batch in (cases[start:start + ruff.MAX_FILES] for start in range(0, len(cases), ruff.MAX_FILES)):
        sources = {f"{case['id']}/{path}": text for case in batch for path, text in case["files"].items()}
        files = parse_diff("".join(diff(path, text.split("\n")[:-1]) for path, text in sources.items()))
        found = {(f.path.split("/")[0], f.rule) for f in ruff.check(files, sources)}
        assert {(case["id"], case["rule"]) for case in batch} <= found


@needs_ruff
def test_real_ruff_finds_errors_on_added_lines_only():
    context = ["def order_total(items):", "    total = 0", "    for item in items:", "        total += item.price", "    print(subtotl)"]
    added = ["    if total is 'none':", "        return totl", "    return {'a': total, 'a': 0}"]
    source = "\n".join(context + added) + "\n"
    findings = ruff.check(parse_diff(diff("shop/orders.py", added, context=context)), {"shop/orders.py": source})
    assert [(f.rule, f.title, f.line_start) for f in findings] == [
        ("ruff/F632", "`is` used to compare with a literal", 6), ("ruff/F821", "Undefined name: `totl`", 7), ("ruff/F601", "Dictionary key repeated: `'a'`", 8),
    ]


@needs_ruff
def test_real_ruff_ignores_noqa_the_pr_adds():
    added = ["# ruff: noqa", "# flake8: noqa", "def total(items):", "    return totl  # noqa: F821", "def other(items):", "    return othr"]
    findings = ruff.check(parse_diff(diff("app/total.py", added)), {"app/total.py": "\n".join(added) + "\n"})
    assert [(f.rule, f.line_start, "`# noqa`" in f.detail) for f in findings] == [("ruff/F821", 4, True), ("ruff/F821", 6, False)]


@needs_ruff
def test_real_ruff_reads_no_config_and_runs_no_code_from_where_the_review_runs(monkeypatch, tmp_path):
    # The folder a review runs from can be a checkout of the PR: a `ruff.py` in it must not run. And no Ruff
    # config counts, not even one above the temporary folder, which is where Ruff would look for one.
    (tmp_path / "ruff.toml").write_text('[lint]\nper-file-ignores = {"*.py" = ["F821"]}\n', encoding="utf-8")
    (tmp_path / "ruff.py").write_text("open('ran', 'w').close()\nraise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    findings = ruff.check(parse_diff(diff("app/total.py", ["x = totl"])), {"app/total.py": "x = totl\n"})
    assert [f.rule for f in findings] == ["ruff/F821"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ruff.py", "ruff.toml"]  # nothing ran, nothing was cached, nothing is left


@needs_ruff
def test_real_ruff_knows_a_packages_init_and_the_names_a_host_defines():
    init = ["from pkgutil import extend_path", "__path__ = extend_path(__path__, __name__)"]
    hosted = ["def greet(name):", "    if get_ipython() or __IPYTHON__:", "        return _('Hello') + name", "    return reveal_type(name)"]
    text = diff("pkg/__init__.py", init) + diff("pkg/greet.py", hosted)
    findings = ruff.check(parse_diff(text), {"pkg/__init__.py": "\n".join(init) + "\n", "pkg/greet.py": "\n".join(hosted) + "\n"})
    assert [(f.path, f.title, f.line_start) for f in findings] == [("pkg/greet.py", "Undefined name: `reveal_type`", 4)]


@needs_ruff
def test_real_ruff_says_nothing_about_a_file_it_cant_parse():
    added = ["def total(items):", "    return totl +"]
    assert ruff.check(parse_diff(diff("app/total.py", added)), {"app/total.py": "\n".join(added) + "\n"}) == []


@needs_ruff
def test_real_ruff_lets_an_annotation_name_a_class_defined_further_down():
    # Fine from Python 3.14, an error before it; the project's version isn't known, so it isn't reported.
    added = ["def first(shapes: list[Shape]) -> Shape:", "    return shapes[0]", "class Shape:", "    def scaled(self) -> Shape:", "        return self",
             "def area(shape: Shap) -> int:", "    return 0"]
    findings = ruff.check(parse_diff(diff("app/shapes.py", added)), {"app/shapes.py": "\n".join(added) + "\n"})
    assert [(f.title, f.line_start) for f in findings] == [("Undefined name: `Shap`", 6)]


@needs_ruff
def test_real_ruff_leaves_names_written_to_globals_alone():
    added = ["__all__ = ['RED', 'GREEN']", "for name in ('red', 'green'):", "    globals()[name.upper()] = name", "print(RED, GREEN)"]
    assert ruff.check(parse_diff(diff("app/colours.py", added)), {"app/colours.py": "\n".join(added) + "\n"}) == []


WORKING = '''\
from __future__ import annotations

import functools
import logging
import sys
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from decimal import Decimal

try:
    import json
except ImportError:
    json = None

try:
    text_type = unicode
except NameError:
    text_type = str

if sys.platform == "win32":
    def home():
        return "C:/"
else:
    def home():
        return "/"

logger = logging.getLogger(__name__)
counter = 0


class Slotted:
    __slots__ = ("x", "__dict__")

    def __init__(self, x):
        self.x = x
        self.label = ""


class Named(Slotted):
    def __init__(self, x):
        return super().__init__(x)


class Shape:
    __slots__ = ("_name",)

    def __init__(self):
        self._name = "shape"
        self.name = "square"

    @overload
    def area(self, scale: int) -> int: ...
    @overload
    def area(self, scale: float) -> float: ...
    def area(self, scale):
        return scale

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    def __eq__(self, other, strict=False):
        return NotImplemented

    def __exit__(self, *args):
        return None

    def __len__(self):
        return len(self._name)

    def __bool__(self):
        return len(self) > 0

    def __str__(self):
        return self._name

    def __hash__(self):
        return hash(self._name)

    def save(self):
        raise NotImplementedError


@functools.singledispatch
def render(value):
    return str(value)


@render.register
def _(value: int):
    return "int"


@render.register
def _(value: str):
    return "str"


def price(amount: Decimal) -> "Decimal":
    return amount


def formats(user, items, mapping, args):
    a = "%s" % {"a": 1}
    b = "%s: %s" % (user, items)
    c = "%(a)s %(b)s" % mapping
    d = "%s %s" % args
    e = "{} {}".format(*args)
    f = "{a} {b}".format(**mapping)
    g = "100%% of %s" % user
    h = "{{literal}} {}".format(user)
    logger.info("%s logged in from %s", user, items)
    logger.info("%(user)s logged in", {"user": user})
    logger.info("done: 100%")
    logger.info("%s %s", *args)
    return a, b, c, d, e, f, g, h


def compare(x, y):
    if x is None or y is not None:
        return x is True or y is ...
    return x == "a"


def keys(k):
    return {"a": 1, "b": 2, k: 3, "A": 4, 1: "x", (1, 2): 0, (1, 3): 0}


def loops(rows):
    for row in rows:
        if not row:
            continue
        if row == "stop":
            break
    else:
        return None
    return [x for x in rows if x]


def generator(rows):
    yield from rows
    value = yield
    return value


async def stream(rows):
    for row in rows:
        yield row
    await rows.close()


def handler():
    wanted = (KeyError, IndexError)
    try:
        pass
    except (OSError, ValueError):
        raise
    except wanted:
        pass
    except BaseException:
        pass


def bump():
    global counter
    counter += 1
    return counter


def outer():
    total = 0

    def inner():
        nonlocal total
        total += 1
        return total

    return inner


def index(items):
    pair = (1, 2)
    return pair[1], pair[-2], items[5], [1, 2, 3][0:10]


def calls(f, options):
    return f(a=1, **options), f(**options, **options), dict(a=1, **{"b": 2})


__all__ = ["Slotted", "Shape", "render", "lazy"]
__all__ += ["price"]


def __getattr__(name):
    if name == "lazy":
        return 1
    raise AttributeError(name)
'''


@needs_ruff
def test_real_ruff_stays_quiet_on_working_code_that_looks_like_a_mistake():
    files = parse_diff(diff("app/working.py", WORKING.split("\n")[:-1]))
    assert ruff.check(files, {"app/working.py": WORKING}) == []
