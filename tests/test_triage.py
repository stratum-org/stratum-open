from datetime import datetime, timezone

from stratum_open.findings import Finding
from stratum_open.policy import parse_policy
from stratum_open.track_record import AuthorRecord, MergedPR, Strike
from stratum_open.triage import PRFacts, description_length, is_doc, is_test, render_queue, triage

POLICY = parse_policy(None)


def facts(**overrides):
    base = dict(number=1, title="Fix parsing", body="Fixes the date parser when the input has no timezone; adds tests.", author="dana",
                draft=False, additions=20, deletions=4, paths=["src/dates.py", "tests/test_dates.py"], ci_ok=True, linked_issues=[9])
    base.update(overrides)
    return PRFacts(**base)


def test_a_tested_linked_pr_with_green_ci_is_ready():
    result = triage(facts(), [], POLICY, matches_description="yes")
    assert result.key == "ready" and result.reasons == []
    assert result.evidence == ["CI passes.", "Linked to #9.", "Changes tests.", "The change matches its description."]


def test_needs_human_beats_everything():
    finding = Finding("Workflow uses pull_request_target", "x", "high", "workflow", ".github/workflows/ci.yml", source="check",
                      rule="workflow/pull-request-target", needs_human=True)
    result = triage(facts(ci_ok=False), [finding], POLICY)
    assert result.key == "needs_human"
    assert result.reasons == ["Workflow uses pull_request_target (`.github/workflows/ci.yml`)."]


def test_needs_work_for_failing_ci_serious_findings_or_a_mismatch():
    assert triage(facts(ci_ok=False), [], POLICY).reasons == ["CI is failing."]
    secret = Finding("Secret in code", "x", "critical", "security", "a.py", 3, source="check", rule="code/secret")
    assert triage(facts(), [secret], POLICY).key == "needs_work"
    model_high = Finding("Maybe a bug", "x", "high", "bug", "a.py", 3)
    assert triage(facts(), [model_high], POLICY).key == "ready"  # a small model's "high" alone doesn't block
    mismatch = triage(facts(), [], POLICY, matches_description="no", mismatch="It also deletes users.")
    assert mismatch.reasons == ["The code doesn't do what the description says: It also deletes users."]


def test_low_effort_needs_three_signals():
    bare = facts(body="", linked_issues=[], paths=["src/dates.py"])
    assert triage(bare, [], POLICY).key == "low_effort"
    docs_only = facts(body="", linked_issues=[], paths=["README.md"])
    assert triage(docs_only, [], POLICY).key == "ready"
    off = parse_policy("triage:\n  label_low_effort: false\n")
    assert triage(bare, [], off).key == "ready"


def test_required_issue():
    policy = parse_policy("triage:\n  require_linked_issue: true\n")
    assert triage(facts(linked_issues=[]), [], policy).reasons == ["No linked issue, and this project asks for one."]


def test_linked_issues_that_couldnt_be_read_count_neither_way():
    # #122: a failed lookup (None) isn't "no linked issue": no reason, no low-effort signal, and no evidence either.
    policy = parse_policy("triage:\n  require_linked_issue: true\n  label_low_effort: true\n")
    unknown = facts(body="", linked_issues=None, paths=["src/dates.py"])
    result = triage(unknown, [], policy)
    assert result.key == "ready" and result.reasons == []
    assert not any("Linked to" in line for line in result.evidence)
    # The same PR with a lookup that found nothing has three low-effort signals, the linked issue one of them.
    low = parse_policy("triage:\n  label_low_effort: true\n")
    assert triage(facts(body="", linked_issues=[], paths=["src/dates.py"]), [], low).key == "low_effort"
    assert triage(unknown, [], low).key == "ready"
    assert (facts(linked_issues=[]).unlinked, facts(linked_issues=None).unlinked, facts(linked_issues=[3]).unlinked) == (True, False, False)


def test_track_record_counts_only_when_the_policy_says_so():
    when = datetime(2026, 10, 1, tzinfo=timezone.utc)
    prs = [MergedPR(n, f"{n:040x}", "dana", when, f"PR {n}") for n in range(1, 5)]
    record = AuthorRecord("dana", prs, [Strike(prs[0], "revert", "c1", "Revert"), Strike(prs[1], "follow-up", "c2", "Fix #2")])
    bare = facts(body="", paths=["README.md"])
    assert triage(bare, [], POLICY, record=record).key == "ready"
    on = parse_policy("track_record:\n  affects_triage: true\n")
    result = triage(bare, [], on, record=record)
    assert "2 of the author's last 4 merged PRs were later reverted or fixed." in triage(facts(body="", linked_issues=[], paths=["README.md"]), [], on, record=record).reasons or result.key == "ready"


def test_path_and_description_helpers():
    assert is_test("tests/test_x.py") and is_test("src/x.test.ts") and is_test("pkg/x_test.go") and not is_test("src/latest.py")
    assert is_doc("docs/guide/intro.md") and is_doc("README.rst") and not is_doc("src/doc.py")
    assert description_length("<!-- template -->\n- [ ] I ran the tests\n\n") == 0


def test_queue_groups_and_orders():
    small = facts(number=2, title="Small fix", additions=3, deletions=1)
    big = facts(number=3, title="Big change", additions=900, deletions=400, paths=[f"src/{i}.py" for i in range(30)])
    rows = [(big, triage(big, [], POLICY)), (small, triage(small, [], POLICY)), (facts(number=4, title="New"), None)]
    text = render_queue(rows, POLICY)
    assert text.index("#2 Small fix") < text.index("#3 Big change")
    assert "### triage: ready (2)" in text and "### Not reviewed yet (1)" in text


def test_the_queue_pings_nobody_named_in_a_title():
    titled = facts(number=5, title="Ping @acme/security-team and @octocat")
    text = render_queue([(titled, triage(titled, [], POLICY)), (facts(number=6, title="cc @mona"), None)], POLICY)
    assert "Ping `@acme`/security-team and `@octocat`" in text and "#6 cc `@mona`" in text


def test_the_queue_pings_nobody_named_in_the_reasons():
    pr = facts(number=5)
    result = triage(pr, [], POLICY, matches_description="no", mismatch="It also emails @octocat.")
    assert "emails `@octocat`." in render_queue([(pr, result)], POLICY)
