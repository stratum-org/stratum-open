"""The CLI and the Action as real processes, against the fakes: exit codes,
one-line errors, no tracebacks, and output that survives a non-UTF-8 pipe."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from stratum_open.checks import bandit

from conftest import DIFF, SQL_LINE, head_of
from contract import assert_contract, assert_no_traceback, run_stratum
from fake_model import Response, finding, json_reply

HEAD = head_of(7)


def env_for(fake_github, fake_model, **extra) -> dict[str, str]:
    return {"GITHUB_TOKEN": fake_github.token, "STRATUM_GITHUB_API": fake_github.url, "STRATUM_MODEL_ENDPOINT": fake_model.url,
            "STRATUM_BOT_LOGIN": fake_github.bot, **extra}


def test_review_prints_non_ascii_model_text_through_a_cp1252_pipe(fake_github, fake_model):
    """Regression: `print` of the model's text raised UnicodeEncodeError on a Windows pipe."""
    fake_github.add_pull(7, diff=DIFF)
    fake_model.queue(Response.ok(json_reply([finding("app/users.py", SQL_LINE, line=5, title="Arrow → bug ✓", detail="détail 🎉 — see “quotes”")], summary="Résumé ✓ → 🎉")))
    result = run_stratum(["review", f"{fake_github.repo}#7"], env=env_for(fake_github, fake_model))
    assert result.returncode == 0, result.stderr
    assert_no_traceback(result.stderr)
    assert "Stratum review" in result.stdout and "Arrow" in result.stdout and "finding(s)." in result.stdout
    assert fake_github.check_runs == [] and fake_github.summary_comments(7) == []  # a dry run posts nothing


def test_review_post_publishes_and_exits_zero(fake_github, fake_model):
    fake_github.add_pull(7, diff=DIFF)
    result = run_stratum(["review", f"{fake_github.repo}#7", "--post"], env=env_for(fake_github, fake_model))
    assert result.returncode == 0 and "PR #7: published" in result.stdout, result.stderr
    assert_contract(fake_github, number=7, head=HEAD, stderr=result.stderr, stdout=result.stdout)
    again = run_stratum(["review", f"{fake_github.repo}#7", "--post"], env=env_for(fake_github, fake_model))
    assert again.returncode == 0 and len(fake_github.summary_comments(7)) == 1


@pytest.mark.parametrize("setup, code, says", [
    ("bad-token", 2, "GitHub answered 401: Bad credentials. The token was refused"),
    ("missing-pr", 2, "GitHub answered 404"),
    ("rate-limited", 2, "GitHub answered 403"),
    ("github-down", 2, "GitHub answered 503"),
    ("github-unreachable", 2, "Couldn't reach"),
    ("not-a-pr", 1, "Not a PR: nope"),
    ("no-token", 1, "Set GITHUB_TOKEN"),
])
def test_github_problems_are_one_line_and_a_documented_exit_code(fake_github, fake_model, setup, code, says):
    fake_github.add_pull(7, diff=DIFF)
    env = env_for(fake_github, fake_model)
    target = f"{fake_github.repo}#7"
    if setup == "bad-token":
        env["GITHUB_TOKEN"] = "ghp_" + "wrong" * 8
    elif setup == "missing-pr":
        target = f"{fake_github.repo}#999"
    elif setup == "rate-limited":
        fake_github.rate_limit(retry_after="0", times=0)
    elif setup == "github-down":
        fake_github.fail(".*", ".*", 503, times=0)
    elif setup == "github-unreachable":
        env["STRATUM_GITHUB_API"] = "http://127.0.0.1:9/"
    elif setup == "not-a-pr":
        target = "nope"
    elif setup == "no-token":
        env.pop("GITHUB_TOKEN")
        env["PATH"] = ""  # so `gh auth token` can't be found either
    result = run_stratum(["review", target], env=env, timeout=240)
    assert result.returncode == code, (result.stdout, result.stderr)
    assert says in result.stderr, result.stderr
    assert_no_traceback(result.stderr, result.stdout)
    assert result.stderr.count("\n") <= 3


def test_dead_model_from_the_cli_still_reviews(fake_github, fake_model):
    fake_github.add_pull(7, diff=DIFF)
    env = env_for(fake_github, fake_model, STRATUM_MODEL_ENDPOINT="http://127.0.0.1:9/v1")
    result = run_stratum(["review", f"{fake_github.repo}#7", "--post"], env=env, timeout=240)
    assert result.returncode == 0, result.stderr
    assert "The model didn't answer, so this review has only the checks" in fake_github.summary_comments(7)[0]["body"]
    assert_contract(fake_github, number=7, head=HEAD, stderr=result.stderr)


def test_triage_command(fake_github, fake_model):
    fake_github.add_pull(7, diff=DIFF)
    result = run_stratum(["triage", fake_github.repo], env=env_for(fake_github, fake_model))
    assert result.returncode == 0 and "Not reviewed yet (1)" in result.stdout, result.stderr


def closed_prs(fake_github):
    """#7 merged with CI that failed, #8 closed unmerged with CI that passed, #9 still open."""
    def run(started, completed, conclusion):
        return {"name": "test", "check_suite": {"id": 1}, "status": "completed", "conclusion": conclusion,
                "started_at": f"2026-01-01T{started}:00Z", "completed_at": f"2026-01-01T{completed}:00Z"}

    fake_github.add_pull(7, diff=DIFF, state="closed", merged=True, body="Fixes #3: the user query, and its test.")
    fake_github.add_pull(8, diff=DIFF, state="closed", title="Résumé → ✓")
    fake_github.add_pull(9, diff=DIFF)
    fake_github.statuses[HEAD] = {"state": "failure", "statuses": [{"context": "docs", "state": "failure", "created_at": "2026-01-01T12:00:00Z"}]}
    # A workflow still waiting for approval doesn't hide the failure.
    fake_github.workflow_runs[HEAD] = [{"id": 1, "workflow_id": 1, "name": "tests", "event": "pull_request", "status": "completed",
                                        "conclusion": "action_required", "created_at": "2026-01-01T11:00:00Z"}]
    # Passed while the PR was open; run again after it closed (2026-01-02), when it failed. The Action's own job isn't CI.
    fake_github.ci_runs[head_of(8)] = [run("12:00", "12:05", "success"), {**run("12:00", "12:05", "failure"), "started_at": "2026-01-05T12:00:00Z"},
                                       {**run("12:00", "12:05", "cancelled"), "name": "stratum", "check_suite": {"id": 2}}]


def test_replay_reads_closed_prs_and_writes_nothing(fake_github, fake_model, tmp_path):
    closed_prs(fake_github)
    report = tmp_path / "replay.md"
    result = run_stratum(["replay", fake_github.repo, "--json", "--out", str(report)], env=env_for(fake_github, fake_model))
    assert result.returncode == 0, result.stderr
    assert_no_traceback(result.stderr)
    data = json.loads(result.stdout)
    assert [(p["number"], p["outcome"], p["ci"], p["linked_issues"]) for p in data["pulls"]] == [
        (8, "closed unmerged", "passed", []), (7, "merged", "failed", [3]),
    ]
    assert data["pulls"][1]["findings"] == {"code/sql": 1} and "CI is failing." in data["pulls"][1]["reasons"]
    assert "| #7 Add a thing | merged | triage: needs work |" in report.read_text(encoding="utf-8")
    assert {r["method"] for r in fake_github.requests} == {"GET"}
    assert fake_github.posted_text() == "" and not fake_github.labels and not fake_github.closed
    # The repo, its policy and the list; then a PR's diff, its three CI reads and, for Bandit, its Python file.
    # #7's CI had failed, so its workflow runs aren't read.
    assert data["requests"] == len(fake_github.requests) == 3 + 2 * (4 + bandit.available()) - 1
    assert fake_github.token not in result.stdout + result.stderr


def test_replay_stops_when_the_rate_limit_runs_low_and_keeps_its_rows(fake_github, fake_model):
    closed_prs(fake_github)

    def spend(request):  # someone else on the same token uses up the hour while #8 is being read
        if request["path"].endswith("/pulls/8"):
            fake_github.remaining = 40

    fake_github.on_request = spend
    result = run_stratum(["replay", fake_github.repo], env=env_for(fake_github, fake_model))
    assert result.returncode == 2, result.stderr
    assert "Stopped after 1 of 50 PRs: GitHub's rate limit is nearly used up (40 requests left)." in result.stderr
    assert "| #8 R" in result.stdout and "| #7 " not in result.stdout
    assert fake_github.requests_to("GET", r"/pulls/7$") == []
    assert_no_traceback(result.stderr)


def test_local_review_in_a_repo_with_a_planted_bug_and_emoji(fake_model, tmp_path):
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
    (repo / "app.py").write_text('x = 1\ndef q(c, uid):\n    return c.execute(f"SELECT * FROM t WHERE id = {uid}")  # 🎉 ✓\n', encoding="utf-8")
    git("commit", "-q", "-am", "Add query ✓")
    fake_model.queue(Response.ok(json_reply([finding("app.py", 'return c.execute(f"SELECT * FROM t WHERE id = {uid}")  # 🎉 ✓', line=3, title="Injection ✓")], summary="Adds a query → risky")))
    result = run_stratum(["review", "--local", "main", "--json", str(tmp_path / "out.json")], env={"STRATUM_MODEL_ENDPOINT": fake_model.url}, cwd=str(repo))
    assert result.returncode == 0, result.stderr
    assert_no_traceback(result.stderr)
    assert "What this changes:" in result.stdout and "code/sql" in result.stdout and "Injection" in result.stdout
    assert {f["rule"] for f in json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))} >= {"code/sql", "model:injection"}


# ── the Action ───────────────────────────────────────────────────────────


def action_env(fake_github, fake_model, tmp_path, payload, event="pull_request_target"):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {"GITHUB_EVENT_NAME": event, "GITHUB_EVENT_PATH": str(path), "GITHUB_REPOSITORY": fake_github.repo, "STRATUM_GITHUB_TOKEN": fake_github.token,
            "GITHUB_API_URL": fake_github.url, "STRATUM_MODEL_ENDPOINT": fake_model.url, "STRATUM_BOT_LOGIN": fake_github.bot, "STRATUM_JOB_NAME": "stratum"}


def pr_payload(action="opened", draft=False, head=HEAD):
    return {"action": action, "repository": {"default_branch": "main", "full_name": "acme/widget"}, "pull_request": {"number": 7, "draft": draft, "head": {"sha": head}}}


def test_action_reviews_a_pr_event(fake_github, fake_model, tmp_path):
    fake_github.add_pull(7, diff=DIFF)
    result = run_stratum(["action"], env=action_env(fake_github, fake_model, tmp_path, pr_payload()))
    assert result.returncode == 0 and "PR #7: published" in result.stdout, result.stderr
    assert_contract(fake_github, number=7, head=HEAD, stderr=result.stderr, stdout=result.stdout)


def test_action_skips_drafts_and_other_actions(fake_github, fake_model, tmp_path):
    fake_github.add_pull(7, diff=DIFF, draft=True)
    result = run_stratum(["action"], env=action_env(fake_github, fake_model, tmp_path, pr_payload(draft=True)))
    assert result.returncode == 0 and "Draft PR" in result.stdout
    result = run_stratum(["action"], env=action_env(fake_github, fake_model, tmp_path, pr_payload(action="labeled")))
    assert result.returncode == 0 and "Nothing to do for a labeled event." in result.stdout
    assert fake_github.check_runs == []


def test_action_with_github_failing_posts_a_neutral_check_and_one_error_line(fake_github, fake_model, tmp_path):
    fake_github.add_pull(7, diff=DIFF)
    fake_github.fail("GET", r"/pulls/7$", 500, times=0)
    result = run_stratum(["action"], env=action_env(fake_github, fake_model, tmp_path, pr_payload()), timeout=240)
    assert result.returncode == 1
    assert result.stdout.count("::error::") == 1 and "Stratum couldn't finish: GitHub answered 500" in result.stdout
    assert_no_traceback(result.stderr, result.stdout)
    [run] = fake_github.check_runs_for(HEAD)
    assert run["status"] == "completed" and run["conclusion"] == "neutral" and run["output"]["title"] == "Stratum couldn't finish"


def test_action_with_a_bug_keeps_the_traceback_in_the_log(fake_github, fake_model, tmp_path, monkeypatch):
    fake_github.add_pull(7, diff=DIFF)
    payload = {**pr_payload(), "repository": ["not", "an", "object"]}  # a shape nothing guards against: a bug-shaped failure
    env = action_env(fake_github, fake_model, tmp_path, payload)
    result = run_stratum(["action"], env=env)
    assert result.returncode == 1
    assert "::error::Stratum hit a bug and couldn't finish" in result.stdout
    assert "Traceback" in result.stderr and "Traceback" not in result.stdout
    assert fake_github.token not in result.stderr + result.stdout


@pytest.mark.parametrize("payload", [{}, {"action": "opened", "pull_request": None}, {"action": "opened", "pull_request": {"draft": False}}, [1, 2]])
def test_action_with_malformed_event_payloads(fake_github, fake_model, tmp_path, payload):
    result = run_stratum(["action"], env=action_env(fake_github, fake_model, tmp_path, payload))
    assert result.returncode in (0, 1)
    assert_no_traceback(result.stderr, result.stdout)


def test_action_missing_event_file_or_token(fake_github, fake_model, tmp_path):
    env = action_env(fake_github, fake_model, tmp_path, pr_payload())
    env["GITHUB_EVENT_PATH"] = str(tmp_path / "missing.json")
    result = run_stratum(["action"], env=env)
    assert result.returncode == 1 and "couldn't read the event payload" in result.stdout
    env = action_env(fake_github, fake_model, tmp_path, pr_payload())
    env.pop("STRATUM_GITHUB_TOKEN")
    result = run_stratum(["action"], env=env)
    assert result.returncode == 1 and "needs a GitHub token" in result.stdout
    assert_no_traceback(result.stderr)


def test_action_comment_and_schedule_events(fake_github, fake_model, tmp_path):
    fake_github.add_pull(7, diff=DIFF)
    run_stratum(["action"], env=action_env(fake_github, fake_model, tmp_path, pr_payload()))
    comment = {"action": "created", "repository": {"default_branch": "main"}, "issue": {"number": 7, "pull_request": {}},
               "comment": {"id": 1, "body": "/stratum triage", "user": {"login": "erin", "type": "User"}}}
    result = run_stratum(["action"], env=action_env(fake_github, fake_model, tmp_path, comment, event="issue_comment"))
    assert result.returncode == 0 and "Command: triage" in result.stdout
    result = run_stratum(["action"], env=action_env(fake_github, fake_model, tmp_path, {"repository": {"default_branch": "main"}}, event="schedule"))
    assert result.returncode == 0 and "Triage queue updated." in result.stdout
    assert [i["title"] for i in fake_github.issues] == ["Stratum triage queue"]
