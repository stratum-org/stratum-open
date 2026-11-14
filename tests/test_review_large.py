import json
import re
import threading
import time

import pytest
from fakes import FakeModel

from stratum_open import areas
from stratum_open.diff import parse_diff
from stratum_open.engine import analyze
from stratum_open.github import publish
from stratum_open.model import Reply
from stratum_open.policy import parse_policy
from stratum_open.profiles import apply
from stratum_open.review_large import LENSES, _score, review_files

DIFF = """diff --git a/app/users.py b/app/users.py
--- a/app/users.py
+++ b/app/users.py
@@ -1,3 +1,5 @@ import sqlite3
 import sqlite3
+def count(rows):
+    return len(rows) / len([r for r in rows if r])
 x = 1
diff --git a/app/auth/session.py b/app/auth/session.py
--- a/app/auth/session.py
+++ b/app/auth/session.py
@@ -1,2 +1,4 @@
 import jwt
+def check(token):
+    return jwt.decode(token, options={"verify_signature": False})
diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,1 +1,2 @@
 {
+  "lockfileVersion": 3,
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 # Widget
+Now with sessions.
"""


def policy(extra=""):
    return parse_policy("review:\n  profile: large\n  parallel_calls: 1\n" + extra)


def file_answer(findings=(), risk=40, summary="Changes the file."):
    return json.dumps({"risk_score": risk, "change_summary": summary, "why_it_matters": "It matters.", "findings": list(findings)})


def finding(path, code, title="Signature not verified", severity="high", line=3, category="security"):
    return {"file": path, "code": code, "line": line, "severity": severity, "category": category, "title": title,
            "detail": "Any token is accepted, so anyone can pass as any user.", "fix": "Verify the signature."}


def run(model, extra="", **kwargs):
    """The per-file passes alone, with the large profile's defaults under `extra`."""
    return review_files(title="Sessions", description="Adds token checks.", files=parse_diff(DIFF),
                        policy=apply(policy(extra), "large"), model=model, **kwargs)


def test_areas_come_from_the_path():
    assert areas.areas("app/auth/login.py") == ["auth"]
    assert areas.areas("src/decision.py") == []  # "ci" only as its own name
    assert areas.areas(".github/workflows/ci.yml") == ["workflow"]
    assert areas.areas("latest.py") == [] and areas.areas("attestation.py") == []
    assert "tests" in areas.areas("tests/test_users.py") and "tests" in areas.areas("web/app.spec.ts")
    assert areas.generated("package-lock.json") and areas.generated("web/dist/app.js")
    assert areas.generated("rebuild/tool.py") == ""


def test_a_word_marker_matches_only_a_whole_word_of_the_path():
    for path in ("docs/privacy-policy.md", "tokenizer.py", "design-tokens.css", "author.py", "viewmodels/user.py",
                 "mysql_client.py", "composer.json"):
        assert areas.areas(path) == [], path
    for path in ("app/auth/login.py", "AuthService.ts", "OAuth2Client.java", "api/access_token.py", "permissions.py",
                 "app/policies/post_policy.rb"):
        assert areas.areas(path) == ["auth"], path
    for path in ("migrations/0001.py", "db/migrate/0002_add.rb", "models.py", "db/schema.rb", "query.sql"):
        assert areas.areas(path) == ["migration"], path
    for path in ("configuration.py", "tsconfig.json", "webpack.config.js", "Dockerfile", "docker-compose.yml", ".env.example"):
        assert areas.areas(path) == ["config"], path
    for path in ("ci/run.sh", ".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml", "k8s/deployment.yaml"):
        assert areas.areas(path) == ["workflow"], path
    assert areas.areas("requirements-dev.txt") == ["dependency"] and areas.areas("pyproject.toml") == ["dependency"]


def test_files_are_ranked_and_capped():
    model = FakeModel(file_answer(), file_answer())
    result = run(model, "  file_reviews: 2\n  lens_files: 0\n")
    assert len(model.prompts) == 2
    by_path = {f.path: f for f in result.files}
    assert [f.path for f in result.files if f.rank][:2] == ["app/auth/session.py", "app/users.py"]  # the auth file first
    assert by_path["app/auth/session.py"].status == by_path["app/users.py"].status == "reviewed"
    assert by_path["README.md"].reason == "beyond the number of files reviewed one at a time"
    assert by_path["package-lock.json"].status == "skipped" and not by_path["package-lock.json"].rank
    assert "File to review: app/auth/session.py. Touches authentication" in model.prompts[0][1]


def test_lens_files_get_one_call_per_lens_of_the_mode():
    model = FakeModel()
    result = run(model, "  file_reviews: 2\n  lens_files: 1\n")
    assert len(model.prompts) == 3 + 1  # security, performance and logic on the top file; one pass on the next
    systems = [system for system, _ in model.prompts]
    assert all(LENSES[lens] in systems[i] for i, lens in enumerate(("security", "performance", "logic")))
    assert result.files[0].lenses == ("security", "performance", "logic") and result.files[1].lenses == ()

    model = FakeModel()
    run(model, "  mode: security\n  file_reviews: 1\n  lens_files: 1\n")
    assert len(model.prompts) == 2  # the mode's own lens and logic


def test_findings_are_placed_by_the_quoted_code_and_filtered():
    code = '    return jwt.decode(token, options={"verify_signature": False})'
    model = FakeModel(
        file_answer([finding("app/auth/session.py", code, severity="medium"), finding("app/users.py", "x = 1", title="Elsewhere")]),
        file_answer([finding("app/auth/session.py", code, severity="critical"),
                     finding("app/auth/session.py", "def check(token):", title="New function check"),
                     finding("app/auth/session.py", "def check(token):", title="Naming", severity="low")]),
        file_answer(),
    )
    result = run(model, "  file_reviews: 1\n  lens_files: 1\n  mode: performance\n")
    [kept] = result.findings
    assert (kept.path, kept.line_start, kept.severity) == ("app/auth/session.py", 3, "critical")  # one per title, the worst copy
    assert result.dropped == 3  # another file's, a narration and a low one


def test_a_failed_call_and_a_model_that_raises():
    class Raising:
        name = "boom"

        def complete(self, system, user, **kwargs):
            raise RuntimeError("token sk-secret-value in the message")

    result = run(Raising(), "  file_reviews: 1\n  lens_files: 0\n")
    assert result.files[0].status == "failed" and not result.complete
    assert result.calls[0]["error"] == "the model call failed (RuntimeError)"  # never the message, which may carry secrets

    result = run(FakeModel(Reply("", "m", 0, error="timed out")), "  file_reviews: 1\n  lens_files: 0\n")
    assert result.failed_calls == 1 and result.files[0].reason == "the model didn't answer for this file"


@pytest.mark.parametrize("risk, score", [
    (10**400, 100), (-10**400, 0), (1e400, 50), (float("nan"), 50), (float("inf"), 50), (float("-inf"), 50),
    (True, 50), (False, 50), ("75", 75), (" 75 ", 75), ("250", 100), (75.9, 75), (-3, 0),
    ("²", 50), ("nan", 50), ("7" * 5000, 50), (None, 50), ([75], 50),
])
def test_risk_scores_are_clamped_and_anything_else_is_the_default(risk, score):
    assert _score(risk) == score


def test_a_risk_score_that_isnt_a_finite_number_is_the_default():
    code = '    return jwt.decode(token, options={"verify_signature": False})'
    for risk in (float("nan"), float("inf"), float("-inf")):
        result = run(FakeModel(file_answer([finding("app/auth/session.py", code)], risk=risk)), "  file_reviews: 1\n  lens_files: 0\n")
        assert result.files[0].risk_score == 50 and [f.title for f in result.findings] == ["Signature not verified"]


def test_parallel_calls_keep_each_answer_with_its_file():
    class ByFile:
        name = "by-file"

        def __init__(self):
            self.active = self.most = 0
            self.lock = threading.Lock()

        def complete(self, system, user, **kwargs):
            with self.lock:
                self.active += 1
                self.most = max(self.most, self.active)
            path = re.search(r"File to review: (\S+)\.", user).group(1)
            time.sleep(0.05 if "auth" in path else 0.01)
            with self.lock:
                self.active -= 1
            return Reply(file_answer(summary=f"About {path}."), self.name, 0)

    model = ByFile()
    result = run(model, "  file_reviews: 3\n  lens_files: 1\n  parallel_calls: 4\n")
    assert model.most > 1
    assert {f.path: f.change_summary for f in result.files if f.status == "reviewed"} == {
        "app/auth/session.py": "About app/auth/session.py.", "app/users.py": "About app/users.py.", "README.md": "About README.md."}


def test_only_the_files_changed_since_the_last_review():
    model = FakeModel()
    result = run(model, "  lens_files: 0\n", only_paths={"app/users.py"})
    assert len(model.prompts) == 1
    assert {f.path: f.reason for f in result.files}["app/auth/session.py"] == "not changed since the last review"


def test_the_engine_runs_the_passes_then_the_walkthrough():
    code = '    return jwt.decode(token, options={"verify_signature": False})'
    synthesis = json.dumps({"summary": "Adds unverified tokens.", "top_priorities": ["The signature check"],
                            "executive_summary": "Risky.", "walkthrough": "- session.py: adds check()."})
    model = FakeModel(
        file_answer([finding("app/auth/session.py", code)]), file_answer(), file_answer(),  # per file
        json.dumps({"summary": "Adds checks.", "matches_description": "yes", "findings": []}),  # the chunked review
        synthesis,
    )
    result = analyze(title="Sessions", description="Adds token checks.", files=parse_diff(DIFF),
                     policy=parse_policy("review:\n  profile: large\n  parallel_calls: 1\n  lens_files: 0\n"), model=model)
    assert result.profile == "large" and len(model.prompts) == 5
    assert "Signature not verified" in {f.title for f in result.findings}
    assert result.large.synthesized and result.large.walkthrough == "- session.py: adds check()."
    assert result.large.top_priorities == ["The signature check"] and result.complete
    assert "Signature not verified" in model.prompts[-1][1]  # the walkthrough is written from the settled findings


def test_the_small_profile_makes_no_extra_calls():
    model = FakeModel(json.dumps({"summary": "", "findings": []}))
    result = analyze(title="t", description="", files=parse_diff(DIFF), policy=parse_policy("review:\n  profile: small\n"), model=model)
    assert len(model.prompts) == 1 and result.large is None


def test_a_walkthrough_that_doesnt_answer_falls_back():
    model = FakeModel(file_answer(summary="Adds check()."), file_answer(), file_answer(),
                      json.dumps({"summary": "", "findings": []}), "not json at all")
    result = analyze(title="Sessions", description="", files=parse_diff(DIFF),
                     policy=parse_policy("review:\n  profile: large\n  parallel_calls: 1\n  lens_files: 0\n"), model=model)
    assert not result.large.synthesized and result.complete  # a missing walkthrough leaves no code unreviewed
    assert result.large.walkthrough.startswith("This PR touches:\n- app/auth/session.py: Adds check().")


def test_the_summary_shows_priorities_and_a_walkthrough():
    summary = publish.Summary(findings=[], inline=set(), policy=parse_policy(None), model_name="big",
                              priorities=["The signature check"], walkthrough="- session.py: adds check().")
    body = publish.render_summary(summary, publish.State())
    assert "**Check first:**\n- The signature check" in body
    assert "<details><summary>Walkthrough</summary>\n\n- session.py: adds check()." in body


def test_a_failed_walkthrough_still_publishes_its_fallbacks():
    from fakes import FakeGitHub

    from stratum_open.runner import Context, review_pull

    code = '    return jwt.decode(token, options={"verify_signature": False})'
    gh = FakeGitHub(diff=DIFF)
    model = FakeModel(file_answer([finding("app/auth/session.py", code)], summary="Adds check()."), file_answer(), file_answer(),
                      json.dumps({"summary": "Adds token checks.", "matches_description": "yes", "findings": []}), "not json")
    review_pull(Context(gh=gh, policy=parse_policy("review:\n  profile: large\n  parallel_calls: 1\n  lens_files: 0\n"),
                        model=model, bot="stratum[bot]", log=lambda line: None), 7)
    body = gh.issue_comments_[7][0]["body"]
    assert "**What this PR does:** Adds token checks." in body  # the chunked review's, not the fallback's generic sentence
    assert "**Check first:**" in body and "\n- Signature not verified in app/auth/session.py" in body
    assert "<details><summary>Walkthrough</summary>\n\nThis PR touches:\n- app/auth/session.py: Adds check()." in body


def _posted_summary(*replies):
    from fakes import FakeGitHub

    from stratum_open.runner import Context, review_pull

    gh = FakeGitHub(diff=DIFF)
    review_pull(Context(gh=gh, policy=parse_policy("review:\n  profile: large\n  parallel_calls: 1\n  lens_files: 0\n"),
                        model=FakeModel(*replies), bot="stratum[bot]", log=lambda line: None), 7)
    return gh.issue_comments_[7][0]["body"]


def test_mentions_in_the_walkthrough_are_neutralised():
    synthesis = json.dumps({"summary": "Adds auth. cc @octocat please look", "top_priorities": ["Ask @torvalds about the lock"],
                            "executive_summary": "x", "walkthrough": "- session.py: pings @some-org/security-team"})
    body = _posted_summary(file_answer(), file_answer(), file_answer(),
                           json.dumps({"summary": "", "findings": []}), synthesis)
    assert "**What this PR does:** Adds auth. cc `@octocat` please look" in body
    assert "\n- Ask `@torvalds` about the lock" in body
    assert "- session.py: pings `@some-org`/security-team" in body
    assert not re.search(r"(?<!`)@(octocat|torvalds|some-org)", body)


def test_mentions_in_a_files_change_summary_are_neutralised():
    # The walkthrough call fails, so the fallback lists each file's change_summary.
    body = _posted_summary(file_answer(summary="Adds check(), cc @octocat."), file_answer(), file_answer(),
                           json.dumps({"summary": "", "findings": []}), "not json")
    assert "- app/auth/session.py: Adds check(), cc `@octocat`." in body
    assert not re.search(r"(?<!`)@octocat", body)


def test_verify_checks_the_per_file_passes_findings_too():
    # One file pass (the auth file ranks first), no lenses, no walkthrough:
    # the file pass, the chunked review, then the verifier on the file pass's finding.
    jwt_line = 'return jwt.decode(token, options={"verify_signature": False})'
    model = FakeModel(
        file_answer([finding("app/auth/session.py", jwt_line)]),
        json.dumps({"summary": "s", "matches_description": "yes", "findings": []}),
        '{"real": "no", "why": "Checked elsewhere."}',
    )
    result = analyze(title="t", description="d", files=parse_diff(DIFF), model=model,
                     policy=policy("  file_reviews: 1\n  lens_files: 0\n  walkthrough: false\n  verify: strict\n"))
    assert not any(f.source == "model" for f in result.findings)
    assert result.large.dropped == 1
    assert any(c.get("pass") == "verify" for c in result.large.calls)


def test_files_whose_every_answer_is_unusable_stop_being_sent_on_one_commit():
    # #59: on the same commit, each event paid for every per-file and chunked call again.
    from fakes import FakeGitHub

    from stratum_open.runner import Context, review_pull

    gh = FakeGitHub(diff=DIFF)
    calls, lines = [], []
    for _ in range(3):
        model = FakeModel(*["not json"] * 30)
        review_pull(Context(gh=gh, policy=parse_policy("review:\n  profile: large\n  parallel_calls: 1\n  lens_files: 0\n"),
                            model=model, bot="stratum[bot]", log=lambda line: None), 7)
        calls.append(len(model.prompts))
        lines.append([line for line in gh.issue_comments_[7][0]["body"].splitlines() if "per-file model calls failed" in line])
    assert calls == [5, 5, 0]
    assert lines[0] == ["- 3 of 3 per-file model calls failed; the next review reads again the ones the chunked review didn't read in full either."]
    assert lines[1] == ["- 3 of 3 per-file model calls failed; the chunked review didn't read those files in full either."]


def test_unusable_is_only_what_the_model_answered():
    from stratum_open.review_large import LargeReview

    large = LargeReview(calls=[
        {"pass": "file", "path": "a.py", "ok": True, "error": "the reply wasn't the JSON asked for"},
        {"pass": "file", "path": "b.py", "ok": True, "error": "", "salvaged": True},
        {"pass": "file", "path": "c.py", "ok": False, "error": "couldn't reach the model: ConnectTimeout"},
        {"pass": "file", "path": "d.py", "ok": True, "error": ""},
    ])
    assert (large.unread, large.unusable) == ({"a.py", "b.py", "c.py"}, {"a.py", "b.py"})


def test_unread_names_the_files_whose_per_file_call_failed_or_was_cut_off():
    from stratum_open.review_large import LargeReview

    large = LargeReview(calls=[
        {"pass": "file", "path": "a.py", "error": ""},
        {"pass": "lens", "path": "b.py", "error": "the reply wasn't the JSON asked for"},
        {"pass": "file", "path": "c.py", "error": "", "salvaged": True},
        {"pass": "synthesis", "error": "timed out"},  # the walkthrough reads no file
    ])
    assert large.unread == {"b.py", "c.py"}


def test_a_per_file_call_that_failed_on_a_file_the_chunked_review_read_isnt_unread():
    body = _posted_summary("not json", file_answer(), file_answer(),
                           json.dumps({"summary": "Adds token checks.", "matches_description": "yes", "findings": []}), "not json")
    assert "the chunked review still covered those files." in body
    assert publish.State.decode(body).unread == []
