"""With every `triage.agent_prs` key off (the default), a review of a PR a
coding agent opened is byte for byte what it is with the check taken out of
the runner: the same summary, inline comments, check run, labels and model
calls, and no read of the PR's commits. The baseline is made in the same run,
so what the environment adds (which tools are installed) is in both."""

from __future__ import annotations

import json

import pytest
from fakes import FakeGitHub, FakeModel

from stratum_open import agent_prs, runner
from stratum_open.diff import parse_diff
from stratum_open.policy import parse_policy
from stratum_open.runner import Context, review_pull

DIFF = """diff --git a/app/users.py b/app/users.py
--- a/app/users.py
+++ b/app/users.py
@@ -1,4 +1,5 @@
 import sqlite3

 def get_user(conn, user_id):
-    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
+    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
+    return row
"""
COMMIT = {"sha": "c" * 40, "commit": {"message": "Keep the row\n\nCo-authored-by: Dana <dana@example.com>",
                                      "author": {"name": "copilot-swe-agent[bot]", "email": "198982749+Copilot@users.noreply.github.com"}}}


def review(policy_text, monkeypatch, *, without_check):
    """What a review of a Copilot PR leaves on the fake GitHub, and the PRs
    whose commits it read. `without_check`: as if the runner had no
    coding-agent check at all."""
    gh = FakeGitHub(diff=DIFF, author="Copilot", body="Keeps the row.\n\n<!-- START COPILOT CODING AGENT SUFFIX -->\n\n- Fixes #3")
    gh.pulls[7]["head"]["ref"] = "copilot/keep-the-row"
    asked = []
    gh.pull_commits = lambda number: asked.append(number) or [COMMIT]
    model = FakeModel(json.dumps({"summary": "Keeps the row before returning it.", "matches_description": "yes", "mismatch": "", "findings": []}))
    with monkeypatch.context() as patch:
        if without_check:
            patch.setattr(runner, "_agent", lambda *args, **kwargs: None)
        review_pull(Context(gh=gh, policy=parse_policy(policy_text), model=model, bot="stratum[bot]", log=lambda line: None), 7)
    # Comment ids come from one counter shared by every fake, so they're left out; the rest is compared as is.
    left = {"comments": [c["body"] for c in gh.issue_comments_[7]], "reviews": gh.reviews, "checks": gh.checks, "labels": gh.labels,
            "created_labels": gh.created_labels, "prompts": model.prompts}
    return json.dumps(left, sort_keys=True), asked


@pytest.mark.parametrize("policy_text", [None, "triage:\n  enabled: true\n"])
def test_with_the_keys_off_a_review_is_byte_for_byte_what_it_is_without_the_check(policy_text, monkeypatch):
    # The PR carries every strong signal, so the check would have something to say.
    pull = {"user": {"login": "Copilot"}, "body": "<!-- START COPILOT CODING AGENT SUFFIX -->", "head": {"ref": "copilot/keep-the-row"}}
    assert agent_prs.detect(pull, [f.path for f in parse_diff(DIFF)], [COMMIT]).likely
    off, asked = review(policy_text, monkeypatch, without_check=False)
    baseline, _ = review(policy_text, monkeypatch, without_check=True)
    assert off == baseline
    assert asked == []
    assert "coding agent" not in json.loads(off)["comments"][0]
