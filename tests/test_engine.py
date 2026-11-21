"""`engine.analyze` called the way an embedder (Stratum) calls it: its own
settings, its own findings, and its hooks."""

import json
from dataclasses import replace

from fakes import FakeModel

from stratum_open import Finding, analyze, parse_diff, parse_policy
from stratum_open.engine import snippet_of
from stratum_open.github.publish import state_finding
from stratum_open.profiles import ModelInfo

DIFF = """diff --git a/app/users.py b/app/users.py
--- a/app/users.py
+++ b/app/users.py
@@ -1,4 +1,6 @@
 import sqlite3
+import requests

 def get_user(conn, user_id):
-    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
+    cur = conn.execute(f"SELECT * FROM users WHERE uid = {user_id}")
+    return cur.fetchone()
"""
FILES = parse_diff(DIFF)


def reply(*findings):
    return json.dumps({"summary": "Changes the query.", "matches_description": "yes", "findings": list(findings)})


UNKNOWN_ID = {"file": "app/users.py", "code": "return cur.fetchone()", "severity": "medium", "category": "bug",
              "title": "None for unknown ids", "detail": "fetchone returns None and callers index it.", "fix": "Check for None."}


def run(model=None, **kwargs):
    return analyze(title="Query users", description="Small refactor.", files=FILES, policy=kwargs.pop("policy", parse_policy(None)),
                   model=model, **kwargs)


def test_checks_only_without_a_model():
    result = run()
    assert result.review is None and result.profile == "small" and result.complete
    assert {f.rule for f in result.findings} >= {"code/sql"}
    assert result.conclusion == "neutral"  # the SQL check is high


def test_the_model_review_merges_with_the_checks_and_reports_the_profile():
    class Big(FakeModel):
        def describe(self):
            return ModelInfo(billions=120, context=200_000, local=False)

    model = Big(reply(UNKNOWN_ID))
    result = run(model)
    assert result.profile == "large" and result.profile_why == "120B parameters"
    assert result.policy.review.diff_format == "split" and model.context_tokens == 32768
    assert {"None for unknown ids"} <= {f.title for f in result.findings}
    assert result.engine_version == __import__("stratum_open").__version__


def test_gpt_oss_gets_its_settings_whatever_the_profile_and_other_models_dont():
    class Named(FakeModel):
        reasoning = "default"

        def describe(self):
            return ModelInfo(billions=20.9, local=True)

    model = Named(reply(), name="gpt-oss:20b")
    result = run(model)
    assert result.profile == "small" and result.profile_why == "20.9B parameters"
    assert (result.policy.review.reasoning, result.policy.review.verify, model.reasoning) == ("low", "strict", "low")
    assert (result.policy.review.logic_pass, result.policy.review.context_lines) == (True, 10)  # over the small profile's 0
    assert result.model_settings == {"review.reasoning": "low", "review.verify": "strict", "review.logic_pass": True, "review.context_lines": 10}
    # An explicit value wins, in any profile.
    result = run(Named(reply(), name="gpt-oss:20b"),
                 policy=parse_policy("review:\n  profile: large\n  verify: none\n  logic_pass: false\n  context_lines: 3\n"))
    assert (result.profile, result.policy.review.verify, result.policy.review.logic_pass, result.policy.review.context_lines) == (
        "large", "none", False, 3)
    assert result.model_settings == {"review.reasoning": "low"}
    # Any other model: the policy as the profile leaves it.
    other = Named(reply(), name="qwen2.5-coder:7b")
    result = run(other)
    assert (result.policy.review.reasoning, result.policy.review.verify, other.reasoning, result.model_settings) == (
        "default", "none", "default", {})
    # Without a model nothing is filled in.
    assert run().model_settings == {} and run().policy.review.verify == "none"


def test_hooks_replace_the_defaults():
    def weigh(findings):
        return [replace(f, severity="critical") if f.rule == "code/sql" else f for f in findings], 7

    seen = {}

    def score(findings, bonus):
        seen["bonus"] = bonus
        return 42

    result = run(FakeModel(reply()), weigh=weigh, score=score, conclusion=lambda findings, total, complete: f"custom:{total}")
    assert seen["bonus"] == 7 and result.score == 42 and result.conclusion == "custom:42"
    assert any(f.severity == "critical" and f.rule == "code/sql" for f in result.findings)


def test_extra_findings_guidance_and_exclusions():
    model = FakeModel(reply(UNKNOWN_ID))
    mine = Finding("Calls a service it shouldn't", "The billing service is off limits here.", "high", "breaking-change",
                   "app/users.py", 2, source="check", rule="stratum/contracts")
    result = run(model, extra_findings=[mine], guidance="We always check fetchone() for None.", exclude_rules={"code/sql"})
    system, _ = model.prompts[0]
    assert "We always check fetchone() for None." in system
    rules = {f.rule for f in result.findings}
    assert "stratum/contracts" in rules and "code/sql" not in rules
    assert [f.rule for f in result.excluded] == ["code/sql"]


def test_a_model_that_raises_is_an_outage_or_a_bug_by_its_exception():
    """Each call says which: the model not answering, or a bug (Stratum's own
    `Model` raising anything else is one). Never the exception's message,
    which may carry a secret; the traceback is kept for the log."""
    class Raising(FakeModel):
        failure_types = ()

        def __init__(self, exc):
            super().__init__()
            self.exc = exc

        def complete(self, system, user, *, max_tokens=2048, json_mode=True):
            raise self.exc

    class OtherClientError(Exception):  # an embedder's HTTP library, which derives from neither OSError nor requests
        pass

    def error_of(model):
        result = run(model)
        [call] = result.review.calls
        assert not result.review.ran and "Traceback" in call["traceback"]
        return call["error"]

    assert error_of(Raising(ConnectionResetError("peer closed"))) == "the model call failed (ConnectionResetError)"
    assert error_of(Raising(TimeoutError("slow"))) == "the model call failed (TimeoutError)"
    assert error_of(Raising(OtherClientError("boom"))) == "Stratum hit a bug during the model step (OtherClientError)"
    assert error_of(Raising(KeyError("file"))) == "Stratum hit a bug during the model step (KeyError)"
    declared = Raising(OtherClientError("boom"))
    declared.failure_types = (OtherClientError,)
    assert error_of(declared) == "the model call failed (OtherClientError)"


def test_a_bug_in_the_model_step_itself_is_named_as_one(monkeypatch):
    def broken(**kwargs):
        raise KeyError("file")

    monkeypatch.setattr("stratum_open.engine.review", broken)
    result = run(FakeModel(reply(UNKNOWN_ID)))
    [call] = result.review.calls
    assert call["error"] == "Stratum hit a bug during the model step (KeyError)"  # the message may carry a secret: only the log has it
    assert "Traceback" in call["traceback"] and "KeyError: 'file'" in call["traceback"]
    assert not result.review.ran and {f.rule for f in result.findings} >= {"code/sql"}  # the checks still report


def test_a_model_that_raises_in_the_verify_pass_keeps_the_finding():
    class AnswersOnce(FakeModel):
        """Answers its first call, then its HTTP client times out on every call after."""
        failure_types = (TimeoutError,)

        def complete(self, system, user, **kwargs):
            if self.prompts:
                self.prompts.append((system, user))
                raise TimeoutError("read timed out")
            return super().complete(system, user, **kwargs)

    per_file = "review:\n  profile: large\n  parallel_calls: 1\n  file_reviews: 1\n  lens_files: 0\n  walkthrough: false\n"
    for policy, calls in ((per_file, lambda r: r.large.calls), ("review:\n  profile: small\n", lambda r: r.review.calls)):
        result = run(AnswersOnce(reply(UNKNOWN_ID)), policy=parse_policy(policy + "  verify: strict\n"))
        assert "None for unknown ids" in {f.title for f in result.findings}
        [failed] = [c for c in calls(result) if c.get("pass") == "verify"]
        assert not failed["ok"] and failed["error"] == "the model call failed (TimeoutError)"


def test_previous_findings_carry_over_on_an_incremental_review():
    first = run(FakeModel(reply(UNKNOWN_ID)))
    prior = [state_finding(f, snippet_of(f, FILES)) for f in first.findings if f.source == "model"]
    model = FakeModel()  # nothing new for the model to read
    again = run(model, only_paths=set(), previous=prior)
    assert model.prompts == [] and again.review is None
    assert "None for unknown ids" in {f.title for f in again.findings}
