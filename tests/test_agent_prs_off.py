"""With every `triage.agent_prs` key off (the default), a review of a PR a
coding agent opened is byte for byte what it was before Stratum could tell:
the same summary, check run and labels, and no read of the PR's commits.
The snapshots were taken with the code from before the check was added, so
this file imports nothing that came with it."""

from __future__ import annotations

import json

import pytest
import snapshots
from fakes import FakeGitHub, FakeModel

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


@pytest.mark.parametrize("name, policy_text", [("defaults", None), ("triage_on", "triage:\n  enabled: true\n")])
def test_with_the_keys_off_a_review_is_byte_for_byte_what_it_was(name, policy_text):
    gh = FakeGitHub(diff=DIFF, author="Copilot", body="Keeps the row.\n\n<!-- START COPILOT CODING AGENT SUFFIX -->\n\n- Fixes #3")
    gh.pulls[7]["head"]["ref"] = "copilot/keep-the-row"
    asked = []
    gh.pull_commits = lambda number: asked.append(number) or [COMMIT]
    model = FakeModel(json.dumps({"summary": "Keeps the row before returning it.", "matches_description": "yes", "mismatch": "", "findings": []}))
    review_pull(Context(gh=gh, policy=parse_policy(policy_text), model=model, bot="stratum[bot]", log=lambda line: None), 7)
    [summary] = gh.issue_comments_[7]
    check = gh.checks[-1]
    snapshots.check(f"agent_prs_off_{name}.md", "\n".join([
        summary["body"], "---", f"check: {check['conclusion']} {check['title']}", f"labels: {gh.labels[7]}",
        f"model calls: {len(model.prompts)}", "",
    ]))
    assert asked == []
