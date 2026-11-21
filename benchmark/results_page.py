"""The results page: every number the benchmarks give, from the result files.

    python benchmark/results_page.py

Each run's summary.md holds that run's numbers, and the READMEs quote a few of
them by hand, where a copy can drift. This reads every folder under
benchmark/results/ with the scorers' own functions and writes
benchmark/RESULTS.md: each number with its sample size, a 95% interval, and
how much of it a person has marked. tests/test_results_page.py fails when the
page is out of date, or when a README quotes a figure the results don't give.

Nothing here reviews a PR or calls a model. The page has no dates and no paths
from this machine, so writing it again without new results changes nothing. A
folder that can't be read, or whose files contradict each other, is listed at
the end of the page with the reason, and none of its numbers are counted.
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import bench  # noqa: E402
import build_checks  # noqa: E402
import corpus  # noqa: E402
import floating_promises  # noqa: E402
import planted  # noqa: E402
import shell_checks  # noqa: E402
import weakened_tests  # noqa: E402
from stratum_open.checks.promises import RULE as FLOATING_PROMISE  # noqa: E402

RESULTS = HERE / "results"
PAGE = HERE / "RESULTS.md"
Z = 1.959964  # 95% of a normal distribution lies within this many standard deviations
KINDS = ("models", "checks", "rules", "corpus", "awaits", "as_new", "merged", "shell", "build")
# The rules a policy has to turn on, by the setting that does it. A rule is
# one of these when its name is one listed here, or starts with one and a slash.
OFF_BY_DEFAULT = {
    "checks.ruff": ("ruff",),
    "checks.unknown_names": (planted.UNKNOWN_NAME,),
    "checks.removed_names": (planted.REMOVED_NAME,),
    "checks.floating_promises": (FLOATING_PROMISE,),
    "checks.weakened_tests": ("tests",),
    "checks.heuristics": ("heuristic",),
    "dependencies.known_vulnerabilities": ("deps/vulnerable",),
}


class Inconsistent(ValueError):
    """A result folder's files contradict each other, or hold nothing to count."""


def main() -> int:
    PAGE.write_text(render(collect()), encoding="utf-8", newline="\n")
    print(f"Wrote benchmark/{PAGE.name}")
    return 0


def wilson(hits: int, n: int) -> tuple[float, float]:
    """The 95% Wilson score interval for a share of `hits` in `n`. Unlike the
    plain plus-or-minus interval it stays inside 0 to 1 and doesn't shrink to
    nothing at 0 of n, which matters for counts as small as these."""
    share = hits / n
    spread = Z * math.sqrt(share * (1 - share) / n + Z * Z / (4 * n * n))
    low, high = ((share + Z * Z / (2 * n) + sign * spread) / (1 + Z * Z / n) for sign in (-1, 1))
    # At none and at all the end is exact, and rounding mustn't move it.
    return (0.0 if hits == 0 else low, 1.0 if hits == n else high)


# ── reading the result folders ───────────────────────────────────────────


def collect(results: Path = RESULTS) -> dict:
    """Every run's numbers by kind of run, and the folders left out with why."""
    page: dict = {**{kind: [] for kind in KINDS}, "invalid": {}, "left_out": []}
    for folder in sorted((path for path in results.iterdir() if path.is_dir()), key=lambda path: path.name):
        try:
            run = _read_folder(folder)
        except Exception as error:  # whatever stops a scorer: the folder is listed as left out, and the rest still counted
            page["left_out"].append((folder.name, _why(error)))
            continue
        page["invalid"].update(run.pop("invalid", {}))
        for kind, rows in run.items():
            page[kind] += rows
    page["models"].sort(key=lambda row: (planted._size(row["model"]), row["model"], row["variant"], row["run"]))
    page["merged"].sort(key=lambda row: (row["run"], planted._size(row["model"]), row["model"]))
    return page


def _read_folder(folder: Path) -> dict:
    run: dict = {"checks": [], "rules": []}
    if (folder / "lists.json").exists():
        _add(run, _weakened_run(folder))
    elif (folder / "checks.csv").exists():
        _add(run, _checks_run(folder))
    # A run split into groups, one per language say: each group is a checks run of its own.
    for group in sorted(path for path in folder.iterdir() if path.is_dir() and (path / "checks.csv").exists()):
        _add(run, _checks_run(group, folder.name))
    if run["checks"] and corpus.scores(folder):
        run["corpus"] = [_corpus_run(folder)]
    if any(folder.rglob("reviews.csv")):
        run["models"], run["invalid"] = _model_runs(folder)
    if (folder / "read.json").exists():
        run["awaits"], run["as_new"] = _promises_run(folder)
    elif (folder / "cases.json").exists():
        run["awaits"] = [_awaits_run(folder)]
    if (folder / "calls.csv").exists():
        run["merged"] = _merged_runs(folder)
    if (folder / "whole.json").exists():
        run["shell"] = shell_checks.scores(folder)
    elif any(folder.rglob("kept.csv")) and any(folder.rglob("prs.json")):  # build_checks.py: its kept.csv and prs.json, in a folder per project
        run["build"] = build_checks.scores(folder)
    if not any(run.get(kind) for kind in KINDS):
        raise Inconsistent("it has no results that a scorer counts")
    return run


def _add(run: dict, rows: tuple[dict, list[dict]]) -> None:
    run["checks"].append(rows[0])
    run["rules"] += rows[1]


def _why(error: Exception) -> str:
    """The reason a folder is left out. An error's own message can hold this
    machine's paths and changes between Python versions, so only its kind is
    given: running the folder's scorer shows the rest."""
    if isinstance(error, Inconsistent):
        return str(error)
    if isinstance(error, KeyError):
        return f"a file has no `{error.args[0]}` column or key"
    if isinstance(error, FileNotFoundError) and error.filename:
        return f"it has no {Path(error.filename).name}"
    return f"its scorer can't read it ({type(error).__name__})"


def _model_runs(folder: Path) -> tuple[list[dict], dict]:
    scores = planted.model_scores(folder)

    def key(row: dict) -> tuple:
        return row["model"], row["variant"], row["version"], row["case"]

    reviewed = Counter(key(row) for row in scores["reviews"])
    again = sorted({f"`{model}`" + (f" ({variant})" if variant else "") for (model, variant, _, _), times in reviewed.items() if times > 1})
    if again:
        # The scorer keeps a case's findings by model and variant, so a bug
        # caught in one of two runs would count as found in both.
        raise Inconsistent(f"{', '.join(again)} reviewed a case more than once, and the scorer can't tell two runs of one model and variant apart")
    listed = Counter(key(row) for row in scores["findings"])
    near = Counter(key(row) for row in scores["findings"] if row["near"] == "yes")
    for row in scores["reviews"]:
        if (int(row["findings"]), int(row["near"])) != (listed[key(row)], near[key(row)]):
            raise Inconsistent(f"reviews.csv and findings.csv disagree about the findings of `{row['model']}` on {row['case']}")
    stray = sorted(set(listed) - set(reviewed))
    if stray:
        raise Inconsistent(f"findings.csv has a finding of `{stray[0][0]}` on {stray[0][3]}, a review that reviews.csv doesn't have")
    return [dict(row, run=folder.name) for row in scores["rows"]], scores["invalid"]


def _checks_run(folder: Path, parent: str = "") -> tuple[dict, list[dict]]:
    """A run of `planted.py checks`, and a row for each setting in it that
    turns rules on. In a run split into groups, `parent` is the run's folder."""
    scores = planted.checks_scores(folder)
    merged = scores["merged"]
    stray = sorted({row["case"] for row in merged} - {f"{pr['repo']}#{pr['pr']}" for pr in scores["prs"]})
    if stray:
        raise Inconsistent(f"checks.csv has a finding on {stray[0]}, which prs.json doesn't list")
    _same_caught(folder, scores["planted"])
    where = {"run": parent, "group": folder.name} if parent else {"run": folder.name, "group": ""}
    return _checks_rows(where, scores["planted"], scores["controls"], merged, len(scores["prs"]))


def _weakened_run(folder: Path) -> tuple[dict, list[dict]]:
    """A run of `weakened_tests.py`: the check on its own planted cases, and
    on every list of merged PRs, whose sizes are in lists.json."""
    scores = weakened_tests.scores(folder)
    _same_caught(folder, scores["risks"])
    return _checks_rows({"run": folder.name, "group": ""}, scores["risks"], scores["controls"], scores["merged"], scores["total"]["prs"],
                        dropped=scores["dropped"])


def _same_caught(folder: Path, cases: list[dict]) -> None:
    expected = {row["case"] for row in planted._read(folder / "checks.csv") if row["set"] == "planted" and row["expected"] == "yes"}
    if expected != {case["id"] for case in cases if case["caught"]}:
        raise Inconsistent("planted.json and checks.csv disagree about which planted risks were caught")


def _setting(rule: str) -> str:
    """The setting that turns a rule on, or "" for a rule that is on by default."""
    return next((setting for setting, names in OFF_BY_DEFAULT.items() if any(rule == name or rule.startswith(name + "/") for name in names)), "")


def _checks_rows(where: dict, cases: list[dict], controls: list[dict], merged: list[dict], prs: int,
                 dropped: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """A checks run's row, and one for each setting that turns rules on, with
    its own planted cases, controls and findings on the merged PRs, as the
    run's summary counts them apart. A rule that was measured and then taken
    out of its check (`dropped`) gets a row of its own."""
    row = {**where, "risks": len(cases), "caught": sum(case["caught"] for case in cases), "missed": [case["id"] for case in cases if not case["caught"]],
           "controls": len(controls), "left_alone": sum(not case["caught"] for case in controls), **_on_merged(merged, prs)}
    rules = []
    for setting in OFF_BY_DEFAULT:
        mine = [case for case in cases if _setting(case["rule"]) == setting]
        theirs = [case for case in controls if _setting(case["rule"]) == setting]
        if mine or theirs:
            found = [finding for finding in merged if _setting(finding["rule"]) == setting]
            rules.append({**where, "setting": setting, "rules": sorted({case["rule"] for case in mine + theirs} | {finding["rule"] for finding in found}),
                          "dropped": False, "risks": len(mine), "caught": sum(case["caught"] for case in mine),
                          "controls": len(theirs), "left_alone": sum(not case["caught"] for case in theirs), **_on_merged(found, prs)})
    for rule in sorted({finding["rule"] for finding in dropped or []}):
        rules.append({**where, "setting": _setting(rule), "rules": [rule], "dropped": True, "risks": 0, "caught": 0, "controls": 0, "left_alone": 0,
                      **_on_merged([finding for finding in dropped or [] if finding["rule"] == rule], prs)})
    return row, rules


def _on_merged(merged: list[dict], prs: int) -> dict:
    """What a person made of the findings on `prs` merged PRs."""
    wrong = {row["case"] for row in merged if row["verdict"] == "wrong"}
    return {
        "prs": prs, "findings": len(merged), "flagged": len({row["case"] for row in merged}),
        "marked": {verdict: sum(row["verdict"] == verdict for row in merged) for verdict in ("right", "wrong", "unsure", "")},
        # A PR whose finding nobody has marked may yet turn out a false alarm.
        "wrong_prs": len(wrong), "open_prs": len({row["case"] for row in merged if not row["verdict"]} - wrong),
    }


def _corpus_run(folder: Path) -> dict:
    """The merged PRs of a run split into groups, added up as its summary adds
    them. The groups' planted risks are the same cases each time, so they aren't."""
    groups = corpus.scores(folder)
    every = [row for _, rows in groups.values() for row in rows]
    return {"run": folder.name, "groups": [corpus.GROUPS[group] for group in groups], **_on_merged(every, sum(len(prs) for prs, _ in groups.values()))}


def _awaits_run(folder: Path) -> dict:
    cases = json.loads((folder / "cases.json").read_text(encoding="utf-8"))
    alarms = json.loads((folder / "alarms.json").read_text(encoding="utf-8"))
    caught = sum(case["caught"] for case in cases)
    # How many PRs were checked as merged is written nowhere but the summary.
    summary = (folder / "summary.md").read_text(encoding="utf-8")
    said_caught = re.search(r"Dropped awaits caught:\*\* (\d+) of (\d+)", summary)
    said_alarms = re.search(r"Findings on the (\d+) PRs as merged:\*\* (\d+)", summary)
    if not said_caught or not said_alarms:
        raise Inconsistent("summary.md doesn't give the counts in the form dropped_awaits.py writes")
    if (int(said_caught[1]), int(said_caught[2]), int(said_alarms[2])) != (caught, len(cases), len(alarms)):
        raise Inconsistent("summary.md gives other counts than cases.json and alarms.json")
    return {"run": folder.name, "language": "Python", "caught": caught, "cases": len(cases), "prs": int(said_alarms[1]),
            "alarms": len(alarms), "alarm_prs": len({alarm["pr"] for alarm in alarms})}


def _promises_run(folder: Path) -> tuple[list[dict], list[dict]]:
    """A run of `floating_promises.py`: its dropped awaits and merged PRs, and
    the merged code it read as if every line were new."""
    scores = floating_promises.scores(*floating_promises.load(folder))
    merged = scores["merged"]
    awaits = {"run": folder.name, "language": "JavaScript and TypeScript", "caught": scores["caught"], "cases": scores["cases"], "prs": scores["prs"],
              "alarms": len(merged), "alarm_prs": len({row["case"] for row in merged})}
    as_new = [{"run": folder.name, "read": read["read"], "files": read["files"], "lines": read["lines"], "findings": len(read["findings"]),
               **{verdict: sum(row["verdict"] == verdict for row in read["findings"]) for verdict in ("right", "wrong", "unsure")}}
              for read in scores["read"]]
    return [awaits], as_new


def _merged_runs(folder: Path) -> list[dict]:
    calls, findings = planted._read(folder / "calls.csv"), planted._read(folder / "findings.csv")
    listed = Counter((row["repo"], str(row["pr"]), row["model"]) for row in findings)
    for row in calls:
        if int(row["findings"]) != listed[(row["repo"], str(row["pr"]), row["model"])]:
            raise Inconsistent(f"calls.csv and findings.csv disagree about the findings of `{row['model']}` on {row['repo']}#{row['pr']}")
    return [dict(row, run=folder.name) for row in bench.scores(calls, findings)["rows"]]


# ── writing the page ─────────────────────────────────────────────────────


def render(page: dict) -> str:
    lines = [
        "# Results",
        "",
        "Every number the benchmarks give: its sample size, its 95% interval, how many runs it comes from, and how much of it a person has marked. "
        "How each benchmark works is in the [README](README.md).",
        "",
        "This page is generated from the files in [results/](results/), so don't edit it. After adding or marking a run, run "
        "`python benchmark/results_page.py`. A test fails when the page is out of date, or when a README quotes a figure the results don't give.",
        "",
        "**The intervals** are 95% Wilson score intervals, which hold up for small samples and for counts near none or all. "
        "Each takes the cases as a sample of the PRs a project might get, and says how far a count this small can be trusted. "
        "It can't say how well these cases stand for yours.",
    ]
    if page["models"]:
        lines += _models_section(page)
    if page["checks"]:
        lines += _checks_section(page)
    if page["awaits"]:
        lines += _awaits_section(page)
    if page["shell"]:
        lines += _shell_section(page["shell"])
    if page["build"]:
        lines += _build_section(page["build"])
    if page["merged"]:
        lines += _merged_section(page["merged"])
    lines += ["", "## Not included", ""]
    if page["left_out"]:
        lines += ["These folders under results/ are in none of the tables above:", ""]
        lines += _table(["Folder", "Why"], [[_link(name), why] for name, why in page["left_out"]])
    else:
        lines.append("Nothing: every folder under results/ is in a table above.")
    return "\n".join(lines) + "\n"


def _models_section(page: dict) -> list[str]:
    rows = page["models"]
    runs = Counter((row["model"], row["variant"]) for row in rows)
    lines = [
        "",
        "## The model's review: planted bugs",
        "",
        "Real merged PRs, each with one bug planted on a line it adds. Where a run has the clean control, the same PRs were reviewed as merged too. "
        "Each row is one run of one model and variant: no row pools runs.",
        "",
        "- **Variant:** the run's name for what it changed in the Action's settings. `-` is the settings as they come.",
        "- **Cases:** how many PRs the run reviewed with the bug. *(dev)* is a run on the dev set alone, the cases a change is tuned on.",
        f"- **Found:** a finding within {planted.LINES_NEAR} lines of the bug, which a person read and marked as describing it.",
        "- **At least:** a finding near a bug isn't marked yet, so the count is a lower bound. "
        "Its interval runs up to where it would end if every such case were marked found.",
        f"- **Near the line when clean:** clean PRs with a finding within {planted.LINES_NEAR} lines of where the bug goes.",
        "- **Comments on the clean PRs:** the model's findings on the PRs as merged. Each is a possible false alarm, and all are counted, marked or not.",
        "- **Failed calls** and **Median time per PR** cover the reviews with the bug and the clean ones together.",
        "- **Not marked:** findings on the PRs with the bug that have no verdict yet, and how many of those are near a bug.",
        "- **Runs:** how many runs of that model and variant are on this page. Where there's more than one, the next table sets them side by side.",
        "",
    ]
    lines += _table(
        ["Model", "Variant", "Run", "Cases", "Found", "95% interval", "Near the line when clean", "Comments on the clean PRs", "Per clean PR",
         "Failed calls", "Median time per PR", "Not marked", "Runs"],
        [[
            f"`{row['model']}`", row["variant"] or "-", _link(row["run"]), _cases(row), _count(row["found"], row["shown"], row["open"]),
            _interval(row["found"], row["shown"], row["open"]),
            f"{row['near_clean']} of {row['clean']}" if row["clean"] else "-",
            f"{row['on_clean']} on {row['clean']}" if row["clean"] else "-",
            f"{row['on_clean'] / row['clean']:.2f}" if row["clean"] else "-",
            f"{row['failed_calls']} of {row['calls']}", f"{row['seconds']:.0f}s",
            f"{row['unmarked']}" + (f" ({row['unmarked_near']} near a bug)" if row["unmarked_near"] else ""),
            str(runs[(row["model"], row["variant"])]),
        ] for row in rows],
    )
    again = [[
        f"`{model}`", variant or "-", str(times),
        "; ".join(f"{_count(row['found'], row['shown'], row['open'])} in {_link(row['run'])}" for row in rows if (row["model"], row["variant"]) == (model, variant)),
    ] for (model, variant), times in runs.items() if times > 1]
    if again:
        lines += [
            "",
            "### Run more than once",
            "",
            "The same review can catch different bugs from one run to the next, so one run's count isn't the last word. These models and variants have more than one:",
            "",
        ]
        lines += _table(["Model", "Variant", "Runs", "Found in each run"], again)
    for case, why in sorted(page["invalid"].items()):
        lines += ["", f"Left out of every run: {case}. {why}"]
    return lines


def _checks_section(page: dict) -> list[str]:
    lines = [
        "",
        "## The checks that need no model",
        "",
        "- **Run:** a run split into groups, such as one per language, has a row for each group.",
        "- **Planted risks caught:** each risk is a PR adding files with one problem in them. Caught means the rule written for it fired.",
        "- **Controls left alone:** files a rule must not flag, where the run has them.",
        "- **Merged PRs:** real merged PRs as they are, where every finding is a possible false alarm. "
        "A person marks each one right (what it says is true) or wrong (a false alarm).",
        "- **PRs with a wrong finding:** the false-alarm rate. It reads *at least* while a finding is still not marked.",
        "",
    ]
    lines += _table(
        ["Run", "Planted risks caught", "95% interval", "Missed", "Controls left alone", "Merged PRs", "Findings on them", "Marked right", "Marked wrong",
         "Unsure", "Not marked", "PRs with a wrong finding", "95% interval"],
        [[
            _link(row["run"], row["group"]), _count(row["caught"], row["risks"]), _interval(row["caught"], row["risks"]),
            ", ".join(f"`{name}`" for name in row["missed"]) or "-", _count(row["left_alone"], row["controls"]) if row["controls"] else "-",
            *_merged_cells(row),
        ] for row in sorted(page["checks"], key=_group_order)],
    )
    if page["rules"]:
        lines += [
            "",
            "### Rules that are off by default",
            "",
            "A run that turns such a rule on measures it on cases of its own, and its summary counts them apart. "
            "They are in the run's row above too.",
            "",
            "- **Setting:** what turns the rules on in `.stratum/policy.yml`.",
            "- **Planted cases caught** and **Controls left alone:** that setting's cases alone.",
            "- **Findings on them:** the findings of those rules on the run's merged PRs.",
            "- A rule *taken out* was measured on the merged PRs and then removed from its check, so it has no planted cases.",
            "",
        ]
        lines += _table(
            ["Run", "Setting", "Rules", "Planted cases caught", "95% interval", "Controls left alone", "95% interval", "Merged PRs", "Findings on them",
             "Marked right", "Marked wrong", "Unsure", "Not marked", "PRs with a wrong finding", "95% interval"],
            [[
                _link(row["run"], row["group"]), f"`{row['setting']}`" if row["setting"] else "-", _rules(row["rules"]) + (", taken out" if row["dropped"] else ""),
                _count(row["caught"], row["risks"]) if row["risks"] else "-", _interval(row["caught"], row["risks"]),
                _count(row["left_alone"], row["controls"]) if row["controls"] else "-", _interval(row["left_alone"], row["controls"]),
                *_merged_cells(row),
            ] for row in sorted(page["rules"], key=_group_order)],
        )
    if page["corpus"]:
        lines += [
            "",
            "### Runs split into groups, all groups together",
            "",
            "Each group is a row above, with the same planted risks each time, so only the merged PRs are added up.",
            "",
        ]
        lines += _table(
            ["Run", "Groups", "Merged PRs", "Findings on them", "Marked right", "Marked wrong", "Unsure", "Not marked", "PRs with a wrong finding", "95% interval"],
            [[_link(row["run"]), ", ".join(row["groups"]), *_merged_cells(row)] for row in page["corpus"]],
        )
    return lines


def _merged_cells(row: dict) -> list[str]:
    """A checks row's merged PRs, its findings on them and how they were marked."""
    return [
        str(row["prs"]), f"{row['findings']} on {row['flagged']} PRs" if row["findings"] else "0",
        str(row["marked"]["right"]), str(row["marked"]["wrong"]), str(row["marked"]["unsure"]), str(row["marked"][""]),
        _count(row["wrong_prs"], row["prs"], row["open_prs"]), _interval(row["wrong_prs"], row["prs"], row["open_prs"]),
    ]


def _group_order(row: dict) -> tuple:
    """A run's groups in the order its summary gives them."""
    order = list(corpus.GROUPS)
    return row["run"], order.index(row["group"]) if row["group"] in order else len(order), row["group"]


def _rules(rules: list[str]) -> str:
    if len(rules) <= 4:
        return ", ".join(f"`{rule}`" for rule in rules)
    return f"{len(rules)} rules, " + ", ".join(f"`{prefix}/*`" for prefix in sorted({rule.split('/')[0] for rule in rules}))


def _awaits_section(page: dict) -> list[str]:
    lines = [
        "",
        "## The missing-await checks",
        "",
        "- **Language:** Python's check is on by default. The one for JavaScript and TypeScript is off unless `checks.floating_promises` is on.",
        "- **Dropped awaits caught:** every `await` the merged PRs add, dropped one at a time. Caught means the check flagged that line.",
        "- **PRs with a finding:** the same PRs as merged, where any finding is a possible false alarm. Nobody marks these, so each one counts.",
        "",
    ]
    lines += _table(
        ["Run", "Language", "Dropped awaits caught", "95% interval", "Merged PRs", "Findings on them", "PRs with a finding", "95% interval"],
        [[
            _link(row["run"]), row["language"], _count(row["caught"], row["cases"]), _interval(row["caught"], row["cases"]), str(row["prs"]), str(row["alarms"]),
            _count(row["alarm_prs"], row["prs"]), _interval(row["alarm_prs"], row["prs"]),
        ] for row in page["awaits"]],
    )
    if page["as_new"]:
        lines += [
            "",
            "### Merged code read as if it were new",
            "",
            "Merged PRs add few lines, so a run can also read merged code as if a PR had just added every line of it. "
            "A finding there is right when nothing waits for the promise by mistake, and wrong (a false alarm) when the project means to leave the call running.",
            "",
            "- **Right, of those marked:** findings marked right, of those marked right or wrong.",
            "",
        ]
        lines += _table(
            ["Run", "Read", "Files", "Lines", "Findings", "Right", "Wrong", "Unsure", "Not marked", "Right, of those marked", "95% interval"],
            [[
                _link(row["run"]), row["read"], f"{row['files']:,}", f"{row['lines']:,}", str(row["findings"]), str(row["right"]), str(row["wrong"]),
                str(row["unsure"]), str(row["findings"] - row["right"] - row["wrong"] - row["unsure"]),
                _count(row["right"], row["right"] + row["wrong"]) if row["right"] + row["wrong"] else "-", _interval(row["right"], row["right"] + row["wrong"]),
            ] for row in page["as_new"]],
        )
    return lines


def _shell_section(rows: list[dict]) -> list[str]:
    lines = [
        "",
        "## ShellCheck and actionlint",
        "",
        "Both are off unless their setting is on. Each ran on merged PRs that change a shell script or a workflow, where any finding is a possible false alarm, "
        "and on the whole of those files read as if every line were new. Each finding is marked right when what it says is true of the code, "
        "and wrong (a false alarm) when the code works as meant.",
        "",
    ]
    return lines + _table(
        ["Setting", "Run", "Merged PRs", "Added lines", "Findings on them", "Files read whole", "Lines", "Findings on them", "Right", "Wrong", "Unsure",
         "Right, of those marked", "95% interval"],
        [[
            f"`{row['setting']}`", _link(row["run"]), str(row["prs"]), f"{row['lines']:,}", str(row["findings"]), str(row["files"]), f"{row['file_lines']:,}",
            str(row["whole"]), str(row["whole_right"]), str(row["whole_wrong"]), str(row["whole_unsure"]),
            _count(row["whole_right"], row["whole_right"] + row["whole_wrong"]) if row["whole_right"] + row["whole_wrong"] else "-",
            _interval(row["whole_right"], row["whole_right"] + row["whole_wrong"]),
        ] for row in rows],
    )


def _build_section(rows: list[dict]) -> list[str]:
    lines = [
        "",
        "## go vet and Clippy",
        "",
        "Both are off unless their setting is on, and both build the PR's code. Each ran on merged PRs checked out at their head, where any finding is a "
        "possible false alarm. Every issue the programs reported on a line a PR adds is marked right when what it says is true of the code, "
        "and wrong (a false alarm) when the code works as meant. A PR didn't build when a coverage line says so, or when the program built nothing.",
        "",
    ]
    return lines + _table(
        ["Setting", "Run", "Merged PRs", "Added lines", "Findings on them", "Right", "Wrong", "Unsure", "Didn't build", "Past the review's limit"],
        [[
            f"`{row['setting']}`", _link(row["run"]), str(row["prs"]), f"{row['lines']:,}", str(row["findings"]), str(row["right"]), str(row["wrong"]),
            str(row["unsure"]), str(row["unbuilt"]), f"{row['slow']} (over {row['limit']} s)",
        ] for row in rows],
    )


def _merged_section(rows: list[dict]) -> list[str]:
    lines = [
        "",
        "## The model's review: merged PRs as they are",
        "",
        "Real merged PRs with nothing planted, so each finding needs a maintainer to mark it. Until one does, a run has times and counts but no share right.",
        "",
        "- **Right, of those marked:** findings marked right, of those marked right or wrong.",
        "- **Not marked:** findings with no verdict yet, or marked unsure.",
        "",
    ]
    return lines + _table(
        ["Model", "Run", "PRs", "Findings", "Per PR", "Right, of those marked", "95% interval", "Not marked", "Failed calls", "Median time per PR"],
        [[
            f"`{row['model']}`", _link(row["run"]), str(row["prs"]), str(row["findings"]), f"{row['findings'] / row['prs']:.1f}",
            _count(row["right"], row["right"] + row["wrong"]) if row["right"] + row["wrong"] else "-",
            _interval(row["right"], row["right"] + row["wrong"]), str(row["unmarked"]),
            f"{row['failed_calls']} of {row['calls']}", f"{row['seconds']:.0f}s",
        ] for row in rows],
    )


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"] + ["| " + " | ".join(row) + " |" for row in rows]


def _link(run: str, group: str = "") -> str:
    if group:
        return f"[{run}](results/{run}/), [{corpus.GROUPS.get(group, group)}](results/{run}/{group}/)"
    return f"[{run}](results/{run}/)"


def _cases(row: dict) -> str:
    """How many cases a run reviewed, and which set when it wasn't both."""
    return f"{row['planted']}" + (f" ({row['sets'][0]})" if len(row["sets"]) == 1 and row["sets"][0] else "")


def _count(hits: int, n: int, unsettled: int = 0) -> str:
    return ("at least " if unsettled else "") + f"{hits} of {n}"


def _interval(hits: int, n: int, unsettled: int = 0) -> str:
    """The interval for `hits` of `n`. With `unsettled` cases that marking may
    yet add, its upper end is the one for the most the count could become."""
    if not n:
        return "-"
    return f"{_percent(wilson(hits, n)[0])} to {_percent(wilson(min(n, hits + unsettled), n)[1])}"


def _percent(share: float) -> str:
    """Whole percents, with a decimal only near the ends, where it's the whole story."""
    value = share * 100
    return f"{value:.0f}%" if value in (0, 100) or 10 <= round(value) <= 90 else f"{value:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
