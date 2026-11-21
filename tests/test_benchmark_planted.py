"""The planted-bug benchmark's case files and the harness that plants them
(benchmark/planted.py). No model and no network: PRs are read only where
`planted.py fetch` has already cached them."""

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import pytest
from fakes import FakeModel

from stratum_open.diff import parse_diff, render, with_context

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
sys.path.insert(0, str(BENCHMARK))

import planted  # noqa: E402

CASE_FILES = sorted(path.name for path in (BENCHMARK / "planted").glob("bugs*.json"))

NEW = """import time


def fetch(session, url):
    attempts = 0
    while True:
        response = session.get(url)
        if response.ok or attempts >= 3:
            return response
        attempts += 1
        time.sleep(attempts)


def close(session):
    session.flush()
    session.close()
    session.closed = True
    return None
"""
DIFF = """diff --git a/app/client.py b/app/client.py
--- a/app/client.py
+++ b/app/client.py
@@ -1,6 +1,12 @@
 import time


 def fetch(session, url):
-    return session.get(url)
+    attempts = 0
+    while True:
+        response = session.get(url)
+        if response.ok or attempts >= 3:
+            return response
+        attempts += 1
+        time.sleep(attempts)

@@ -8,3 +14,5 @@ def close(session):
 def close(session):
+    session.flush()
     session.close()
+    session.closed = True
     return None
"""
ONE_LINE = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,2 +1,3 @@
 first = 1
+second = 2
 third = 3
"""


def case(line: int, find: str, replace: str | None, path: str = "app/client.py") -> dict:
    return {"repo": "acme/widget", "pr": 7, "path": path, "line": line, "kind": "dropped-statement" if replace is None else "wrong-constant",
            "set": "dev", "find": find, "replace": replace, "why": "The call is retried without end."}


def cases(name: str) -> list[dict]:
    return json.loads((BENCHMARK / "planted" / name).read_text(encoding="utf-8"))


def key(bug: dict) -> tuple[str, int]:
    return bug["repo"], int(bug["pr"])


# ── the case files ───────────────────────────────────────────────────────


@pytest.mark.parametrize("path", sorted((BENCHMARK / "planted" / "variants").glob("*.yml")), ids=lambda path: path.stem)
def test_a_review_variant_sets_the_logic_pass_and_context_itself(path):
    # A model family's settings (profiles.MODEL_SETTINGS) fill in what a variant leaves out, so a variant that left these
    # out would measure something else once a family's default changed: gpt-oss gets both since 21 Nov.
    import yaml

    review = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("review")
    if review is not None:
        assert {"logic_pass", "context_lines"} <= set(review), path.name
        assert not planted.policy_for(8192, path.read_text(encoding="utf-8")).errors


@pytest.mark.parametrize("name", CASE_FILES)
def test_every_case_is_well_formed(name):
    bugs = cases(name)
    assert [(key(bug), planted.case_format(bug)) for bug in bugs if planted.case_format(bug)] == []
    assert [pr for pr, count in Counter(key(bug) for bug in bugs).items() if count > 1] == []


def test_no_pr_is_in_two_case_files_and_the_second_set_reviews_none_of_the_clean_prs():
    """`score` tells a run's case file by its PRs, and a PR the checks were
    measured on as merged shouldn't come back with a bug in it."""
    first, second = {key(bug) for bug in cases("bugs.json")}, {key(bug) for bug in cases("bugs-2.json")}
    assert first & second == set()
    assert second & {key(item) for item in cases("clean.json")} == set()


def test_the_second_set_is_half_dev_and_half_test_for_each_kind():
    counts = Counter((bug["kind"], bug["set"]) for bug in cases("bugs-2.json"))
    kinds = {kind for kind, _ in counts}
    assert kinds == {"swapped-arguments", "wrong-variable", "wrong-constant", "dropped-statement"}
    assert all(counts[(kind, "dev")] == counts[(kind, "test")] for kind in kinds)
    assert kinds.isdisjoint(bug["kind"] for bug in cases("bugs.json"))


def test_a_dropped_statement_case_has_no_replacement_and_no_other_case_drops():
    for name in CASE_FILES:
        for bug in cases(name):
            assert (bug["replace"] is None) == (bug["kind"] == "dropped-statement"), key(bug)


@pytest.mark.parametrize("name", CASE_FILES)
def test_cached_cases_hold_against_their_prs(name):
    """What `planted.py validate` checks, for the cases `planted.py fetch` has
    cached (none on a fresh checkout). The second set also has to be free of
    notes: its `find` is on one added line only."""
    def cached(bug: dict) -> bool:
        stem = f"{bug['repo'].replace('/', '__')}__{bug['pr']}"
        return (planted.CACHE / f"{stem}.json").exists() and (planted.CACHE / f"{stem}__contents.json").exists()

    def load(bug: dict):
        files = parse_diff(planted._fetch(bug["repo"], int(bug["pr"]))[1])
        return files, planted._head_contents(bug["repo"], int(bug["pr"]), files)

    bugs = [bug for bug in cases(name) if cached(bug)]
    if not bugs:
        pytest.skip(f"no PR of {name} is cached: python benchmark/planted.py fetch --bugs benchmark/planted/{name}")
    found = planted.validate(bugs, load)
    assert [(case, problems) for case, problems, _ in found if problems] == []
    if name != "bugs.json":
        assert [(case, notes) for case, _, notes in found if notes] == []


# ── planting ─────────────────────────────────────────────────────────────


def test_a_replaced_line_keeps_every_line_number():
    files = parse_diff(DIFF)
    line = planted.plant(case(8, "        if response.ok or attempts >= 3:", "        if response.ok or attempts >= 30:"), files)
    assert (line.new, line.text) == (8, "        if response.ok or attempts >= 30:")
    assert [number for number, _ in files[0].added()] == [5, 6, 7, 8, 9, 10, 11, 15, 17]


def test_a_dropped_line_leaves_the_diff_the_pr_would_have_without_it():
    bug = case(10, "        attempts += 1", None)
    files = parse_diff(DIFF)
    beside = planted.plant(bug, files)
    assert (beside.new, beside.text) == (10, "        time.sleep(attempts)")  # the line that followed, now in its place
    assert [text for _, text in files[0].added()].count("        attempts += 1") == 0
    assert [number for number, _ in files[0].added()] == [5, 6, 7, 8, 9, 10, 14, 16]  # later lines, and the later hunk, moved up
    assert files[0].hunks[1].new_start == 13

    contents = planted.with_line({"app/client.py": NEW}, bug, None)
    assert contents["app/client.py"] == NEW.replace("        attempts += 1\n", "")
    # The file's text agrees with the diff, or no context would be shown around the hunks.
    assert with_context(files[0], contents["app/client.py"], 2) is not files[0]
    assert planted.shown([render(files[0])], "app/client.py", beside)


def test_the_line_beside_a_gap_can_be_one_the_pr_left_as_it_was():
    files = parse_diff(DIFF)
    beside = planted.plant(case(17, "    session.closed = True", None), files)
    assert (beside.kind, beside.new, beside.text) == (" ", 17, "    return None")
    assert planted.shown([render(files[0])], "app/client.py", beside)
    assert not planted.shown([render(files[0], files[0].hunks[:1])], "app/client.py", beside)  # a prompt clipped before that hunk


def test_a_line_dropped_from_the_end_of_a_hunk_is_shown_by_the_line_above():
    files, _ = planted._added_files({"m.py": "first = 1\nsecond = 2\n"})
    beside = planted.plant(case(2, "second = 2", None, path="m.py"), files)
    assert (beside.new, beside.text) == (1, "first = 1")


def test_a_hunk_must_add_another_line_for_one_to_be_dropped_from_it():
    """Without its only added line a hunk isn't in the PR's diff at all, and a
    reviewer of the diff couldn't know anything was missing."""
    bug = case(2, "second = 2", None, path="m.py")
    with pytest.raises(SystemExit, match="only line its hunk adds"):
        planted.plant(bug, parse_diff(ONE_LINE))
    found, _ = planted.case_problems(bug, parse_diff(ONE_LINE), {"m.py": "first = 1\nsecond = 2\nthird = 3\n"})
    assert "only line its hunk adds" in found[0]


def test_planting_fails_loudly_on_a_stale_case():
    with pytest.raises(SystemExit, match="reads"):
        planted.plant(case(10, "        attempts += 2", None), parse_diff(DIFF))
    with pytest.raises(SystemExit, match="no added line"):
        planted.plant(case(16, "    session.close()", None), parse_diff(DIFF))


def test_the_clean_control_keeps_a_dropped_line():
    bug = case(10, "        attempts += 1", None)
    files = parse_diff(DIFF)
    line = planted.original(bug, files)
    assert (line.new, line.text) == (10, "        attempts += 1")
    assert planted.with_line({"app/client.py": NEW}, bug, line.text) == {"app/client.py": NEW}


# ── validate ─────────────────────────────────────────────────────────────


def problems(text: str, line: int, find: str, replace: str | None) -> tuple[list[str], list[str]]:
    """A case on a new file m.py holding `text`, checked against it."""
    return planted.case_problems(case(line, find, replace, path="m.py"), *planted._added_files({"m.py": text}))


def test_a_sound_case_has_no_problems_and_no_notes():
    assert problems("limit = 10\nprint(limit)\n", 1, "limit = 10", "limit = 100") == ([], [])
    assert planted.case_problems(case(10, "        attempts += 1", None), parse_diff(DIFF), {"app/client.py": NEW}) == ([], [])


def test_a_case_must_be_on_a_line_the_pr_adds_that_reads_find():
    found, _ = planted.case_problems(case(16, "    session.close()", "    session.open()"), parse_diff(DIFF), {"app/client.py": NEW})
    assert "adds no line app/client.py:16" in found[0]
    found, _ = planted.case_problems(case(17, "    session.closed = False", "    session.closed = None"), parse_diff(DIFF), {"app/client.py": NEW})
    assert "not `find`" in found[0]


def test_the_planted_file_must_still_parse():
    found, _ = problems("limit = 10\nprint(limit)\n", 1, "limit = 10", "limit = (10")
    assert "doesn't parse with the bug planted" in found[0]
    found, _ = problems("if ready:\n    start()\nstop()\n", 2, "    start()", None)  # an `if` left with no body
    assert "doesn't parse with the bug planted" in found[0]


def test_only_a_whole_statement_can_be_dropped():
    found, _ = problems("total = add(\n    first,\n    second,\n)\n", 3, "    second,", None)
    assert "isn't a whole statement" in found[0]


def test_the_cache_must_hold_the_file_as_the_pr_left_it():
    files, _ = planted._added_files({"m.py": "limit = 10\n"})
    bug = case(1, "limit = 10", "limit = 100", path="m.py")
    assert "has no text of m.py" in planted.case_problems(bug, files, {})[0][0]
    assert "doesn't read `find`" in planted.case_problems(bug, files, {"m.py": "limit = 11\n"})[0][0]


def test_a_find_or_a_replace_the_diff_holds_elsewhere_is_noted_not_refused():
    found, notes = problems("return_code = 0\nlimit = 10\nlimit = 10\n", 2, "limit = 10", "limit = 100")
    assert found == [] and "on 2 added lines" in notes[0]
    found, notes = problems("limit = 100\nlimit = 10\n", 2, "limit = 10", "limit = 100")
    assert found == [] and "already an added line" in notes[0]
    found, notes = planted.case_problems(case(5, "    attempts = 0", "    return session.get(url)"), parse_diff(DIFF), {"app/client.py": NEW})
    assert found == [] and "a line the PR removes" in notes[0]


def test_validate_checks_the_fields_before_it_reads_any_pr():
    def load(bug):
        raise AssertionError("a malformed case reached its PR")

    good = case(1, "limit = 10", "limit = 100", path="m.py")
    bad = [
        {key: value for key, value in good.items() if key != "why"},
        {**good, "set": "train"},
        {**good, "line": "1"},
        {**good, "replace": good["find"]},
        {**good, "note": "extra"},
        "pallets/flask#1",
    ]
    found = planted.validate(bad, load)
    assert [case for case, _, _ in found] == [f"case {n}" for n in range(1, 7)]
    assert [problems[0] for _, problems, _ in found] == [
        "has no `why`", "`set` is neither dev nor test", "`line` isn't a positive whole number",
        "`replace` is the same as `find`", "has an unknown key `note`", "isn't an object",
    ]
    assert planted.validate({"cases": []}, load) == [("the file", ["isn't a list of cases"], [])]


def test_validate_refuses_a_pr_listed_twice():
    good = case(1, "limit = 10", "limit = 100", path="m.py")
    found = planted.validate([good, dict(good)], lambda bug: planted._added_files({"m.py": "limit = 10\n"}))
    assert found == [("acme/widget#7", ["is in the file twice: a PR holds one planted bug"], [])]


# ── run and score ────────────────────────────────────────────────────────


def rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def run(tmp_path, monkeypatch, bug: dict, reply: str, *, clean: bool = False) -> tuple[dict, str]:
    """`planted.py run` on one case of a made-up PR, with a fake model."""
    models = []

    class Model(FakeModel):
        def __init__(self, endpoint, name, **kwargs):
            super().__init__(reply, name=name)
            models.append(self)

    monkeypatch.setattr(planted, "_fetch", lambda repo, number: ({"title": "Retry a failed fetch", "body": "Retries three times."}, DIFF))
    monkeypatch.setattr(planted, "_head_contents", lambda repo, number, files: {"app/client.py": NEW})
    monkeypatch.setattr(planted, "Model", Model)
    (tmp_path / "bugs.json").write_text(json.dumps([bug]), encoding="utf-8")
    out = tmp_path / ("clean" if clean else "planted")
    args = argparse.Namespace(bugs=str(tmp_path / "bugs.json"), model="fake-7b@http://localhost:1/v1", out=str(out), shard="1/1",
                              context_tokens=8192, set="all", clean=clean, policy=None, variant="")
    assert planted._run(args) == 0
    return rows(out / "reviews.csv")[0], models[0].prompts[0][1]


def finding(code: str) -> str:
    return json.dumps({"summary": "Retries.", "matches_description": "yes", "findings": [{
        "file": "app/client.py", "code": code, "severity": "high", "category": "bug", "title": "The loop never ends",
        "detail": "attempts is never increased, so a failing call is retried without end.", "fix": "Count the attempts."}]})


def test_run_reviews_a_dropped_line_as_a_pr_that_never_wrote_it(tmp_path, monkeypatch):
    bug = case(10, "        attempts += 1", None)
    review, prompt = run(tmp_path, monkeypatch, bug, finding("        if response.ok or attempts >= 3:"))
    assert "attempts += 1" not in prompt and "    10 +          time.sleep(attempts)" in prompt
    assert (review["shown"], review["findings"], review["near"]) == ("yes", "1", "1")  # two lines above the gap

    review, prompt = run(tmp_path, monkeypatch, bug, finding("    session.closed = True"))
    assert (review["shown"], review["findings"], review["near"]) == ("yes", "1", "0")  # in the other function

    review, prompt = run(tmp_path, monkeypatch, bug, finding("        attempts += 1"), clean=True)
    assert "    10 +          attempts += 1" in prompt
    assert (review["version"], review["shown"], review["near"]) == ("clean", "yes", "1")


def test_score_leaves_out_the_invalid_cases_of_the_case_file_a_run_used(tmp_path, monkeypatch):
    def bug(repo: str, **more) -> dict:
        return {**case(1, "a = 1", "a = 2"), "repo": repo, **more}

    def review(bug: dict) -> dict:
        return {"case": f"{bug['repo']}#{bug['pr']}", "set": "dev", "kind": bug["kind"], "path": bug["path"], "line": 1, "model": "fake:7b",
                "variant": "", "version": "planted", "seconds": 1.0, "shown": "yes", "calls": 1, "failed_calls": 0, "findings": 0, "near": 0, "error": ""}

    first = [bug("acme/one"), bug("acme/two", invalid="Not a bug: the first set's.")]
    second = [bug("acme/three"), bug("acme/four", invalid="Not a bug: the second set's.")]
    folder = tmp_path / "planted"
    folder.mkdir()
    (folder / "bugs.json").write_text(json.dumps(first), encoding="utf-8")
    (folder / "bugs-2.json").write_text(json.dumps(second), encoding="utf-8")
    monkeypatch.setattr(planted, "BUGS", folder / "bugs.json")
    for name, ran in (("first", first), ("second", second)):
        out = tmp_path / name / "raw"
        out.mkdir(parents=True)
        planted._write(out / "reviews.csv", planted.REVIEW_COLUMNS, [review(b) for b in ran])
        planted._write(out / "findings.csv", planted.FINDING_COLUMNS, [])
    first_summary, second_summary = planted._model_summary(tmp_path / "first"), planted._model_summary(tmp_path / "second")
    assert "1 real merged PRs" in first_summary and "Left out: acme/two#7. Not a bug: the first set's." in first_summary
    assert "acme/four" not in first_summary  # the other file's cases don't touch this run's summary
    assert "1 real merged PRs" in second_summary and "Left out: acme/four#7. Not a bug: the second set's." in second_summary
    assert "acme/two" not in second_summary
