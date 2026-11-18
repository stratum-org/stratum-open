"""I. Triage labels and the queue issue."""

from __future__ import annotations

import pytest

from stratum_open.runner import review_pull
from stratum_open.triage_queue import TITLE, update_queue

from conftest import DIFF, SQL_LINE, head_of
from contract import assert_contract
from fake_model import Response, finding, json_reply

HEAD = head_of(7)
ON = "triage:\n  enabled: true\n"


def test_queue_with_no_open_prs(fake_github, make_context):
    body = update_queue(make_context(model=None), post=True)
    assert body.startswith("Open PRs sorted by Stratum.")
    assert [i["title"] for i in fake_github.issues] == [TITLE]


@pytest.mark.slow
def test_queue_with_a_thousand_open_prs_is_capped(fake_github, make_context):
    for n in range(1, 1001):
        fake_github.add_pull(n, diff=DIFF, title=f"PR {n}", body=None, linked_issues=[])
    body = update_queue(make_context(model=None), post=False)
    assert "Not reviewed yet (100)" in body
    assert len(fake_github.requests_to("GET", r"/issues/\d+/comments")) == 100


def test_prs_with_no_ci_tests_or_issue_are_sorted_and_explained(fake_github, make_context, logs):
    fake_github.add_pull(7, diff=DIFF, body="x", linked_issues=[])
    outcome = review_pull(make_context(policy_text=ON), 7)
    assert outcome.triage.key == "needs_work"  # the SQL check is a serious finding
    reasons = " ".join(outcome.triage.reasons)
    assert "serious finding" in reasons
    body = update_queue(make_context(model=None, policy_text=ON), post=True)
    assert "### triage: needs work (1)" in body and "#7 Add a thing" in body
    assert_contract(fake_github, number=7, head=HEAD, logs=logs)


def test_ci_waiting_for_approval_is_not_a_pass(fake_github, make_context, fake_model, logs):
    # A first-time contributor's PR from a fork: GitHub holds `tests` for approval, so it made no check runs; the CLA check ran.
    clean = DIFF.replace(f"+    {SQL_LINE}", '+    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))')
    fake_github.add_pull(7, diff=clean, fork=True, linked_issues=[3])
    fake_github.ci_runs[HEAD] = [{"name": "cla", "status": "completed", "conclusion": "success"}]
    fake_github.workflow_runs[HEAD] = [{"id": 1, "workflow_id": 1, "name": "tests", "event": "pull_request", "status": "completed",
                                        "conclusion": "action_required", "created_at": "2026-01-01T12:00:00Z"}]
    fake_model.queue(Response.ok(json_reply([])))
    outcome = review_pull(make_context(policy_text=ON), 7)
    assert outcome.triage.key == "needs_human" and "CI passes." not in outcome.triage.evidence
    assert "CI hasn't run: a maintainer needs to approve the workflows. Comment `/stratum review` once they've run to sort it again." in outcome.triage.reasons
    assert fake_github.labels_on(7) == ["triage: needs a maintainer"]
    asked = fake_github.requests_to("GET", r"/actions/runs$")
    assert [(r["query"]["head_sha"], r["query"]["event"]) for r in asked] == [(HEAD, "pull_request")]
    assert_contract(fake_github, number=7, head=HEAD, logs=logs)


def test_label_exists_with_a_different_colour_and_is_not_recreated(fake_github, make_context, logs):
    fake_github.labels["triage: needs work"] = {"name": "triage: needs work", "color": "ffffff", "description": "ours"}
    fake_github.add_pull(7, diff=DIFF)
    review_pull(make_context(policy_text=ON), 7)
    assert fake_github.requests_to("POST", r"/repos/[^/]+/[^/]+/labels$") == []  # the label itself was not recreated
    assert fake_github.labels["triage: needs work"]["color"] == "ffffff"
    assert fake_github.labels_on(7) == ["triage: needs work"]


def test_label_missing_and_cannot_be_created(fake_github, make_context, logs):
    fake_github.add_pull(7, diff=DIFF)
    fake_github.fail("POST", r"/labels$", 403, body={"message": "Resource not accessible by integration"}, times=0)
    outcome = review_pull(make_context(policy_text=ON), 7)
    assert outcome.status == "published" and fake_github.labels_on(7) == []
    assert any("couldn't apply the triage label" in line for line in logs)
    assert_contract(fake_github, number=7, head=HEAD, logs=logs)


def test_label_created_by_a_racing_run_is_fine(fake_github, make_context, logs):
    fake_github.add_pull(7, diff=DIFF)
    fake_github.fail("GET", r"/labels/triage", 404, times=1)
    fake_github.labels["triage: needs work"] = {"name": "triage: needs work", "color": "d93f0b", "description": ""}
    review_pull(make_context(policy_text=ON), 7)
    assert fake_github.labels_on(7) == ["triage: needs work"]
    assert_contract(fake_github, number=7, head=HEAD, logs=logs)


def test_label_changes_with_new_commits_and_the_stale_one_goes(fake_github, make_context, fake_model, logs):
    fake_github.add_pull(7, diff=DIFF, labels=["triage: ready", "bug", "triage: low effort"])
    ctx = make_context(policy_text=ON)
    review_pull(ctx, 7)
    assert sorted(fake_github.labels_on(7)) == ["bug", "triage: needs work"]
    fixed = DIFF.replace(f"+    {SQL_LINE}", '+    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))')
    fake_github.move_head(7, "b" * 40, fixed, changed=["app/users.py"])
    fake_model.queue(Response.ok(json_reply([])))
    outcome = review_pull(ctx, 7)
    assert outcome.triage.key in ("ready", "low_effort")
    triage_labels = [n for n in fake_github.labels_on(7) if n.startswith("triage:")]
    assert len(triage_labels) == 1 and triage_labels != ["triage: needs work"]
    assert_contract(fake_github, number=7, head="b" * 40, logs=logs)


def test_triage_off_labels_and_closes_nothing(fake_github, make_context, logs):
    fake_github.add_pull(7, diff=DIFF, body="x", linked_issues=[])
    review_pull(make_context(policy_text="triage:\n  enabled: false\n  require_linked_issue: true\n  close_without_issue: true\n"), 7)
    assert fake_github.labels_on(7) == [] and fake_github.closed == []
    assert "labels are off in this project's policy" in fake_github.summary_comments(7)[0]["body"]


def test_triage_on_with_close_without_issue_closes_and_says_why(fake_github, make_context, fake_model, logs):
    fake_github.add_pull(7, diff=DIFF.replace("+    cur = conn.execute(f\"SELECT * FROM users WHERE id = {user_id}\")", "+    cur = 1"), body="x", linked_issues=[])
    fake_model.queue(Response.ok(json_reply([])))
    review_pull(make_context(policy_text=ON + "  require_linked_issue: true\n  close_without_issue: true\n"), 7)
    assert fake_github.closed == [7]
    assert any("Closing this PR" in c["body"] for c in fake_github.issue_comments[7])
    assert_contract(fake_github, number=7, head=HEAD, logs=logs)


def test_the_queue_issue_is_updated_in_place(fake_github, make_context, logs):
    fake_github.add_pull(7, diff=DIFF)
    ctx = make_context(policy_text=ON)
    review_pull(ctx, 7)
    update_queue(ctx, post=True)
    fake_github.add_pull(8, diff=DIFF, title="Second")
    review_pull(ctx, 8)
    update_queue(ctx, post=True)
    assert [i["title"] for i in fake_github.issues] == [TITLE]
    assert "#8 Second" in fake_github.issues[0]["body"] and fake_github.requests_to("PATCH", r"/issues/\d+$")
    assert_contract(fake_github, number=8, head=head_of(8), logs=logs)


def test_a_queue_issue_by_someone_else_is_not_edited(fake_github, make_context):
    fake_github.issues.append({"number": 50, "title": TITLE, "body": "mine", "state": "open", "user": {"login": "mallory"}})
    update_queue(make_context(model=None), post=True)
    assert len(fake_github.issues) == 2 and fake_github.issues[0]["body"] == "mine"
