"""L. GitLab: a merge request reviewed from a GitLab CI job, against a fake
GitLab API served under a self-hosted path prefix. Positions, the summary
note updated in place, suggestions, thread resolution, the commit status,
labels, pagination and errors.

Everything here is checked against `fake_gitlab.py`, which follows GitLab's
REST docs; none of it has run against a live GitLab."""

from __future__ import annotations

import base64
import json

import pytest

from stratum_open.github import publish
from stratum_open.gitlab import ci
from stratum_open.gitlab.api import GitLab, GitLabError

from conftest import DIFF, SQL_LINE
from fake_gitlab import FakeGitLabServer
from fake_model import Response, finding, json_reply

HEAD = "a" * 40
FIXED = """diff --git a/app/users.py b/app/users.py
--- a/app/users.py
+++ b/app/users.py
@@ -1,4 +1,6 @@
 import sqlite3
+import requests

 def get_user(conn, user_id):
-    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
+    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
+    return cur.fetchone()
"""


@pytest.fixture
def gl():
    with FakeGitLabServer() as server:
        yield server


def env_for(gl: FakeGitLabServer, fake_model, **extra: str) -> dict[str, str]:
    return {
        "CI_MERGE_REQUEST_IID": "7", "CI_MERGE_REQUEST_PROJECT_ID": str(gl.project_id), "CI_PROJECT_ID": str(gl.project_id),
        "CI_MERGE_REQUEST_SOURCE_PROJECT_ID": str(gl.project_id), "CI_MERGE_REQUEST_PROJECT_PATH": gl.path,
        "CI_API_V4_URL": gl.url, "CI_PIPELINE_ID": "77", "CI_JOB_NAME": "stratum", "CI_JOB_URL": f"{gl.origin}/{gl.path}/-/jobs/5",
        "STRATUM_GITLAB_TOKEN": gl.token, "STRATUM_MODEL_ENDPOINT": fake_model.url, "STRATUM_MODEL_NAME": "fake-7b", **extra,
    }


def sql_finding(**extra):
    fields = {"title": "SQL injection", "detail": "The id goes into the query as text.", "severity": "critical", "category": "security", **extra}
    return finding("app/users.py", SQL_LINE, line=5, **fields)


def client(gl: FakeGitLabServer) -> GitLab:
    return GitLab(gl.token, gl.project_id, api=gl.url)


# ── a review, start to finish ────────────────────────────────────────────


def test_an_mr_is_reviewed_and_published_on_a_self_hosted_gitlab(gl, fake_model, capsys):
    gl.add_mr(7, diff=DIFF)
    fake_model.queue(Response.ok(json_reply([sql_finding(replacement='    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))')])))
    assert ci.main(env_for(gl, fake_model)) == 0, capsys.readouterr().out
    assert "MR !7: published" in capsys.readouterr().out
    assert gl.errors == []
    assert all(r["path"].startswith("/gitlab/api/v4/") for r in gl.requests)  # every call under the instance's own prefix
    assert all(r["token"] == gl.token for r in gl.requests)

    [summary] = gl.summary_notes(7)
    assert "### Stratum review" in summary["body"] and "on the changed lines below" in summary["body"]
    state = publish.State.decode(summary["body"])
    assert state.head == HEAD

    [thread] = gl.threads(7)
    note = thread["notes"][0]
    assert note["position"] == {"position_type": "text", "base_sha": "b" * 40, "start_sha": "c" * 40, "head_sha": HEAD,
                                "old_path": "app/users.py", "new_path": "app/users.py", "new_line": 5}
    assert "```suggestion:-0+0\n" in note["body"] and "```suggestion\n" not in note["body"]
    # GitLab CI can't read commands, so no hint names one ("/stratum " with the space: the footer's link to stratum-org/stratum-open isn't a command).
    assert "/stratum " not in gl.posted_text() and "stratum-org/stratum-open" in gl.posted_text()
    assert "Resolve this thread" in note["body"]

    [status] = gl.posted_statuses(HEAD)
    assert status["name"] == "Stratum" and status["status"] == "failed"  # a critical finding, as the check run's failure
    assert status["pipeline_id"] == 77 and status["target_url"].endswith("/jobs/5")


def test_the_summary_note_is_edited_in_place_on_the_next_pipeline(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    gl.add_note(7, "Looks fine to me", author="dave")
    gl.add_note(7, "added 1 commit", author="carol", system=True)
    env = env_for(gl, fake_model)
    assert ci.main(env) == 0
    first = gl.summary_notes(7)[0]["id"]
    assert ci.main(env) == 0
    assert [n["id"] for n in gl.summary_notes(7)] == [first]
    assert len(gl.requests_to("POST", r"/notes$")) == 1 and gl.requests_to("PUT", rf"/notes/{first}$")


def test_a_thread_is_closed_and_resolved_when_its_code_changes(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    fake_model.queue(Response.ok(json_reply([sql_finding()])))
    env = env_for(gl, fake_model)
    assert ci.main(env) == 0
    [thread] = gl.threads(7)
    assert thread["notes"][0]["resolved"] is False

    gl.move_head(7, "e" * 40, diff=FIXED)
    assert ci.main(env) == 0
    [thread] = gl.threads(7)
    note = thread["notes"][0]
    assert note["resolved"] is True
    assert note["body"].startswith("~~") and publish.RESOLVED_MARKER in note["body"]
    assert gl.requests_to("PUT", rf"/discussions/{thread['id']}$")
    # Only the file that changed since the last head was read again, as on GitHub.
    assert publish.State.decode(gl.summary_notes(7)[0]["body"]).head == "e" * 40


def test_a_force_push_reviews_everything_again(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    assert ci.main(env_for(gl, fake_model)) == 0
    gl.move_head(7, "e" * 40, diff=FIXED)
    gl.parents.clear()  # the old head isn't an ancestor of the new one
    assert client(gl).compare_files(HEAD, "e" * 40) is None
    assert ci.main(env_for(gl, fake_model)) == 0
    assert "Reviewed only" not in gl.summary_notes(7)[0]["body"]


def test_a_person_resolving_a_thread_keeps_the_finding_from_coming_back(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    fake_model.default = Response.ok(json_reply([sql_finding()]))
    env = env_for(gl, fake_model)
    assert ci.main(env) == 0
    gl.threads(7)[0]["notes"][0]["resolved"] = True
    assert ci.main(env, post=True) == 0
    assert len(gl.threads(7)) == 1


def test_triage_labels_and_status_off(gl, fake_model):
    gl.add_mr(7, diff=DIFF, labels=["backend"])
    gl.set_policy("triage:\n  enabled: true\n")
    gl.closes[7] = [3]
    assert ci.main(env_for(gl, fake_model, STRATUM_GITLAB_STATUS="off")) == 0
    mr = gl.mrs[7]
    added = [label for label in mr["labels"] if label != "backend"]
    assert len(added) == 1 and added[0] in gl.labels
    assert gl.labels[added[0]]["color"].startswith("#")
    assert gl.posted == []
    assert gl.requests_to("GET", r"/merge_requests/7/closes_issues$")


def test_the_policy_comes_from_the_default_branch(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    gl.set_policy("review:\n  check_name: Stratum review\n")
    gl.set_policy("review:\n  check_name: From the MR\n", ref="feature")
    assert ci.main(env_for(gl, fake_model)) == 0
    assert [s["name"] for s in gl.posted_statuses(HEAD)] == ["Stratum review"]
    assert all(r["query"].get("ref") != ["feature"] for r in gl.requests_to("GET", r"policy\.yml$"))


def test_learning_from_history_is_off_on_gitlab_and_says_so(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    gl.set_policy("review:\n  learn_from_history: true\n")
    fake_model.queue(Response.ok(json_reply([sql_finding()])))
    assert ci.main(env_for(gl, fake_model)) == 0
    assert "review.learn_from_history isn't read on GitLab yet" in gl.summary_notes(7)[0]["body"]
    assert "👍" not in gl.threads(7)[0]["notes"][0]["body"]


def test_a_draft_is_skipped_per_the_policy(gl, fake_model, capsys):
    gl.add_mr(7, diff=DIFF, draft=True)
    assert ci.main(env_for(gl, fake_model)) == 0
    assert "Draft" in capsys.readouterr().out
    assert gl.summary_notes(7) == [] and gl.posted == []


def test_dry_run_posts_nothing(gl, fake_model, capsys):
    gl.add_mr(7, diff=DIFF)
    assert ci.main(env_for(gl, fake_model), post=False) == 0
    assert "### Stratum review" in capsys.readouterr().out
    assert [r for r in gl.requests if r["method"] != "GET"] == []


# ── positions and suggestions ────────────────────────────────────────────


def _comment(line: int, start: int | None = None, body: str = "**High · bug:** T\n\nD") -> dict:
    comment = {"path": "app/users.py", "line": line, "side": "RIGHT", "body": body}
    if start is not None:
        comment.update(start_line=start, start_side="RIGHT")
    return comment


def test_an_unchanged_line_takes_both_numbers_and_an_added_one_only_its_new_one(gl):
    gl.add_mr(7, diff=DIFF)
    api = client(gl)
    api.pull(7)
    api.create_review(7, HEAD, [_comment(1)])  # "import sqlite3", unchanged
    api.create_review(7, HEAD, [_comment(2)])  # "+import requests"
    positions = [t["notes"][0]["position"] for t in gl.threads(7)]
    assert (positions[0]["old_line"], positions[0]["new_line"]) == (1, 1)
    assert "old_line" not in positions[1] and positions[1]["new_line"] == 2


def test_a_line_outside_the_diff_is_a_422_and_a_batch_goes_one_at_a_time(gl):
    gl.add_mr(7, diff=DIFF)
    api = client(gl)
    api.pull(7)
    with pytest.raises(GitLabError) as exc:
        api.create_review(7, HEAD, [_comment(40)])
    assert exc.value.status == 422
    with pytest.raises(GitLabError) as exc:
        api.create_review(7, HEAD, [_comment(2), _comment(5)])
    assert exc.value.status == 422 and gl.requests_to("POST", r"/discussions$") == []  # refused before anything was sent


def test_gitlab_refusing_a_position_is_a_422_too(gl):
    gl.add_mr(7, diff=DIFF)
    gl.fail("POST", r"/discussions$", 400, body={"message": "400 Bad request - Note {:line_code=>[\"can't be blank\"]}"})
    api = client(gl)
    api.pull(7)
    with pytest.raises(GitLabError) as exc:
        api.create_review(7, HEAD, [_comment(5)])
    assert exc.value.status == 422


def test_a_multi_line_suggestion_counts_the_lines_above(gl):
    gl.add_mr(7, diff=DIFF)
    api = client(gl)
    api.pull(7)
    body = "**High · bug:** T\n\nD\n\n```suggestion\n    a\n    b\n    c\n```\n\n<sub>hint</sub>"
    api.create_review(7, HEAD, [_comment(7, start=5, body=body)])
    note = gl.threads(7)[0]["notes"][0]
    assert note["position"]["new_line"] == 7 and "```suggestion:-2+0\n    a\n    b\n    c\n```" in note["body"]


def test_the_inline_comments_go_one_per_request_through_the_runner(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    fake_model.queue(Response.ok(json_reply([sql_finding(), finding("app/users.py", 'print("unreachable")', line=7, title="Dead code",
                                                                    detail="Nothing after the return runs.", severity="medium")])))
    assert ci.main(env_for(gl, fake_model, STRATUM_MODEL_NAME="fake-7b")) == 0
    lines = sorted(t["notes"][0]["position"]["new_line"] for t in gl.threads(7))
    assert 5 in lines and 7 in lines
    assert all(len(r["body"]["position"]) >= 7 for r in gl.requests_to("POST", r"/discussions$"))


# ── pagination ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("links", [True, False], ids=["link-header", "x-next-page-only"])
def test_every_page_is_read(gl, fake_model, links):
    gl.links = links
    gl.per_page_cap = 20
    gl.add_mr(7, diff=DIFF)
    env = env_for(gl, fake_model)
    assert ci.main(env) == 0
    for n in range(70):
        gl.add_note(7, f"comment {n}", author="dave")
    assert ci.main(env) == 0
    assert len(gl.summary_notes(7)) == 1  # the summary, on the first page, was found past 70 later notes
    assert len(gl.requests_to("GET", r"/merge_requests/7/notes$")) >= 4 * 2


def test_many_files_across_diff_pages(gl, fake_model):
    gl.per_page_cap = 5
    diff = "".join(f"diff --git a/f{n}.py b/f{n}.py\nnew file mode 100644\n--- /dev/null\n+++ b/f{n}.py\n@@ -0,0 +1 @@\n+x = {n}\n" for n in range(12))
    gl.add_mr(7, diff=diff)
    api = client(gl)
    pull = api.pull(7)
    assert pull["additions"] == 12 and len(gl.requests_to("GET", r"/merge_requests/7/diffs$")) == 3
    assert api.pull_diff(7).count("diff --git ") == 12


def test_an_old_gitlab_without_diffs_falls_back_to_changes(gl):
    gl.has_diffs_endpoint = False
    gl.add_mr(7, diff=DIFF)
    api = client(gl)
    assert api.pull(7)["additions"] == 4
    assert "+import requests" in api.pull_diff(7) and gl.requests_to("GET", r"/changes$")


# ── errors ───────────────────────────────────────────────────────────────


def test_a_refused_token_is_one_line_and_exit_1(gl, fake_model, capsys):
    gl.add_mr(7, diff=DIFF)
    assert ci.main(env_for(gl, fake_model, STRATUM_GITLAB_TOKEN="glpat-wrong")) == 1
    out = capsys.readouterr().out
    assert "GitLab answered 401" in out and "Traceback" not in out


def test_a_5xx_on_a_read_is_tried_again(gl, fake_model, monkeypatch):
    monkeypatch.setattr("stratum_open.gitlab.api.time.sleep", lambda s: None)
    gl.add_mr(7, diff=DIFF)
    gl.fail("GET", r"/merge_requests/7$", 502, times=1)
    gl.fail("GET", r"/notes$", 429, times=1)
    assert ci.main(env_for(gl, fake_model)) == 0
    assert len(gl.summary_notes(7)) == 1


def test_threads_failing_still_leave_the_summary_and_status(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    fake_model.queue(Response.ok(json_reply([sql_finding()])))
    gl.fail("GET", r"/discussions$", 500, times=3)
    assert ci.main(env_for(gl, fake_model)) == 0
    body = gl.summary_notes(7)[0]["body"]
    assert "Couldn't post inline comments (GitLab answered 500)" in body and "SQL injection" in body
    assert gl.posted_statuses(HEAD)


def test_a_status_gitlab_refuses_doesnt_fail_the_job(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    gl.fail("POST", r"/statuses/", 400, body={"message": "Cannot transition status via :run from :success"})
    assert ci.main(env_for(gl, fake_model)) == 0
    assert len(gl.summary_notes(7)) == 1


def test_a_fork_pipeline_without_the_token_posts_nothing_and_says_why(gl, fake_model, capsys):
    gl.add_mr(7, diff=DIFF)
    env = env_for(gl, fake_model, CI_PROJECT_ID="1234", CI_MERGE_REQUEST_SOURCE_PROJECT_ID="1234")
    del env["STRATUM_GITLAB_TOKEN"]
    assert ci.main(env) == 0
    assert "fork" in capsys.readouterr().out and gl.requests == []


def test_a_missing_token_in_the_project_itself_fails_the_job(gl, fake_model, capsys):
    env = env_for(gl, fake_model)
    del env["STRATUM_GITLAB_TOKEN"]
    assert ci.main(env) == 1
    assert "STRATUM_GITLAB_TOKEN" in capsys.readouterr().out


def test_not_an_mr_pipeline_is_nothing_to_do(gl, fake_model, capsys):
    env = env_for(gl, fake_model)
    del env["CI_MERGE_REQUEST_IID"]
    assert ci.main(env) == 0 and gl.requests == []


def test_a_closed_mr_is_skipped(gl, fake_model):
    gl.add_mr(7, diff=DIFF, state="merged")
    assert ci.main(env_for(gl, fake_model)) == 0
    assert gl.summary_notes(7) == [] and not gl.requests_to("GET", r"/diffs$")


# ── what GitLab is sent ──────────────────────────────────────────────────


def test_a_model_line_that_reads_as_a_quick_action_is_escaped(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    fake_model.queue(Response.ok(json_reply([sql_finding(detail="Fix it, then:\n/approve\n/merge")], summary="Adds a query.")))
    assert ci.main(env_for(gl, fake_model)) == 0
    text = gl.posted_text()
    assert "\\n/approve" not in text and "\\n/merge" not in text
    assert "\\\\/approve" in text  # \/approve: shown as /approve, not run


def test_ci_state_ignores_stratums_own_job_and_allowed_failures(gl):
    gl.add_mr(7, diff=DIFF)
    api = client(gl)
    api.pull(7)
    ref = "refs/merge-requests/7/head"
    gl.statuses[HEAD] = [{"name": "stratum", "status": "running", "ref": ref}, {"name": "lint", "status": "failed", "allow_failure": True, "ref": ref},
                         {"name": "tests", "status": "success", "ref": ref}]
    assert api.ci_state(HEAD, ignore={"stratum", "Stratum"}) is True
    gl.statuses[HEAD].append({"name": "build", "status": "failed", "ref": ref})
    assert api.ci_state(HEAD, ignore={"stratum"}) is False
    gl.statuses[HEAD][-1]["status"] = "pending"
    assert api.ci_state(HEAD, ignore={"stratum"}) is None
    assert all(r["query"]["ref"] == [ref] for r in gl.requests_to("GET", r"/statuses$"))


def test_file_text_and_size_limit(gl):
    gl.files[("src/a b.py", HEAD)] = "x = 'é'\n"
    api = client(gl)
    assert api.file_text("src/a b.py", HEAD) == "x = 'é'\n"
    assert api.file_text("src/a b.py", HEAD, max_bytes=3) is None
    assert api.file_text("missing.py", HEAD) is None
    assert gl.requests_to("GET", r"/repository/files/src%2Fa%20b\.py$")


def test_commit_emails_come_through_the_users_name(gl):
    gl.users["carol"] = {"id": 7, "username": "carol", "name": "Carol C", "public_email": "carol@example.com"}
    gl.commits_by_author["Carol C"] = [{"author_email": "carol@work.example"}]
    assert client(gl).commit_emails("carol") == {"carol@example.com", "carol@work.example"}
    assert client(gl).commit_emails("nobody") == set()


def test_project_paths_are_encoded(gl):
    api = GitLab(gl.token, gl.path, api=gl.url + "/")
    assert api.repository()["default_branch"] == "main"
    assert gl.requests[-1]["path"].endswith("/projects/acme%2Fwidget")


def test_the_token_is_never_posted(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    assert ci.main(env_for(gl, fake_model)) == 0
    assert gl.token not in gl.posted_text()
    assert base64.b64encode(gl.token.encode()).decode() not in json.dumps(gl.notes)


# ── as a process, the way the CI job runs it ─────────────────────────────


def test_the_gitlab_command_as_a_process_with_a_checkout_of_the_default_branch(gl, fake_model, tmp_path):
    import subprocess

    from contract import assert_no_traceback, run_stratum

    repo = tmp_path / "default-branch"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["-c", "user.name=A", "-c", "user.email=a@example.com", "commit", "-q", "--allow-empty", "-m", "Start"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    gl.add_mr(7, diff=DIFF)
    env = env_for(gl, fake_model, STRATUM_CHECKOUT=str(repo))
    dry = run_stratum(["gitlab", "--dry-run"], env=env)
    assert dry.returncode == 0 and "MR !7: dry-run" in dry.stdout, dry.stderr
    assert gl.summary_notes(7) == []
    result = run_stratum(["gitlab"], env=env)
    assert result.returncode == 0 and "MR !7: published" in result.stdout, result.stderr
    assert_no_traceback(result.stderr)
    assert len(gl.summary_notes(7)) == 1 and gl.posted_statuses(HEAD)


# ── review.early_checks' calls ───────────────────────────────────────────


def test_an_early_comment_the_review_doesnt_keep_is_deleted_with_its_thread(gl):
    gl.add_mr(7, diff=DIFF)
    api = client(gl)
    api.pull(7)
    api.create_review(7, HEAD, [_comment(5)])
    [thread] = api.review_threads(7)
    api.delete_review_comment(thread["comments"][0]["id"])
    assert gl.threads(7) == [] and gl.requests_to("DELETE", rf"/discussions/{thread['id']}/notes/\d+$")
    with pytest.raises(GitLabError) as exc:
        api.delete_review_comment(thread["comments"][0]["id"])  # gone, and no longer known
    assert exc.value.status == 404


def test_a_started_status_runs_until_the_review_completes_it(gl):
    gl.add_mr(7, diff=DIFF)
    api = GitLab(gl.token, gl.project_id, api=gl.url, pipeline_id=77)
    api.pull(7)
    started = api.start_check_run(HEAD, "Stratum", "Checks: nothing. Model: still running.", "text")
    assert api.check_run_output(started["id"]) == {"title": "Checks: nothing. Model: still running.", "summary": "text"}
    assert api.check_run_output(started["id"] + 1) == {}  # GitLab can't read a status back by its id
    api.check_run(HEAD, "Stratum", "neutral", "No findings", "text", "", [], run_id=started["id"])
    [status] = gl.posted_statuses(HEAD)
    assert gl.status_history == [(started["id"], "running"), (started["id"], "success")]
    assert status["status"] == "success" and status["description"] == "No findings"


def test_with_statuses_off_nothing_is_started(gl):
    gl.add_mr(7, diff=DIFF)
    api = GitLab(gl.token, gl.project_id, api=gl.url, statuses=False)
    assert api.start_check_run(HEAD, "Stratum", "t", "s") == {} and gl.posted == []


def test_early_checks_is_off_on_gitlab_and_says_so(gl, fake_model):
    gl.add_mr(7, diff=DIFF)
    gl.set_policy("review:\n  early_checks: true\n  learn_from_history: true\n")
    assert ci.main(env_for(gl, fake_model)) == 0
    body = gl.summary_notes(7)[0]["body"]
    assert "review.early_checks isn't run on GitLab yet" in body and "review.learn_from_history isn't read on GitLab yet" in body
    assert [state for _, state in gl.status_history] == ["success"]  # posted once, never left running


def test_the_early_path_of_review_pull_holds_on_gitlab(gl, fake_model, make_model):
    """Not reachable from `stratum-open gitlab`, which turns early checks off,
    but `review_pull` must stay correct whatever the summary holds: an early
    post's running status is completed, and its summary edited into the review."""
    from stratum_open.policy import parse_policy
    from stratum_open.runner import Context, post_early, review_pull

    gl.add_mr(7, diff=DIFF)
    api = GitLab(gl.token, gl.project_id, api=gl.url, pipeline_id=77)
    ctx = Context(gh=api, policy=parse_policy("review:\n  early_checks: true\n"), model=make_model(), bot=gl.bot, log=lambda line: None)
    assert post_early(ctx, 7).status == "early"
    assert [state for _, state in gl.status_history] == ["running"]
    fake_model.queue(Response.ok(json_reply([sql_finding()])))
    assert review_pull(ctx, 7).status == "published"
    [summary] = gl.summary_notes(7)
    assert publish.Early.decode(summary["body"], HEAD) is None and publish.State.decode(summary["body"]).head == HEAD
    assert [state for _, state in gl.status_history] == ["running", "failed"]
    assert [t["notes"][0]["position"]["new_line"] for t in gl.threads(7)] == [5]
