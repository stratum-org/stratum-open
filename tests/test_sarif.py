import base64
import gzip
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from fakes import FakeGitHub, FakeModel
from test_action import workflow
from test_runner import DIFF, model_answer

from stratum_open import action, cli, sarif
from stratum_open.findings import SEVERITIES, Finding

ROOT = Path(__file__).parent.parent
# The official SARIF 2.1.0 schema, as OASIS publishes it (tests/sarif/SOURCE.txt says where from).
SCHEMA = json.loads((Path(__file__).parent / "sarif" / "sarif-schema-2.1.0.json").read_text(encoding="utf-8"))
VALIDATOR = jsonschema.Draft4Validator(SCHEMA, format_checker=jsonschema.FormatChecker())


def valid(log: dict) -> dict:
    errors = sorted(VALIDATOR.iter_errors(log), key=lambda e: list(e.absolute_path))
    assert not errors, [f"{list(e.absolute_path)}: {e.message}" for e in errors[:5]]
    return log


def every_severity() -> list[Finding]:
    """The checks' findings and the model's, of every severity: on lines, on a whole file, and on the PR as a whole."""
    return [
        Finding(title="Bandit B101: assert used", detail="Bandit flags this line.", severity="low", category="security", path="app/users.py",
                line_start=9, source="check", rule="bandit/B101", fingerprint="d" * 12),
        Finding(title="Unknown id", detail="fetchone returns None\nfor an unknown id.", severity="medium", category="bug", path="app/users.py",
                line_start=5, line_end=6, suggestion="Check for None.", source="model", fingerprint="c" * 12),
        Finding(title="`reqeusts` looks like `requests`", detail="A near-copy of a popular name.", severity="high", category="supply-chain",
                path="requirements.txt", line_start=3, source="check", rule="deps/lookalike", fingerprint="b" * 12),
        Finding(title="pull_request_target runs the PR's own code", detail="Line 12 checks out the PR's code.", severity="critical",
                category="workflow", path=".github/workflows/ci.yml", line_start=14, suggestion="Check out the base branch.",
                source="check", rule="workflow/pwn-request", fingerprint="a" * 12),
        Finding(title="A public function changed its signature", detail="Callers break.", severity="medium", category="breaking-change",
                path="app/api.py", source="check", rule="heuristic/breaking-change", fingerprint="e" * 12),
        Finding(title="Does more than the description says", detail="It also renames the table.", severity="low", category="maintainability",
                source="model", fingerprint="f" * 12),
    ]


def test_the_vendored_schema_is_the_official_one():
    jsonschema.Draft4Validator.check_schema(SCHEMA)
    assert SCHEMA["id"] == sarif.SCHEMA  # the output's $schema names it


def test_the_output_validates_against_the_sarif_schema(tmp_path):
    path = tmp_path / "out.sarif"
    sarif.write(path, every_severity())
    log = valid(json.loads(path.read_text(encoding="utf-8")))
    assert log == sarif.to_sarif(every_severity())
    # The findings kept outside the results have a result's shape: as results, the file still validates.
    run = log["runs"][0]
    valid({**log, "runs": [{**run, "results": run["results"] + run["properties"]["findingsWithoutLine"]}]})
    assert valid(sarif.to_sarif([]))["runs"][0]["results"] == []  # a PR with nothing found closes its alerts


def test_every_severity_maps_to_its_level_rule_and_location():
    run = sarif.to_sarif(every_severity())["runs"][0]
    assert [(r["ruleId"], r["level"], r["properties"]) for r in run["results"]] == [  # the most severe first
        ("workflow/pwn-request", "error", {"source": "check", "severity": "critical"}),
        ("deps/lookalike", "error", {"source": "check", "severity": "high"}),
        ("model/bug", "warning", {"source": "model", "severity": "medium"}),  # its own rule, model:unknown-id, changes with its wording
        ("bandit/B101", "note", {"source": "check", "severity": "low"}),
    ]
    assert [r["partialFingerprints"] for r in run["results"]] == [{"primaryLocationLineHash": c * 12} for c in "abcd"]
    assert [r["locations"] for r in run["results"]] == [
        [{"physicalLocation": {"artifactLocation": {"uri": path, "uriBaseId": "%SRCROOT%"}, "region": {"startLine": start, "endLine": end}}}]
        for path, start, end in [(".github/workflows/ci.yml", 14, 14), ("requirements.txt", 3, 3), ("app/users.py", 5, 6), ("app/users.py", 9, 9)]
    ]
    # The title first, as code scanning shows only the first sentence where there's little room.
    assert run["results"][2]["message"]["text"] == "Unknown id. fetchone returns None for an unknown id. Fix: Check for None."
    assert run["results"][1]["message"]["text"] == "`reqeusts` looks like `requests`. A near-copy of a popular name."


def test_each_rule_has_what_code_scanning_asks_for():
    rules = sarif.to_sarif(every_severity())["runs"][0]["tool"]["driver"]["rules"]
    assert [rule["id"] for rule in rules] == [
        "workflow/pwn-request", "deps/lookalike", "model/bug", "bandit/B101", "heuristic/breaking-change", "model/maintainability",
    ]
    for rule in rules:
        for key in ("shortDescription", "fullDescription"):
            assert 0 < len(rule[key]["text"]) <= 1024, rule
        assert rule["help"]["text"] and sarif.HELP in rule["help"]["text"] and rule["helpUri"] == sarif.HELP
        assert len(rule["properties"]["tags"]) <= 10
    by_id = {rule["id"]: rule for rule in rules}
    assert by_id["model/bug"]["properties"]["tags"] == ["model", "bug"]  # so a team can leave the model's findings out
    assert by_id["workflow/pwn-request"]["properties"]["tags"] == ["check", "workflow"]


def test_findings_without_a_line_stay_out_of_the_results_and_are_kept_in_the_run(tmp_path):
    log = sarif.to_sarif(every_severity())
    run = log["runs"][0]
    assert all("region" in r["locations"][0]["physicalLocation"] for r in run["results"])  # code scanning shows a result only on a line
    without = run["properties"]["findingsWithoutLine"]
    assert [(r["ruleId"], r["level"], r.get("locations")) for r in without] == [
        ("heuristic/breaking-change", "warning", [{"physicalLocation": {"artifactLocation": {"uri": "app/api.py", "uriBaseId": "%SRCROOT%"}}}]),
        ("model/maintainability", "note", None),  # the PR as a whole
    ]
    assert run["properties"]["leftOut"] == 0
    line = sarif.write(tmp_path / "out.sarif", every_severity())
    assert line == (f"SARIF: 4 results in {tmp_path / 'out.sarif'}. 2 findings without a line are in runs[0].properties.findingsWithoutLine "
                    "instead: code scanning shows a result only on a line.")


def test_the_results_are_the_5000_most_severe_and_the_rest_are_counted(tmp_path):
    """Code scanning keeps 5,000 results of a run, the most severe; the file says how many it left out."""
    lows = [Finding(title=f"Low {n}", detail="x", severity="low", path="a.py", line_start=n + 1, source="check", rule="code/x",
                    fingerprint=f"{n:012x}") for n in range(sarif.MAX_RESULTS + 2)]
    critical = Finding(title="Critical", detail="x", severity="critical", path="b.py", line_start=1, source="check", rule="code/y", fingerprint="9" * 12)
    log = valid(sarif.to_sarif(lows + [critical]))
    run = log["runs"][0]
    assert sarif.MAX_RESULTS == 5_000 and len(run["results"]) == 5_000
    assert run["results"][0]["ruleId"] == "code/y" and run["properties"]["leftOut"] == 3
    assert len(run["tool"]["driver"]["rules"]) == 2  # one for each rule id, far under code scanning's 25,000
    assert sarif.write(tmp_path / "out.sarif", lows + [critical]).endswith(
        "3 left out, the least severe, to stay within code scanning's limits (5,000 results, 10 MB).")


def test_the_file_stays_under_the_size_limit_with_the_most_severe_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(sarif, "MAX_BYTES", 40_000)
    detail = "word " * 400
    on_lines = [Finding(title=f"F{n}", detail=detail, severity=SEVERITIES[n % 4], path="a.py", line_start=n + 1, source="check", rule="code/x",
                        fingerprint=f"{n:012x}") for n in range(60)]
    whole_file = Finding(title="Whole file", detail="short", severity="critical", path="a.py", source="check", rule="code/y", fingerprint="9" * 12)
    path = tmp_path / "out.sarif"
    line = sarif.write(path, on_lines + [whole_file])
    assert 30_000 < path.stat().st_size <= 40_000
    run = json.loads(path.read_text(encoding="utf-8"))["runs"][0]
    kept = [r["properties"]["severity"] for r in run["results"]]
    assert 0 < len(kept) < 60 and run["properties"]["leftOut"] == 60 - len(kept) and f"{60 - len(kept)} left out" in line
    assert len(kept) < 15 and kept == ["critical"] * len(kept)  # of the 15 critical ones, as many as fit
    assert len(run["properties"]["findingsWithoutLine"]) == 1  # the results go first

    monkeypatch.setattr(sarif, "MAX_BYTES", 1_000)  # room for no finding at all: once the results are gone, the others go too
    assert "61 left out, the least severe" in sarif.write(path, on_lines + [whole_file])
    run = json.loads(path.read_text(encoding="utf-8"))["runs"][0]
    assert path.stat().st_size <= 1_000 and run["results"] == run["properties"]["findingsWithoutLine"] == [] and run["properties"]["leftOut"] == 61


def test_a_path_is_written_as_a_uri_reference():
    finding = Finding(title="T", detail="D", path="docs/my notes/ü%.md", line_start=1, source="check", rule="code/x")
    uri = sarif.to_sarif([finding])["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "docs/my%20notes/%C3%BC%25.md"


# ── the CLI ──────────────────────────────────────────────────────────────


def test_review_local_writes_sarif(tmp_path, capsys, monkeypatch):
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text('x = 1\nAPI_KEY = "sk-live-0123456789abcdef0123"\n')
    git("commit", "-q", "-am", "Add key")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    out = tmp_path / "out.sarif"
    assert cli.main(["review", "--local", "main", "--no-model", "--sarif", str(out)]) == 1  # a critical finding
    captured = capsys.readouterr()
    assert f"SARIF: 1 result in {out}." in captured.err and "SARIF" not in captured.out
    [result] = valid(json.loads(out.read_text(encoding="utf-8")))["runs"][0]["results"]
    assert (result["ruleId"], result["level"], result["locations"][0]["physicalLocation"]["region"]) == ("code/secret", "error", {"startLine": 2, "endLine": 2})


def test_review_of_a_pr_writes_sarif(tmp_path, capsys, monkeypatch):
    import stratum_open.github.api as api

    gh = FakeGitHub(diff=DIFF)
    monkeypatch.setattr(api, "GitHub", lambda token, repo, api=None: gh)
    monkeypatch.setattr(cli, "_token", lambda: "t")
    monkeypatch.setattr(cli, "model_from_policy", lambda policy: FakeModel(model_answer()))
    out = tmp_path / "out.sarif"
    assert cli.main(["review", "acme/widget#7", "--sarif", str(out)]) == 0
    assert "SARIF: " in capsys.readouterr().err
    run = valid(json.loads(out.read_text(encoding="utf-8")))["runs"][0]
    assert {r["ruleId"] for r in run["results"]} >= {"code/sql", "model/bug"}
    assert {r["properties"]["source"] for r in run["results"] if r["ruleId"] == "model/bug"} == {"model"}
    assert gh.checks == [] and gh.issue_comments_[7] == []  # a dry run, as without --sarif


# ── the Action ───────────────────────────────────────────────────────────


def pr_event(draft=False, head="a" * 40):  # FakeGitHub's PR #7 has the head "a" * 40
    return {"action": "opened", "repository": {"default_branch": "main"}, "pull_request": {"number": 7, "draft": draft, "head": {"sha": head}}}


def run_action(tmp_path, monkeypatch, gh, payload=None, **env):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload or pr_event()))
    monkeypatch.setattr(action, "GitHub", lambda token, repo, api: gh)
    monkeypatch.setattr(action, "model_from_policy", lambda policy, env: FakeModel(model_answer()))
    return action.main({"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "acme/widget",
                        "STRATUM_GITHUB_TOKEN": "t", "STRATUM_BOT_LOGIN": "stratum[bot]", **env})


def test_the_action_writes_sarif_once_the_review_is_posted(tmp_path, monkeypatch, capsys):
    gh = FakeGitHub(diff=DIFF)
    out = tmp_path / "results" / "stratum.sarif"  # a folder that isn't there yet
    assert run_action(tmp_path, monkeypatch, gh, STRATUM_SARIF=str(out)) == 0
    assert gh.issue_comments_[7] and "SARIF: " in capsys.readouterr().out
    run = valid(json.loads(out.read_text(encoding="utf-8")))["runs"][0]
    assert {r["ruleId"] for r in run["results"]} >= {"code/sql", "model/bug"}


@pytest.mark.parametrize("change", ["no input", "draft", "closed", "rerun"])
def test_the_action_writes_sarif_only_with_the_input_and_for_a_posted_review_of_the_events_commit(tmp_path, monkeypatch, capsys, change):
    gh = FakeGitHub(diff=DIFF)
    out = tmp_path / "stratum.sarif"
    # rerun: an older run started again after a push. The review reads the PR's head now, and the
    # upload step would name the event's commit, so the newer commit's lines would go on the older one.
    payload = pr_event(draft=change == "draft", head="b" * 40 if change == "rerun" else "a" * 40)
    if change == "closed":
        gh.pulls[7]["state"] = "closed"  # closed while the job waited: review_pull skips it
    env = {} if change == "no input" else {"STRATUM_SARIF": str(out)}
    assert run_action(tmp_path, monkeypatch, gh, payload, **env) == 0
    assert not out.exists()
    if change == "rerun":
        assert gh.issue_comments_[7] and "SARIF: none written. The review read aaaaaaa, a newer commit than the event's bbbbbbb" in capsys.readouterr().out
    # The same PR, open and ready, with the input.
    assert run_action(tmp_path, monkeypatch, FakeGitHub(diff=DIFF), STRATUM_SARIF=str(out)) == 0
    assert out.exists()


def test_a_sarif_file_that_cant_be_written_is_a_warning_and_leaves_the_review_alone(tmp_path, monkeypatch, capsys):
    gh = FakeGitHub(diff=DIFF)
    (tmp_path / "taken").write_text("a file, not a folder")
    assert run_action(tmp_path, monkeypatch, gh, STRATUM_SARIF=str(tmp_path / "taken" / "stratum.sarif")) == 0
    assert "::warning::Stratum couldn't write the SARIF file" in capsys.readouterr().out
    [check] = gh.checks
    assert check["title"] != "Stratum couldn't finish" and check["annotations"]


def test_the_action_passes_sarif_to_the_review_step():
    action_yml = workflow("action.yml")
    assert action_yml["inputs"]["sarif"]["default"] == ""  # off unless the workflow sets it
    steps = {step.get("id"): step for step in action_yml["runs"]["steps"]}
    assert steps["review"]["env"]["STRATUM_SARIF"] == "${{ inputs.sarif }}"
    assert "STRATUM_SARIF" not in steps["early"]["env"]  # the early post isn't the review




# ── the example workflow ─────────────────────────────────────────────────

EXAMPLE = ROOT / "examples" / "stratum.yml"


def with_sarif(example: str) -> str:
    """examples/stratum.yml as the release after 0.3.0 will have it: `security-events: write`, the
    `sarif` input and the upload step after Stratum's, uncommented together."""
    for off in ("  # security-events: write\n", "          # sarif: stratum.sarif\n"):
        assert example.count(off) == 1
        example = example.replace(off, off.replace("# ", "", 1))
    start = example.index("      # - name: Upload the findings to code scanning\n")
    end = example.index("\n\n", start)
    block = example[start:end].split("\n")
    assert all(line.startswith("      # ") for line in block)  # what an editor's "uncomment" undoes
    return example[:start] + "\n".join("      " + line[8:] for line in block) + example[end:]


def upload_step() -> dict:
    later = yaml.safe_load(with_sarif(EXAMPLE.read_text(encoding="utf-8")))
    return later["jobs"]["stratum"]["steps"][-1]


def test_the_example_keeps_the_sarif_upload_commented_until_its_pin_has_the_input():
    """v0.3.0 has no `sarif` input: it would write no file, and the upload step would never run."""
    shipped = workflow("examples/stratum.yml")
    steps = shipped["jobs"]["stratum"]["steps"]
    [review] = [step for step in steps if "ollama-model" in (step.get("with") or {})]
    assert review["uses"] == "stratum-org/stratum-open@v0.3.0" and "sarif" not in review["with"]
    assert "security-events" not in shipped["permissions"] and steps[-1] is review

    later = yaml.safe_load(with_sarif(EXAMPLE.read_text(encoding="utf-8")))
    steps = later["jobs"]["stratum"]["steps"]
    [review] = [step for step in steps if "ollama-model" in (step.get("with") or {})]
    path = review["with"]["sarif"]
    assert later["permissions"]["security-events"] == "write"
    [upload] = steps[steps.index(review) + 1:]
    # Only PR events write the file, a fork's PR included: on pull_request_target its token is the base repository's.
    assert upload["if"] == f"github.event_name == 'pull_request_target' && hashFiles('{path}') != ''"
    # The PR's ref and head: on pull_request_target, GITHUB_REF and GITHUB_SHA are the default branch's.
    assert upload["env"] == {"GH_TOKEN": "${{ github.token }}", "PR": "${{ github.event.pull_request.number }}",
                             "SHA": "${{ github.event.pull_request.head.sha }}"}
    assert f"gzip -c {path} " in upload["run"] and 'ref="refs/pull/$PR/head"' in upload["run"] and 'commit_sha="$SHA"' in upload["run"]
    assert "${{" not in upload["run"]  # what the event says reaches the script as variables, never as its text
    # The README shows the same steps.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme[readme.index("### Findings in code scanning (SARIF)"):]
    block = section[section.index("```yaml\n") + len("```yaml\n"):]
    shown = yaml.safe_load(block[:block.index("```\n")])
    assert shown[0]["with"] == {"ollama-model": "gpt-oss:20b", "sarif": path} and shown[1:] == [upload]
    # The release uncomments the queue job too; the two go together.
    from test_action import uncommented

    both = yaml.safe_load(with_sarif(uncommented(EXAMPLE.read_text(encoding="utf-8"))))
    assert list(both["jobs"]) == ["stratum", "queue"] and both["jobs"]["stratum"]["steps"][-1] == upload


GH = """#!/bin/bash
# A stand-in for the GitHub CLI: logs each call, keeps the file sent, and answers as the code scanning API does.
echo "$*" >> "$GH_LOG"
for arg in "$@"; do case "$arg" in sarif=@*) cp "${arg#sarif=@}" "$GH_LOG.sent" ;; esac; done
case "$*" in
  *"/code-scanning/sarifs -f"*) echo 47 ;;
  *"/code-scanning/sarifs/47 --jq .processing_status") echo "$GH_STATUS" ;;
  *"/code-scanning/sarifs/47") echo "{\\"processing_status\\":\\"$GH_STATUS\\",\\"errors\\":[\\"the reason\\"]}" ;;
  *) exit 1 ;;
esac
"""


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="the step runs in bash, on GitHub's Ubuntu runner")
@pytest.mark.parametrize("status, code", [("complete", 0), ("failed", 1)])
def test_the_upload_step_sends_the_file_for_the_prs_head_and_waits_for_code_scanning(tmp_path, status, code):
    upload = upload_step()
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "gh").write_text(GH, encoding="utf-8", newline="\n")
    (tmp_path / "bin" / "gh").chmod(0o755)
    (tmp_path / "work").mkdir()
    sarif.write(tmp_path / "work" / "stratum.sarif", every_severity())
    log = tmp_path / "gh.log"
    env = {"PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}", "GITHUB_REPOSITORY": "acme/widget", "RUNNER_TEMP": str(tmp_path),
           "GH_TOKEN": "t", "PR": "7", "SHA": "a" * 40, "GH_LOG": str(log), "GH_STATUS": status}
    # How GitHub runs a `run:` step with `shell: bash`.
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", upload["run"]], cwd=tmp_path / "work", env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == code, result.stderr
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[:2] == [
        f"api repos/acme/widget/code-scanning/sarifs -f commit_sha={'a' * 40} -f ref=refs/pull/7/head -f tool_name=Stratum Open "
        f"-F sarif=@{tmp_path}/stratum.sarif.b64 --jq .id",
        "api repos/acme/widget/code-scanning/sarifs/47 --jq .processing_status",
    ]
    # What the API takes: the file, gzipped and in base64.
    assert gzip.decompress(base64.b64decode((tmp_path / "gh.log.sent").read_text())) == (tmp_path / "work" / "stratum.sarif").read_bytes()
    assert f"Code scanning: {status}" in result.stdout
    if code:  # what code scanning said is in the job's log
        assert calls[2:] == ["api repos/acme/widget/code-scanning/sarifs/47"] and "the reason" in result.stdout
