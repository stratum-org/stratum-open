"""`--sarif` and the Action's `sarif` input as real processes, against the
fakes: the file validates against the official SARIF 2.1.0 schema, and
holds what the review found."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import jsonschema

from conftest import DIFF, SQL_LINE, head_of
from contract import assert_contract, assert_no_traceback, run_stratum
from fake_model import Response, finding, json_reply

SCHEMA = json.loads((Path(__file__).parents[1] / "sarif" / "sarif-schema-2.1.0.json").read_text(encoding="utf-8"))


def read(path: Path) -> dict:
    log = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.Draft4Validator(SCHEMA, format_checker=jsonschema.FormatChecker()).validate(log)
    return log["runs"][0]


def env_for(fake_github, fake_model) -> dict[str, str]:
    return {"GITHUB_TOKEN": fake_github.token, "STRATUM_GITHUB_API": fake_github.url, "STRATUM_MODEL_ENDPOINT": fake_model.url,
            "STRATUM_BOT_LOGIN": fake_github.bot}


def test_review_of_a_pr_writes_sarif(fake_github, fake_model, tmp_path):
    fake_github.add_pull(7, diff=DIFF)
    fake_model.queue(Response.ok(json_reply([finding("app/users.py", SQL_LINE, line=5, severity="medium")])))
    out = tmp_path / "out.sarif"
    result = run_stratum(["review", f"{fake_github.repo}#7", "--sarif", str(out)], env=env_for(fake_github, fake_model))
    assert result.returncode == 0, result.stderr
    assert_no_traceback(result.stderr)
    assert f"SARIF: 2 results in {out}." in result.stderr and "SARIF" not in result.stdout
    run = read(out)
    assert [(r["ruleId"], r["level"], r["properties"]["source"]) for r in run["results"]] == [("code/sql", "error", "check"), ("model/bug", "warning", "model")]
    assert fake_github.check_runs == [] and fake_github.summary_comments(7) == []  # a dry run posts nothing, and uploads nothing


def test_local_review_writes_sarif(fake_model, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x.y",
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@x.y"}
    (tmp_path / "gitconfig").write_text("")

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    git("checkout", "-q", "-b", "feature")
    line = 'return c.execute(f"SELECT * FROM t WHERE id = {uid}")'
    (repo / "app.py").write_text(f"x = 1\ndef q(c, uid):\n    {line}\n", encoding="utf-8")
    git("commit", "-q", "-am", "Add a query")
    fake_model.queue(Response.ok(json_reply([finding("app.py", line, line=3, title="Injection", category="bug", severity="critical")])))
    out = tmp_path / "out.sarif"
    result = run_stratum(["review", "--local", "main", "--sarif", str(out)], env={"STRATUM_MODEL_ENDPOINT": fake_model.url}, cwd=str(repo))
    assert result.returncode == 1, result.stderr  # a critical finding
    assert_no_traceback(result.stderr)
    run = read(out)
    assert {(r["ruleId"], r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]) for r in run["results"]} == {
        ("code/sql", "app.py"), ("model/bug", "app.py")}


def test_action_writes_sarif_on_a_pr_event(fake_github, fake_model, tmp_path):
    fake_github.add_pull(7, diff=DIFF)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "opened", "repository": {"default_branch": "main", "full_name": "acme/widget"},
                                 "pull_request": {"number": 7, "draft": False, "head": {"sha": head_of(7)}}}), encoding="utf-8")
    out = tmp_path / "stratum.sarif"
    env = {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": fake_github.repo,
           "STRATUM_GITHUB_TOKEN": fake_github.token, "GITHUB_API_URL": fake_github.url, "STRATUM_MODEL_ENDPOINT": fake_model.url,
           "STRATUM_BOT_LOGIN": fake_github.bot, "STRATUM_JOB_NAME": "stratum", "STRATUM_SARIF": str(out)}
    result = run_stratum(["action"], env=env)
    assert result.returncode == 0 and "PR #7: published" in result.stdout and f"SARIF: 1 result in {out}." in result.stdout, result.stderr
    assert_contract(fake_github, number=7, head=head_of(7), stderr=result.stderr, stdout=result.stdout)
    [sql] = read(out)["results"]
    # The fingerprint the inline comment carries, which `/stratum ignore <fingerprint>` takes.
    [comment] = fake_github.inline_comments(7)
    assert sql["ruleId"] == "code/sql" and sql["partialFingerprints"]["primaryLocationLineHash"] in comment["body"]
