"""benchmark/RESULTS.md is generated from the result files, and the READMEs
quote some of its numbers by hand. These tests keep the three in step: the
page is what the script writes today, and each figure a README quotes is one
the results give.

A new folder under benchmark/results/ changes the page, and the first test
then says which command to run. No test names a result folder, so a new one
needs nothing else here, unless it makes a figure in a README out of date."""

import json
import re
import sys
from itertools import zip_longest
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "benchmark"))

import bench  # noqa: E402
import planted  # noqa: E402
import results_page  # noqa: E402

README = ROOT / "README.md"
BENCHMARK_README = ROOT / "benchmark" / "README.md"
COUNT = re.compile(r"\b(\d+) of (\d+)\b")
NUMBERS = {word: number for number, word in enumerate("two three four five six seven eight nine ten".split(), start=2)}


@pytest.fixture(scope="module")
def page():
    return results_page.collect()


# ── the page ─────────────────────────────────────────────────────────────


def test_the_page_is_what_the_script_writes(page):
    written = results_page.render(page)
    committed = results_page.PAGE.read_text(encoding="utf-8") if results_page.PAGE.exists() else ""
    if written != committed:
        lines = zip_longest(committed.splitlines(), written.splitlines())
        first = next((f"line {number}, which becomes: {new}" for number, (old, new) in enumerate(lines, start=1) if old != new), "how its last line ends")
        pytest.fail(
            "benchmark/RESULTS.md isn't what the result files give now. Run `python benchmark/results_page.py` and commit the page.\n"
            f"The first thing to change is {first}"
        )


def test_every_result_folder_is_in_a_table_or_listed_as_left_out(page):
    text = results_page.render(page)
    silent = [path.name for path in sorted(results_page.RESULTS.iterdir()) if path.is_dir() and f"(results/{path.name}/)" not in text]
    assert not silent, f"benchmark/RESULTS.md says nothing about: {', '.join(silent)}"


def test_the_interval_is_wilsons():
    # Wilson (1927), checked against values worked by hand.
    assert results_page.wilson(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert results_page.wilson(1, 10) == pytest.approx((0.0179, 0.4042), abs=1e-4)
    # At none and at all the end is exact, not a rounding away from it.
    assert results_page.wilson(0, 10) == (0.0, pytest.approx(0.2775, abs=1e-4))
    assert results_page.wilson(10, 10) == (pytest.approx(0.7225, abs=1e-4), 1.0)


def _review(case, **values):
    row = {"case": case, "set": "", "kind": "off-by-one", "path": "a.py", "line": 1, "model": "m:7b", "variant": "", "version": "planted",
           "seconds": 10, "shown": "yes", "calls": 1, "failed_calls": 0, "findings": 0, "near": 0, "error": ""}
    return {**row, **values}


def _finding(case, verdict, near="yes", **values):
    row = {"case": case, "model": "m:7b", "variant": "", "version": "planted", "severity": "medium", "category": "bug", "path": "a.py",
           "line": 1, "near": near, "title": "t", "detail": "d", "verdict": verdict, "note": ""}
    return {**row, **values}


def _run(folder, reviews, findings):
    folder.mkdir(parents=True)
    bench._write(folder / "reviews.csv", planted.REVIEW_COLUMNS, reviews)
    bench._write(folder / "findings.csv", planted.FINDING_COLUMNS, findings)


def test_a_finding_near_the_bug_that_nobody_marked_makes_found_a_lower_bound(tmp_path):
    reviews = [_review("o/r#1", findings=1, near=1), _review("o/r#2", findings=1, near=1), _review("o/r#3", findings=1, near=1), _review("o/r#4")]
    _run(tmp_path / "run", reviews, [_finding("o/r#1", "right"), _finding("o/r#2", ""), _finding("o/r#3", "wrong")])
    page = results_page.collect(tmp_path)
    (row,) = page["models"]
    assert (row["found"], row["shown"], row["open"]) == (1, 4, 1)
    upper = results_page._percent(results_page.wilson(2, 4)[1])
    assert f"| at least 1 of 4 | {results_page._percent(results_page.wilson(1, 4)[0])} to {upper} |" in results_page.render(page)


def test_a_marked_run_gives_a_plain_count(tmp_path):
    _run(tmp_path / "run", [_review("o/r#1", findings=2, near=1), _review("o/r#2")], [_finding("o/r#1", "right"), _finding("o/r#1", "", near="")])
    text = results_page.render(results_page.collect(tmp_path))
    assert "| 1 of 2 |" in text and "at least" not in text.split("| Model |", 1)[1]


def test_a_build_check_run_gets_its_own_table(tmp_path):
    import build_checks

    run = tmp_path / "2026-01-01-build"
    (run / "raw" / "gin").mkdir(parents=True)
    prs = [{"repo": "gin-gonic/gin", "pr": 1, "head": "a" * 40, "order": 0, "error": "", "seconds": {"go vet": 30.0},
            "lines": {"go vet": 12, "Clippy": 0}, "notes": [], "runs": [{"program": "go vet", "folder": ".", "code": 0, "stderr": "",
                                                                       "seconds": 30.0, "timed_out": False, "built": 2}]}]
    (run / "raw" / "gin" / "prs.json").write_text(json.dumps(prs))
    issue = {"case": "gin-gonic/gin#1", "tool": "go vet", "code": "printf", "level": "", "path": "a.go", "line": 7, "text": "x",
             "message": "m", "kept": "yes", "verdict": "wrong", "note": ""}
    bench._write(run / "raw" / "gin" / "issues.csv", build_checks.ISSUE_COLUMNS, [issue])
    bench._write(run / "raw" / "gin" / "kept.csv", build_checks.FINDING_COLUMNS, [])
    page = results_page.collect(tmp_path)
    assert [(row["setting"], row["prs"], row["lines"], row["findings"], row["wrong"]) for row in page["build"]] == [
        ("checks.go_vet", 1, 12, 1, 1), ("checks.clippy", 0, 0, 0, 0)]
    assert "| `checks.go_vet` | [2026-01-01-build](results/2026-01-01-build/) | 1 | 12 | 1 | 0 | 1 | 0 | 0 | 0 (over 300 s) |" in results_page.render(page)


def test_a_folder_that_cant_be_counted_is_listed_with_the_reason(tmp_path):
    _run(tmp_path / "good", [_review("o/r#1")], [])
    _run(tmp_path / "twice", [_review("o/r#1"), _review("o/r#1")], [])
    _run(tmp_path / "disagree", [_review("o/r#1", findings=2)], [_finding("o/r#1", "wrong", near="")])
    (tmp_path / "empty").mkdir()
    (tmp_path / "broken" / "raw").mkdir(parents=True)
    (tmp_path / "broken" / "raw" / "reviews.csv").write_text("case,model\no/r#1,m:7b\n", encoding="utf-8")
    (tmp_path / "half").mkdir()
    (tmp_path / "half" / "checks.csv").write_text(",".join(planted.CHECK_COLUMNS) + "\n", encoding="utf-8")
    (tmp_path / "half" / "planted.json").write_text("[]", encoding="utf-8")
    (tmp_path / "garbled").mkdir()
    (tmp_path / "garbled" / "cases.json").write_text("not json", encoding="utf-8")
    page = results_page.collect(tmp_path)
    assert [row["run"] for row in page["models"]] == ["good"]
    why = dict(page["left_out"])
    assert sorted(why) == ["broken", "disagree", "empty", "garbled", "half", "twice"]
    assert "more than once" in why["twice"] and "disagree" in why["disagree"] and "no results" in why["empty"] and "`kind`" in why["broken"]
    assert why["half"] == "it has no prs.json" and why["garbled"] == "its scorer can't read it (JSONDecodeError)"
    text = results_page.render(page)
    assert all(f"| [{name}](results/{name}/) | {reason} |" in text for name, reason in why.items())
    # The page is the same wherever it's written.
    assert str(tmp_path) not in text and tmp_path.name not in text


def _checks(folder, cases, merged, prs, repo="o/r"):
    """A run of `planted.py checks`: its planted cases, and findings on `prs` merged PRs."""
    folder.mkdir(parents=True)
    (folder / "planted.json").write_text(json.dumps(cases), encoding="utf-8")
    (folder / "prs.json").write_text(json.dumps([{"repo": repo, "pr": number} for number in range(1, prs + 1)]), encoding="utf-8")
    rows = [{"set": "planted", "case": case["id"], "rule": case["rule"], "expected": "yes"} for case in cases if case["caught"] and not case.get("control")]
    rows += [{"set": "merged", "case": case, "rule": rule, "verdict": verdict} for case, rule, verdict in merged]
    bench._write(folder / "checks.csv", planted.CHECK_COLUMNS, rows)


def test_a_rule_that_is_off_by_default_gets_a_row_of_its_own_and_groups_are_added_up(tmp_path):
    name = planted.UNKNOWN_NAME
    cases = [{"id": "secret", "rule": "code/secret", "caught": True}, {"id": "name-1", "rule": name, "caught": True},
             {"id": "name-2", "rule": name, "caught": False}, {"id": "name-control", "rule": name, "caught": False, "control": True}]
    _checks(tmp_path / "split" / "go", cases, [("o/r#1", name, "wrong"), ("o/r#2", "paths/always-human", "right")], prs=3)
    _checks(tmp_path / "split" / "js", cases, [("o/s#1", name, "")], prs=2, repo="o/s")
    page = results_page.collect(tmp_path)
    assert [(row["group"], row["caught"], row["risks"], row["left_alone"], row["controls"]) for row in page["checks"]] == [("go", 2, 3, 1, 1), ("js", 2, 3, 1, 1)]
    assert [(row["setting"], row["caught"], row["risks"], row["findings"], row["wrong_prs"], row["open_prs"]) for row in page["rules"]] == [
        ("checks.unknown_names", 1, 2, 1, 1, 0), ("checks.unknown_names", 1, 2, 1, 0, 1)]
    (together,) = page["corpus"]
    assert (together["prs"], together["findings"], together["flagged"], together["wrong_prs"], together["open_prs"]) == (5, 3, 3, 1, 1)
    text = results_page.render(page)
    assert "(results/split/)" in text and "(results/split/go/)" in text and "| `checks.unknown_names` | `code/unknown-name` | 1 of 2 |" in text
    assert "| at least 1 of 5 |" in text


# ── the figures the READMEs quote ────────────────────────────────────────


def _pairs(page, run=None):
    """Every "k of n" the page gives as settled, for one run or for all of them."""
    def rows(kind):
        return [row for row in page.get(kind, []) if run in (None, row["run"])]

    pairs = set()
    for row in rows("models"):
        pairs |= {(row["near_clean"], row["clean"])} | ({(row["found"], row["shown"])} if not row["open"] else set())
    for row in rows("checks") + rows("rules"):
        pairs |= {(row["caught"], row["risks"]), (row["left_alone"], row["controls"])}
    for row in rows("checks") + rows("rules") + rows("corpus"):
        pairs |= {(row["flagged"], row["prs"])} | ({(row["wrong_prs"], row["prs"])} if not row["open_prs"] else set())
    for row in rows("awaits"):
        pairs |= {(row["caught"], row["cases"]), (row["alarm_prs"], row["prs"])}
    for row in rows("as_new"):
        pairs |= {(row["right"], row["right"] + row["wrong"])}
    for row in rows("shell"):
        pairs |= {(row["whole_right"], row["whole_right"] + row["whole_wrong"])}
    return {(hits, n) for hits, n in pairs if n}


def count_problems(text, pairs, where):
    """A "k of n" whose n is one of the benchmark's sample sizes has to be a
    count the page gives. Other numbers ("the top 5 of 15 files") are left alone."""
    sizes = {n for _, n in pairs}
    quoted = {(int(hits), int(n)) for hits, n in COUNT.findall(text)}
    return [f"{where} says \"{hits} of {n}\". The results give, of {n}: {', '.join(str(k) for k, m in sorted(pairs) if m == n)}."
            for hits, n in sorted(quoted) if n in sizes and (hits, n) not in pairs]


def _cells(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _said(row):
    return f"{row['found']} of {row['shown']}" + ("" if not row["open"] else ", not all marked") + f" in {row['run']}"


def _without_verify(variant):
    """`review.verify` only drops findings, so runs that differ in nothing
    else are runs of the same review: the README counts them together."""
    return re.sub(r"-?verify(-\w+)?$", "", variant)


def _variant(settings):
    """The variant a row's settings name: the policy file in
    benchmark/planted/variants/ that sets exactly those (`review.logic_pass:
    true` and the three others the example sets are logic-context10-verify-strict),
    else their names joined the way the runs name them (`review.reasoning: low`
    and `review.verify: strict` is reasoning-low-verify-strict). No setting is
    the plain run."""
    import yaml

    wanted = {key: value.lower() for key, value in settings.items()}
    for path in sorted((ROOT / "benchmark" / "planted" / "variants").glob("*.yml")):
        review = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("review") or {}
        if wanted and {key: str(value).lower() for key, value in review.items()} == wanted:
            return path.stem
    return "-".join(f"{key}-{value}" for key, value in settings.items())


def models_table_problems(text, page):
    """The README's Models table, row by row, against the runs of that model
    with those settings. A row's settings name its variant (`_variant`)."""
    if "\n## Models\n" not in text:
        return ["README.md has no \"## Models\" section, where tests/test_results_page.py looks for the benchmark's figures."]
    section = text.split("\n## Models\n", 1)[1].split("\n## ", 1)[0]
    table = [_cells(line) for line in section.splitlines() if line.startswith("|")]
    columns = ["Policy setting", "Bugs found", "Wrong comments per clean PR", "Time per PR"]
    if not table or not set(columns) <= set(table[0]):
        return [f"README.md's Models table should have the columns {', '.join(columns)}: tests/test_results_page.py reads them by name."]
    problems, sizes = [], set()
    for cells in table[2:]:
        row = dict(zip(table[0], cells))
        named = re.search(r"`([^`]+)`", cells[0])
        if not named:
            problems.append(f"README.md's Models table: the row \"{cells[0]}\" names no model in backticks.")
            continue
        model = named[1]
        variant = _variant(dict(re.findall(r"`review\.(\w+): (\w+)`", row["Policy setting"])))
        where = f"README.md's Models table, `{model}`" + (f" with {variant}" if variant else "")
        runs = [run for run in page["models"] if (run["model"], run["variant"]) == (model, variant)]
        if not runs:
            problems.append(f"{where}: no run under benchmark/results/ has that model and variant.")
            continue

        found = re.fullmatch(r"(\d+) of (\d+)(?: \((\d+) to (\d+) over (\w+) runs\))?", row["Bugs found"])
        if not found:
            problems.append(f"{where}: can't read \"{row['Bugs found']}\" as \"k of n\" or \"k of n (a to b over three runs)\".")
            continue
        hits, n = int(found[1]), int(found[2])
        sizes.add(n)
        if (hits, n) not in {(run["found"], run["shown"]) for run in runs if not run["open"]}:
            problems.append(f"{where}: says {hits} of {n} found. The results give {'; '.join(_said(run) for run in runs)}.")
        if found[3]:
            times = NUMBERS.get(found[5]) or (int(found[5]) if found[5].isdigit() else None)
            alike = sorted(run["found"] for run in page["models"] if run["model"] == model and run["shown"] == n and not run["open"]
                           and _without_verify(run["variant"]) == _without_verify(variant))
            if not alike or (int(found[3]), int(found[4]), times) != (alike[0], alike[-1], len(alike)):
                problems.append(f"{where}: says {found[3]} to {found[4]} over {found[5]} runs. The results give {len(alike)} marked run(s) "
                                f"on {n} cases that differ at most in verify, which found {', '.join(map(str, alike)) or 'nothing'}.")

        comments = re.fullmatch(r"(?:about )?(\d+(?:\.(\d+))?)", row["Wrong comments per clean PR"])
        rates = {run["run"]: run["on_clean"] / run["clean"] for run in runs if run["clean"]}
        places = len(comments[2] or "") if comments else 0
        if not comments or f"{float(comments[1]):.{places}f}" not in {f"{rate:.{places}f}" for rate in rates.values()}:
            problems.append(f"{where}: says \"{row['Wrong comments per clean PR']}\" comments per clean PR. The results give "
                            + ("; ".join(f"{rate:.2f} in {run}" for run, rate in rates.items()) or "no run with the clean control") + ".")

        minutes = re.fullmatch(r"(?:about )?(\d+(?:\.\d+)?) min", row["Time per PR"])
        # The README rounds to the half minute.
        if not minutes or not any(abs(run["seconds"] / 60 - float(minutes[1])) <= 0.25 for run in runs):
            problems.append(f"{where}: says \"{row['Time per PR']}\" per PR. The results give a median of "
                            + "; ".join(f"{run['seconds']:.0f}s ({run['seconds'] / 60:.1f} min) in {run['run']}" for run in runs) + ".")
    cases = re.search(r"(\d+) real PRs, each with one bug planted", section)
    if cases and sizes - {int(cases[1])}:
        problems.append(f"README.md says {cases[1]} real PRs each have a bug planted, and its table counts bugs found of {', '.join(map(str, sorted(sizes)))}.")
    return problems


def merged_prs_problems(text, page, where):
    """"184 merged PRs" has to be the number of merged PRs some run checked."""
    known = {row["prs"] for kind in ("checks", "corpus", "awaits", "merged", "shell", "build") for row in page.get(kind, [])}
    return [f"{where} says \"{number} merged PRs\". The runs on the page checked {', '.join(map(str, sorted(known)))}."
            for number in sorted({int(n) for n in re.findall(r"\b(\d+) (?:real |clean )?merged PRs\b", text)}) if number not in known]


def run_table_problems(text, page):
    """benchmark/README.md's table of runs: what each row says of its run,
    against that run's numbers. A row is read only where it uses one of the
    forms below, so a new row in other words is left alone."""
    problems = []
    for line in text.splitlines():
        linked = re.match(r"\| \[[^\]]+\]\(results/([^/)]+)/", line)
        if not linked:
            continue
        run, said = linked[1], _cells(line)[-1]
        where = f"benchmark/README.md, on {run}"
        models = [row for row in page["models"] if row["run"] == run]
        checks = [row for row in page["checks"] if row["run"] == run]
        if not any(row["run"] == run for kind in results_page.KINDS for row in page.get(kind, [])):
            problems.append(f"{where}: benchmark/RESULTS.md has no numbers for that run.")
            continue
        problems += count_problems(said, _pairs(page, run), where)
        problems += merged_prs_problems(said, {kind: [row for row in page.get(kind, []) if row["run"] == run] for kind in results_page.KINDS}, where)
        found = sorted(row["found"] for row in models)

        every = re.match(r"Planted bugs found: (.+?), of (\d+)", said)  # "... 7B 1, 14B 10, of 39"
        if every:
            counts = sorted(int(item.rsplit(" ", 1)[-1]) for item in every[1].split(", "))
            if counts != found or {row["shown"] for row in models} != {int(every[2])}:
                problems.append(f"{where}: says found {', '.join(map(str, counts))}, of {every[2]}. The results give {'; '.join(_said(row) for row in models)}.")
        for name, hits in re.findall(r"`([^`]+)` (\d+)(?=, |$)", said):  # "`qwen3:14b` 4, `phi4:14b` 3"
            named = [row for row in models if name in row["model"]]
            if int(hits) not in {row["found"] for row in named if not row["open"]}:
                problems.append(f"{where}: says `{name}` found {hits}. The results give {'; '.join(_said(row) for row in named) or 'no such model in that run'}.")
        for comments, prs in re.findall(r"\b(\d+) comments on (\d+) clean PRs\b", said):
            if (int(comments), int(prs)) not in {(row["on_clean"], row["clean"]) for row in models}:
                problems.append(f"{where}: says {comments} comments on {prs} clean PRs. The results give "
                                + "; ".join(f"{row['on_clean']} on {row['clean']}" for row in models if row["clean"]) + ".")
        for cases in re.findall(r"\bthe (\d+) dev cases\b", said):
            if {(row["planted"], *row["sets"]) for row in models} != {(int(cases), "dev")}:
                problems.append(f"{where}: says the {cases} dev cases. The results give {', '.join(sorted({results_page._cases(row) for row in models}))}.")
        if "no false alarms" in said and any(row["marked"]["wrong"] or row["marked"][""] for row in checks):
            problems.append(f"{where}: says no false alarms. The results give "
                            + "; ".join(f"{row['marked']['wrong']} marked wrong and {row['marked']['']} not marked" for row in checks) + ".")
    return problems


def test_the_readme_models_table_agrees_with_the_results(page):
    problems = models_table_problems(README.read_text(encoding="utf-8"), page)
    assert not problems, "\n".join(problems + ["Every figure is in benchmark/RESULTS.md. Correct the README, not the page."])


def test_counts_the_readmes_quote_are_on_the_page(page):
    problems = [problem for path in (README, BENCHMARK_README)
                for problem in count_problems(path.read_text(encoding="utf-8"), _pairs(page), path.relative_to(ROOT).as_posix())]
    problems += merged_prs_problems(README.read_text(encoding="utf-8"), page, "README.md")
    assert not problems, "\n".join(problems + ["Every figure is in benchmark/RESULTS.md. Correct the README, not the page."])


def test_the_benchmark_readme_says_of_each_run_what_its_results_give(page):
    problems = run_table_problems(BENCHMARK_README.read_text(encoding="utf-8"), page)
    assert not problems, "\n".join(problems + ["Every figure is in benchmark/RESULTS.md. Correct the README, not the page."])


def test_the_readmes_link_the_page():
    assert "(benchmark/RESULTS.md)" in README.read_text(encoding="utf-8")
    assert "(RESULTS.md)" in BENCHMARK_README.read_text(encoding="utf-8")


# The checks above, tried on a README and results made up for the purpose: they
# pass when the two agree and say so when a figure is changed.


def _model(run, model, variant, found, shown=39, **values):
    row = {"run": run, "model": model, "variant": variant, "found": found, "shown": shown, "planted": shown, "sets": ["dev", "test"], "open": 0,
           "clean": 0, "near_clean": 0, "on_clean": 0, "seconds": 100.0}
    return {**row, **values}


MADE_UP = {
    "models": [
        _model("first", "small:7b", "", 1, seconds=169.0),
        _model("second", "small:7b", "", 0, open=1, clean=39, on_clean=14, seconds=148.4),
        _model("second", "big:20b", "reasoning-low", 12, clean=39, near_clean=2, on_clean=16, seconds=166.0),
        _model("third", "big:20b", "reasoning-low", 6, shown=19, planted=19, sets=["dev"]),
        _model("third", "big:20b", "reasoning-low-verify-lenient", 16, clean=39, near_clean=2, on_clean=10, seconds=222.0),
        _model("third", "big:20b", "reasoning-low-verify-strict", 16, clean=39, on_clean=7, seconds=209.75),
    ],
    "checks": [{"run": "checks", "risks": 44, "caught": 43, "controls": 0, "left_alone": 0, "prs": 184, "flagged": 2, "wrong_prs": 0, "open_prs": 0,
                "marked": {"right": 3, "wrong": 0, "unsure": 0, "": 0}}],
    "awaits": [], "merged": [],
}
MODELS = """# A project

## Models

The numbers are from the benchmark: 39 real PRs, each with one bug planted.

| `ollama-model` | Policy setting | Bugs found | Wrong comments per clean PR | Time per PR | Download |
| --- | --- | --- | --- | --- | --- |
| `small:7b` (the example's) | | 1 of 39 | about 0.4 | about 2.5 min | 4.7 GB |
| `big:20b` | `review.reasoning: low` and `review.verify: strict` | 16 of 39 (12 to 16 over three runs) | about 0.2 | about 3.5 min | 13.8 GB |

## Data
"""
RUNS = """| Run | Machine | Models |
| --- | --- | --- |
| [checks](results/checks/summary.md) | Any (no model) | The checks: 43 of 44 planted risks caught, no false alarms on 184 merged PRs |
| [first](results/first/summary.md) | GitHub runner, 4 CPUs | Planted bugs found: `small` 7B 1, of 39 |
| [third](results/third/summary.md) | GitHub runner, 4 CPUs | `big:20b` with each `verify`: 16 of 39 found with strict verify, 7 comments on 39 clean PRs |
"""


def test_a_rows_settings_name_the_variant_file_that_sets_exactly_them():
    assert _variant({}) == ""
    assert _variant({"reasoning": "low", "verify": "strict"}) == "reasoning-low-verify-strict"
    assert _variant({"reasoning": "low", "verify": "strict", "logic_pass": "true", "context_lines": "10"}) == "logic-context10-verify-strict"
    assert _variant({"reasoning": "low", "verify": "strict", "logic_pass": "false", "context_lines": "0"}) == "reasoning-low-verify-strict"
    assert _variant({"verify": "strict", "samples": "2"}) == "verify-strict-samples-2"  # no file sets just those


def test_the_checks_pass_a_readme_that_agrees_with_the_results():
    assert models_table_problems(MODELS, MADE_UP) == []
    assert run_table_problems(RUNS, MADE_UP) == []
    assert count_problems(MODELS + RUNS + "It reads the top 5 of 15 files.", _pairs(MADE_UP), "README.md") == []


@pytest.mark.parametrize("was, now", [
    ("| 1 of 39 |", "| 2 of 39 |"),
    ("16 of 39 (", "17 of 39 ("),
    ("12 to 16", "13 to 16"),
    ("three runs", "four runs"),
    ("about 0.4", "about 0.3"),
    ("about 0.2", "about 0.1"),
    ("about 3.5 min", "about 4 min"),
    ("`review.verify: strict`", "`review.verify: lenient`"),
    ("`review.reasoning: low` and ", ""),
    ("39 real PRs", "40 real PRs"),
    ("Bugs found", "Bugs caught"),
])
def test_a_models_table_the_results_dont_give_is_reported(was, now):
    assert was in MODELS
    assert models_table_problems(MODELS.replace(was, now), MADE_UP)


@pytest.mark.parametrize("was, now", [
    ("43 of 44", "44 of 44"),
    ("184 merged PRs", "200 merged PRs"),
    ("7B 1, of 39", "7B 1, of 40"),
    ("7B 1, of 39", "7B 2, of 39"),
    ("16 of 39 found", "15 of 39 found"),
    ("7 comments on 39", "9 comments on 39"),
    ("results/third/", "results/fourth/"),
])
def test_a_run_described_as_the_results_dont_give_is_reported(was, now):
    assert was in RUNS
    assert run_table_problems(RUNS.replace(was, now), MADE_UP)


def test_no_false_alarms_is_reported_once_one_is_marked():
    marked = {**MADE_UP, "checks": [{**MADE_UP["checks"][0], "marked": {"right": 2, "wrong": 1, "unsure": 0, "": 0}, "wrong_prs": 1}]}
    assert any("no false alarms" in problem for problem in run_table_problems(RUNS, marked))


def test_a_count_of_a_known_sample_size_has_to_be_on_the_page():
    assert count_problems("It caught 44 of 44 planted risks.", _pairs(MADE_UP), "README.md")
    assert count_problems("The 7B found 3 of 39.", _pairs(MADE_UP), "README.md")
    assert merged_prs_problems("No false alarms on 200 merged PRs.", MADE_UP, "README.md")
