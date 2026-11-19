from stratum_open.checks import heuristics, run_checks
from stratum_open.diff import parse_diff
from stratum_open.policy import parse_policy
from stratum_open.profiles import apply


def diff(path, added=(), removed=()):
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{len(removed) + 1} +1,{len(added) + 1} @@\n context\n{body}")


def rules(text, pr_text=""):
    return sorted(f.rule for f in heuristics.check(parse_diff(text), pr_text))


def test_each_heuristic_fires_on_its_area():
    assert rules(diff("app/auth/session.py", ["x = 1"])) == ["heuristic/auth-path"]
    assert rules(diff("db/migrations/0042_add.sql", ["ALTER TABLE t ADD c int;"])) == ["heuristic/migration"]
    assert rules(diff(".github/workflows/ci.yml", ["on: push"])) == ["heuristic/workflow-changed"]
    assert rules(diff("requirements.txt", ["requests==2.32.0"])) == ["heuristic/dependencies-changed"]
    assert rules(diff("deploy/config.yaml", ["cors_origins: ['*']"])) == ["heuristic/cors-config", "heuristic/workflow-changed"]
    assert rules(diff("src/decision.py", ["x = 1"])) == []  # not CI, despite the "ci" in its name
    assert rules(diff("docs/privacy-policy.md", ["typo"])) == []  # not auth, despite "policy"
    assert rules(diff("app/viewmodels/user.py", ["x = 1"])) == []  # not a migration, despite "model"


def test_tests_that_lose_more_than_they_gain():
    assert rules(diff("tests/test_users.py", ["pass"], ["assert a", "assert b"])) == ["heuristic/tests-weakened"]
    assert rules(diff("tests/test_users.py", ["assert a", "assert b"], ["pass"])) == []
    assert rules(diff("latest.py", [], ["x = 1", "y = 2"])) == []  # not a test file


def test_a_breaking_change_is_read_from_the_pr_text_not_the_code():
    assert rules(diff("CHANGELOG.md", ["- Breaking change: x"])) == []
    [finding] = heuristics.check(parse_diff(diff("app/x.py", ["x = 1"])), "Drop py3.9\nThis is a breaking change for 3.9 users.")
    assert (finding.rule, finding.path, finding.line_start) == ("heuristic/breaking-change", "", None)


def test_they_run_only_when_the_policy_turns_them_on():
    files = parse_diff(diff("app/auth/session.py", ["x = 1"]))
    assert not [f for f in run_checks(files, parse_policy(None)) if f.rule.startswith("heuristic/")]
    assert not [f for f in run_checks(files, apply(parse_policy(None), "small")) if f.rule.startswith("heuristic/")]
    large = [f for f in run_checks(files, apply(parse_policy(None), "large")) if f.rule.startswith("heuristic/")]
    assert [f.rule for f in large] == ["heuristic/auth-path"]
    assert all(f.source == "check" and f.line_start is None for f in large)
    off = apply(parse_policy("checks:\n  heuristics: false\n"), "large")  # the file's choice beats the profile
    assert not [f for f in run_checks(files, off) if f.rule.startswith("heuristic/")]


def test_secrets_and_sql_are_left_to_the_pattern_checks():
    text = diff("app/db.py", ['API_KEY = "sk-abcdefghijklmnopqrstuvwx"', 'cur.execute(f"SELECT * FROM t WHERE id = {i}")'])
    assert rules(text) == []
    found = {f.rule for f in run_checks(parse_diff(text), apply(parse_policy(None), "large"))}
    assert {"code/secret", "code/sql"} <= found


def test_an_area_sends_the_pr_to_a_maintainer_not_back_to_its_author():
    from stratum_open.platform import CI
    from stratum_open.triage import PRFacts, triage

    facts = PRFacts(number=1, title="Tidy a comment", body="Rewords a comment in the session code; no behaviour change.", author="dana",
                    draft=False, additions=1, deletions=1, paths=["app/auth/session.py"], ci=CI.PASSED, linked_issues=[9])
    files = parse_diff(diff("app/auth/session.py", ["# checks the token"], ["# check token"]))
    assert triage(facts, heuristics.check(files), parse_policy(None)).key == "needs_human"
    weakened = heuristics.check(parse_diff(diff("tests/test_users.py", ["pass"], ["assert a", "assert b"])))
    assert triage(facts, weakened, parse_policy(None)).key == "needs_work"  # the author can restore the tests
