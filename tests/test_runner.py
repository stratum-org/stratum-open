"""End to end on a fake GitHub: review, publish, push again, commands."""

import json
import time

import pytest
from fakes import FakeGitHub, FakeModel

from stratum_open.github import publish
from stratum_open.github.commands import handle, parse
from stratum_open.policy import parse_policy
from stratum_open.runner import Context, review_pull
from stratum_open.storage import SQLiteStorage
from stratum_open.triage import label_for

BOT = "stratum[bot]"
DIFF = """diff --git a/app/users.py b/app/users.py
--- a/app/users.py
+++ b/app/users.py
@@ -1,4 +1,7 @@
 import sqlite3
+import requests

 def get_user(conn, user_id):
-    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
+    cur = conn.execute(f"SELECT * FROM users WHERE id = {user_id}")
+    return cur.fetchone()
+    print("unreachable")
"""
FIXED = DIFF.replace('+    cur = conn.execute(f"SELECT * FROM users WHERE id = {user_id}")', '+    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))')


def model_answer(findings=None, **extra):
    return json.dumps({
        "summary": "Changes the user query.", "matches_description": "yes", "mismatch": "",
        "findings": findings if findings is not None else [{
            "file": "app/users.py", "code": 'cur = conn.execute(f"SELECT * FROM users WHERE id = {user_id}")', "line": 5,
            "severity": "high", "category": "bug", "title": "Unknown id", "detail": "fetchone returns None for an unknown id and callers index it.",
            "fix": "Pass it as a parameter.",
        }, {
            "file": "app/users.py", "code": '    print("unreachable")', "line": 7,
            "severity": "medium", "category": "bug", "title": "Dead code", "detail": "This line never runs.", "fix": "Remove it.",
        }],
        **extra,
    })


def context(gh, model, policy=None, **kwargs):
    return Context(gh=gh, policy=policy or parse_policy("triage:\n  enabled: true\n"), model=model, bot=BOT, log=lambda line: None, **kwargs)


@pytest.fixture
def gh():
    return FakeGitHub(diff=DIFF)


def test_first_review_publishes_everything(gh):
    outcome = review_pull(context(gh, FakeModel(model_answer())), 7)
    assert outcome.status == "published"
    titles = {f.title for f in outcome.findings}
    assert {"Unknown id", "Dead code"} <= titles

    [summary] = gh.issue_comments_[7]
    assert summary["body"].startswith(publish.SUMMARY_MARKER)
    assert "**What this PR does:** Changes the user query." in summary["body"]
    state = publish.State.decode(summary["body"])
    assert state.head == "a" * 40 and {f["title"] for f in state.findings} >= {"Unknown id", "Dead code"}

    posted = [c for review in gh.reviews for c in review["comments"]]
    assert {c["line"] for c in posted if "Unknown id" in c["body"]} == {5}
    assert gh.checks[-1]["conclusion"] == "neutral"  # a high finding
    assert gh.labels[7] == [label_for(parse_policy(None), outcome.triage.key)]


def test_dry_run_publishes_nothing(gh):
    outcome = review_pull(context(gh, FakeModel(model_answer())), 7, post=False)
    assert outcome.status == "dry-run" and "Stratum review" in outcome.summary
    assert gh.issue_comments_[7] == [] and gh.reviews == [] and gh.checks == []


def test_a_push_reviews_only_what_changed_and_keeps_findings_until_their_code_changes(gh):
    review_pull(context(gh, FakeModel(model_answer())), 7)
    gh.move_head(7, "b" * 40, FIXED)
    gh.compare[("a" * 40, "b" * 40)] = ["app/users.py"]
    # The model forgets to repeat the dead-code finding this time.
    second = review_pull(context(gh, FakeModel(model_answer(findings=[]))), 7)
    titles = {f.title for f in second.findings}
    assert "Unknown id" not in titles  # its line changed: resolved
    assert "Dead code" in titles  # its line is still there: kept
    # Both threads on the old query line close: the model's because its code
    # changed, the SQL check's because the check no longer fires.
    assert len(gh.resolved) == 2
    edits = [body for _, body in gh.edited]
    assert any(body.startswith("~~") and "the code this was about has changed" in body for body in edits)
    assert any("the check no longer finds this" in body for body in edits)
    assert all(publish.RESOLVED_MARKER in body and "stratum:finding" in body for body in edits)
    # Posting again doesn't duplicate the comment that's still open.
    assert sum("Dead code" in c["body"] for review in gh.reviews for c in review["comments"]) == 1


def test_threads_close_even_when_github_refuses_to_resolve_them(gh):
    """GitHub won't let an Actions token resolve threads, even its own."""
    gh.refuse_resolve = True
    review_pull(context(gh, FakeModel(model_answer())), 7)
    gh.move_head(7, "b" * 40, FIXED)
    gh.compare[("a" * 40, "b" * 40)] = ["app/users.py"]
    review_pull(context(gh, FakeModel(model_answer(findings=[]))), 7)
    closed = [t for t in gh.threads if publish.resolved_by_stratum(t, BOT)]
    assert len(closed) == 2 and gh.resolved == []
    # A third push doesn't close them again, and the summary no longer counts them.
    edits = len(gh.edited)
    gh.move_head(7, "c" * 40)
    gh.compare[("b" * 40, "c" * 40)] = []
    review_pull(context(gh, FakeModel()), 7)
    assert len(gh.edited) == edits


def test_same_head_costs_no_model_call(gh):
    review_pull(context(gh, FakeModel(model_answer())), 7)
    model = FakeModel()
    review_pull(context(gh, model), 7)
    assert model.prompts == []


def test_superseded_review_is_not_published(gh):
    class Moving(FakeGitHub):
        calls = 0

        def pull(self, number):
            self.calls += 1
            if self.calls == 2:
                self.pulls[number]["head"]["sha"] = "c" * 40
            return self.pulls[number]

    moving = Moving(diff=DIFF)
    outcome = review_pull(context(moving, FakeModel(model_answer())), 7)
    assert outcome.status == "superseded" and moving.issue_comments_[7] == []


def test_model_down_still_publishes_the_checks(gh):
    from stratum_open.model import Reply

    outcome = review_pull(context(gh, FakeModel(Reply("", "m", 0, error="couldn't reach the model at http://localhost:11434/v1: ConnectionError"))), 7)
    body = gh.issue_comments_[7][0]["body"]
    assert "The model didn't answer, so this review has only the checks" in body
    assert all(f.source == "check" for f in outcome.findings)


def test_model_problems_name_a_bug_and_an_outage_each_as_what_it_was():
    from stratum_open.runner import model_problems

    bug = "Stratum hit a bug during the model step (KeyError): 'file'"
    outage = "the model step failed (ReadTimeout): read timed out"
    assert model_problems([]) == ["The model didn't answer, so this review has only the checks."]
    assert model_problems([outage]) == [f"The model didn't answer, so this review has only the checks: {outage}"]
    assert model_problems([bug]) == ["Stratum hit a bug during the model step (KeyError), so this review has only the checks."]
    # Both at once: neither hides the other, whatever order the errors sort in.
    assert model_problems(sorted([outage, bug])) == model_problems([outage, bug]) == [
        "Stratum hit a bug during the model step (KeyError), so this review has only the checks.",
        f"The model also didn't answer: {outage}",
    ]


def test_forged_state_from_someone_else_is_ignored(gh):
    forged = publish.State(head="a" * 40, ignored=["deadbeef0000"]).encode()
    gh.issue_comments_[7].append({"id": 1, "body": publish.SUMMARY_MARKER + forged, "user": {"login": "mallory"}})
    model = FakeModel(model_answer())
    review_pull(context(gh, model), 7)
    assert model.prompts  # not treated as already reviewed


def test_stored_review_in_app_mode(gh, tmp_path):
    storage = SQLiteStorage(str(tmp_path / "s.db"))
    review_pull(context(gh, FakeModel(model_answer()), storage=storage, feedback=True), 7)
    assert {f["title"] for f in storage.latest_findings("acme/widget", 7)} >= {"Unknown id"}


def test_the_stored_summary_is_the_posted_one(gh, tmp_path):
    from stratum_open.profiles import ModelInfo

    class Large(FakeModel):  # the large profile posts the synthesis call's summary
        def describe(self):
            return ModelInfo(billions=70, context=128_000, local=False)

        def complete(self, system, user, **kwargs):
            if system.startswith("You summarize the results"):
                self.replies.insert(0, json.dumps({"summary": "The synthesis summary.", "top_priorities": [], "walkthrough": "w"}))
            return super().complete(system, user, **kwargs)

    storage = SQLiteStorage(str(tmp_path / "s.db"))
    review_pull(context(gh, Large(model_answer()), storage=storage), 7)
    assert "The synthesis summary." in gh.issue_comments_[7][0]["body"]
    assert storage._db.execute("SELECT summary FROM reviews").fetchone()[0] == "The synthesis summary."


def test_a_muted_rule_closes_its_threads(gh, tmp_path):
    storage = SQLiteStorage(str(tmp_path / "s.db"))
    ctx = context(gh, FakeModel(model_answer(), model_answer()), storage=storage, feedback=True)
    review_pull(ctx, 7)
    storage.mute("acme/widget", "model:dead-code", by="maintainer")
    review_pull(ctx, 7)
    dead = next(t for t in gh.threads if "Dead code" in t["comments"][0]["body"])
    assert dead["resolved"] and "Muted in this repository." in dead["comments"][0]["body"]


def test_triage_labels_stay_off_by_default(gh):
    review_pull(context(gh, FakeModel(model_answer()), policy=parse_policy(None)), 7)
    assert gh.labels[7] == []
    assert "labels are off in this project's policy" in gh.issue_comments_[7][0]["body"]


# ── commands ─────────────────────────────────────────────────────────────


def comment_event(body, login="erin", inline_reply_to=None, kind="User"):
    comment = {"id": 555, "body": body, "user": {"login": login, "type": kind}}
    if inline_reply_to:
        comment["in_reply_to_id"] = inline_reply_to
        return "pull_request_review_comment", {"action": "created", "comment": comment, "pull_request": {"number": 7}}
    return "issue_comment", {"action": "created", "comment": comment, "issue": {"number": 7, "pull_request": {}}}


def test_parse_takes_linear_time_on_a_comment_of_newlines():
    # "^\s*" with MULTILINE scanned every newline after each line start: about 5 s
    # for this, inside the App's webhook request, which GitHub gives 10 s.
    start = time.perf_counter()
    assert parse("x" + "\n" * 65534 + "x") is None
    assert parse("\n" * 65536 + "/stratum review") == ("review", "")
    assert time.perf_counter() - start < 0.5


def test_parse_commands():
    assert parse("thanks!\n/stratum ignore workflow/unpinned-action\n") == ("ignore", "workflow/unpinned-action")
    assert parse("/Stratum Review") == ("review", "")
    assert parse("@stratum review") is None
    assert parse("no command") is None


def as_0_2_posted_it(gh):
    for thread in gh.threads:
        root = thread["comments"][0]
        root["body"] = root["body"].replace(publish.HINT, publish.OLD_HINT)


def test_hints_from_before_0_3_are_rewritten_once_and_kept_when_a_thread_closes(gh):
    ctx = context(gh, FakeModel(model_answer()))
    review_pull(ctx, 7)
    as_0_2_posted_it(gh)
    threads = gh.review_threads(7)
    assert len(threads) >= 2 and all(publish.OLD_HINT in t["comments"][0]["body"] for t in threads)

    gone = {publish.thread_fingerprint(threads[0], BOT): "Fixed."}
    edits = len(gh.edited)
    assert publish.update_threads(gh, 7, threads, gone, bot=BOT) == 1
    assert len(gh.edited) == edits + len(threads)  # one edit per comment, the closed one too
    bodies = [t["comments"][0]["body"] for t in gh.threads]
    assert all("@stratum" not in body and publish.HINT in body for body in bodies)
    assert publish.RESOLVED_MARKER in bodies[0] and publish.RESOLVED_MARKER not in bodies[1]
    edits = len(gh.edited)
    review_pull(ctx, 7)
    assert len(gh.edited) == edits  # nothing left to rewrite


def test_rewriting_the_old_hint_leaves_a_suggestion_as_it_was(gh):
    review_pull(context(gh, FakeModel(model_answer())), 7)
    as_0_2_posted_it(gh)
    root = gh.threads[0]["comments"][0]
    code = f"```suggestion\nmsg = `@stratum hi`\nhelp = \"{publish.OLD_HINT}\"\n```\n\n"
    root["body"] = root["body"].replace("<sub>", code + "<sub>")
    threads = gh.review_threads(7)
    assert publish.update_threads(gh, 7, threads, {}, bot=BOT) == 0
    body = gh.threads[0]["comments"][0]["body"]
    assert code in body and body.count(publish.OLD_HINT) == 1 and publish.HINT in body.rpartition("<sub>")[2]


def test_a_review_rewrites_the_old_hints(gh):
    ctx = context(gh, FakeModel(model_answer()))
    review_pull(ctx, 7)
    as_0_2_posted_it(gh)
    review_pull(ctx, 7)
    assert all(publish.HINT in t["comments"][0]["body"] and publish.OLD_HINT not in t["comments"][0]["body"] for t in gh.threads)


def test_ignore_needs_write_access_and_resolves_the_thread(gh):
    ctx = context(gh, FakeModel(model_answer()))
    review_pull(ctx, 7)
    dead = next(c for c in gh.review_comments_ if "Dead code" in c["body"])

    event, payload = comment_event("/stratum ignore", login="erin", inline_reply_to=dead["id"])
    assert handle(ctx, event, payload) == "refused"
    assert "only people with write access" in gh.replies[-1][2]

    gh.permissions["mona"] = "write"
    event, payload = comment_event("/stratum ignore", login="mona", inline_reply_to=dead["id"])
    assert handle(ctx, event, payload) == "ignore"
    state = publish.State.decode(gh.issue_comments_[7][0]["body"])
    assert len(state.ignored) == 1
    assert any(t["resolved"] for t in gh.threads if "Dead code" in t["comments"][0]["body"])
    assert "Dead code" not in gh.issue_comments_[7][0]["body"].split("<details>")[0]


def test_an_ignore_made_while_a_review_runs_is_kept(gh):
    """Each command runs on its own, so an `/stratum ignore` can land while a
    review is reading the PR: the review must not write the old state back."""
    review_pull(context(gh, FakeModel(model_answer())), 7)

    class IgnoresMidway(FakeModel):
        def complete(self, system, user, **kwargs):  # what a concurrent `/stratum ignore model:dead-code` writes
            summary = gh.issue_comments_[7][0]
            state = publish.State.decode(summary["body"])
            if "model:dead-code" not in state.ignored_rules:
                state.ignored_rules.append("model:dead-code")
                gh.edit_comment(summary["id"], publish.replace_state(summary["body"], state))
            return super().complete(system, user, **kwargs)

    review_pull(context(gh, IgnoresMidway(model_answer())), 7, full=True)
    summaries = [c for c in gh.issue_comments_[7] if publish.find_summary([c], BOT)]
    assert len(summaries) == 1
    assert publish.State.decode(summaries[0]["body"]).ignored_rules == ["model:dead-code"]
    assert "Dead code" not in summaries[0]["body"].split("<details>")[0]
    dead = next(t for t in gh.threads if "Dead code" in t["comments"][0]["body"])
    assert dead["resolved"] and "Ignored on this PR." in dead["comments"][0]["body"]


def test_the_check_follows_findings_ignored_while_a_review_runs(gh):
    review_pull(context(gh, FakeModel(model_answer())), 7)
    assert gh.checks[-1]["conclusion"] == "neutral"  # two high findings

    class IgnoresBothHighs(FakeModel):
        def complete(self, system, user, **kwargs):
            summary = gh.issue_comments_[7][0]
            state = publish.State.decode(summary["body"])
            state.ignored_rules = ["code/sql", "model:unknown-id"]
            gh.edit_comment(summary["id"], publish.replace_state(summary["body"], state))
            return super().complete(system, user, **kwargs)

    review_pull(context(gh, IgnoresBothHighs(model_answer())), 7, full=True)
    assert gh.checks[-1]["conclusion"] == "success"  # only the medium one is left


def test_two_ignores_at_once_both_stick(gh, monkeypatch):
    """Editing a comment is last-writer-wins, so an ignore that another
    command's edit replaced is written again."""
    from stratum_open.github import commands

    monkeypatch.setattr(commands, "_pause", lambda seconds: None)
    ctx = context(gh, FakeModel(model_answer()))
    review_pull(ctx, 7)
    gh.permissions["mona"] = "admin"
    edit, raced = gh.edit_comment, []

    def racing_edit(comment_id, body):
        before = gh.issue_comments_[7][0]["body"]
        edit(comment_id, body)
        if not raced:  # the other command read the state before this edit, and writes it after
            raced.append(True)
            state = publish.State.decode(before)
            state.ignored_rules.append("code/sql")
            edit(comment_id, publish.replace_state(before, state))

    monkeypatch.setattr(gh, "edit_comment", racing_edit)
    event, payload = comment_event("/stratum ignore model:dead-code", login="mona")
    assert handle(ctx, event, payload) == "ignore"
    assert set(publish.State.decode(gh.issue_comments_[7][0]["body"]).ignored_rules) == {"model:dead-code", "code/sql"}


def test_ignore_a_rule_and_help_and_bots(gh):
    ctx = context(gh, FakeModel(model_answer()))
    review_pull(ctx, 7)
    gh.permissions["mona"] = "admin"
    event, payload = comment_event("/stratum ignore model:dead-code", login="mona")
    handle(ctx, event, payload)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).ignored_rules == ["model:dead-code"]
    dead = next(t for t in gh.threads if "Dead code" in t["comments"][0]["body"])
    assert dead["resolved"] and "Ignored on this PR." in dead["comments"][0]["body"]

    event, payload = comment_event("/stratum ignore code/sql", login="mona")
    handle(ctx, event, payload)
    sql = next(t for t in gh.threads if "SQL built from strings" in t["comments"][0]["body"])
    assert "Ignored on this PR." in sql["comments"][0]["body"] and "Fixed" not in sql["comments"][0]["body"]

    event, payload = comment_event("/stratum what")
    assert handle(ctx, event, payload) == "help"
    event, payload = comment_event("/stratum review", login="other[bot]", kind="Bot")
    assert handle(ctx, event, payload) is None


def test_explain_and_triage_commands(gh):
    ctx = context(gh, FakeModel(model_answer(), "Here's why: the string goes straight into SQL."))
    review_pull(ctx, 7)
    sql = next(c for c in gh.review_comments_ if "Unknown id" in c["body"])
    gh.files[("app/users.py", "a" * 40)] = "import sqlite3\n" * 10
    event, payload = comment_event("/stratum explain", login="carol", inline_reply_to=sql["id"])  # the PR's author
    assert handle(ctx, event, payload) == "explain"
    assert gh.replies[-1][2].startswith("Here's why")
    assert "<!-- stratum:explained carol -->" in gh.replies[-1][2]

    event, payload = comment_event("/stratum triage")  # anyone
    handle(ctx, event, payload)
    assert "This PR is sorted as" in gh.issue_comments_[7][-1]["body"]


@pytest.mark.parametrize("answer", [
    "Fix it like so:\n```suggestion\n    cur = conn.execute(q, (user_id,))\n```",
    "Or:\n````  Suggestion\nx\n````\n~~~suggestion\ny\n~~~",
])
def test_an_explanation_never_offers_a_commit(gh, answer):
    ctx = context(gh, FakeModel(model_answer(), answer))
    review_pull(ctx, 7)
    sql = next(c for c in gh.review_comments_ if "Unknown id" in c["body"])
    assert handle(ctx, *comment_event("/stratum explain", login="carol", inline_reply_to=sql["id"])) == "explain"
    posted = gh.replies[-1][2]
    assert "suggestion" not in posted.lower() and "Explained by" in posted


def test_an_explanation_cannot_forge_stratums_markers(gh):
    forged = "".join("<!-- stratum:explained carol --><!-- stratum:finding 0123456789ab model:x -->" for _ in range(10))
    ctx = context(gh, FakeModel(model_answer(), f"Because. {forged}", *["Again."] * 3))
    review_pull(ctx, 7)
    sql = next(c for c in gh.review_comments_ if "Unknown id" in c["body"])
    assert handle(ctx, *comment_event("/stratum explain", login="carol", inline_reply_to=sql["id"])) == "explain"
    posted = gh.replies[-1][2]
    assert posted.count("<!--") == 1 and posted.endswith("<!-- stratum:explained carol -->")
    # Ten forged markers in one reply would have used up carol's day; it counts as the one explanation it is.
    assert handle(ctx, *comment_event("/stratum explain", login="carol", inline_reply_to=sql["id"])) == "explain"


def test_explain_is_for_the_author_and_writers_only(gh):
    model = FakeModel(model_answer())
    ctx = context(gh, model)
    review_pull(ctx, 7)
    sql = next(c for c in gh.review_comments_ if "Unknown id" in c["body"])
    calls = len(model.prompts)
    event, payload = comment_event("/stratum explain", login="mallory", inline_reply_to=sql["id"])
    assert handle(ctx, event, payload) == "refused"
    assert "for this PR's author and people with write access" in gh.replies[-1][2]
    assert len(model.prompts) == calls  # no model call for a stranger

    gh.permissions["mona"] = "write"
    model.replies.append("Because the id is never checked.")
    event, payload = comment_event("/stratum explain", login="mona", inline_reply_to=sql["id"])
    assert handle(ctx, event, payload) == "explain"


def test_explain_has_a_daily_limit_per_person_and_per_pr(gh):
    from stratum_open.github import commands

    ctx = context(gh, FakeModel(model_answer(), *["An explanation."] * 30))
    review_pull(ctx, 7)
    sql = next(c for c in gh.review_comments_ if "Unknown id" in c["body"])
    gh.files[("app/users.py", "a" * 40)] = "x = 1\n"
    for _ in range(commands.EXPLAIN_PER_PERSON):
        event, payload = comment_event("/stratum explain", login="carol", inline_reply_to=sql["id"])
        assert handle(ctx, event, payload) == "explain"
    event, payload = comment_event("/stratum explain", login="carol", inline_reply_to=sql["id"])
    assert handle(ctx, event, payload) == "refused"
    assert "Try again tomorrow" in gh.replies[-1][2]

    # Old explanations stop counting after a day.
    for comment in gh.review_comments_:
        if "stratum:explained" in comment["body"]:
            comment["created_at"] = "2026-01-01T00:00:00Z"
    event, payload = comment_event("/stratum explain", login="carol", inline_reply_to=sql["id"])
    assert handle(ctx, event, payload) == "explain"

    # A forged marker from someone else doesn't count against anyone.
    gh.review_comments_.extend({"id": 9000 + n, "body": "<!-- stratum:explained carol -->", "user": {"login": "mallory"},
                                "created_at": gh.now} for n in range(10))
    event, payload = comment_event("/stratum explain", login="carol", inline_reply_to=sql["id"])
    assert handle(ctx, event, payload) == "explain"


def test_record_command_without_history(gh):
    ctx = context(gh, None)
    event, payload = comment_event("/stratum record")
    handle(ctx, event, payload)
    assert "needs the repo's history" in gh.issue_comments_[7][-1]["body"]


def test_a_review_the_model_never_answered_is_tried_again_on_the_same_commit(gh):
    from stratum_open.model import Reply

    down = Reply("", "m", 0, error="couldn't reach the model: ConnectTimeout")
    review_pull(context(gh, FakeModel(down)), 7)
    assert gh.checks[-1]["conclusion"] == "neutral"
    state = publish.State.decode(gh.issue_comments_[7][0]["body"])
    assert (state.head, state.unread) == ("a" * 40, ["app/users.py"])

    # The same commit again (CI finishing, a reopen, a re-run), the model still down:
    # it was sent the file again, and the summary doesn't say it read it.
    still_down = FakeModel(down)
    review_pull(context(gh, still_down), 7)
    body = gh.issue_comments_[7][0]["body"]
    assert gh.checks[-1]["conclusion"] == "neutral" and len(still_down.prompts) == 1
    assert "The model didn't answer" in body and "Read again" not in body
    assert publish.State.decode(body).unread == ["app/users.py"]

    # The model is back: it reads the file it missed, and nothing is left unread.
    model = FakeModel(model_answer())
    outcome = review_pull(context(gh, model), 7)
    assert len(model.prompts) == 1 and "Unknown id" in {f.title for f in outcome.findings}
    body = gh.issue_comments_[7][0]["body"]
    assert "Read again the 1 file the last review couldn't read" in body
    assert publish.State.decode(body).unread == []

    # And a later run on the same commit has nothing left to read.
    model = FakeModel(model_answer())
    review_pull(context(gh, model), 7)
    assert model.prompts == [] and "No files changed since aaaaaaa" in gh.issue_comments_[7][0]["body"]


def test_after_a_failed_push_the_model_reads_the_files_it_missed(gh):
    from stratum_open.model import Reply

    review_pull(context(gh, FakeModel(model_answer())), 7)
    gh.move_head(7, "b" * 40, FIXED)
    gh.compare[("a" * 40, "b" * 40)] = ["app/users.py"]
    review_pull(context(gh, FakeModel(Reply("", "m", 0, error="couldn't reach the model: ConnectTimeout"))), 7)
    state = publish.State.decode(gh.issue_comments_[7][0]["body"])
    assert (state.head, state.unread) == ("b" * 40, ["app/users.py"])

    model = FakeModel(model_answer(findings=[]))
    review_pull(context(gh, model), 7)
    body = gh.issue_comments_[7][0]["body"]
    assert len(model.prompts) == 1 and "app/users.py" in model.prompts[0][1]
    assert "Read again the 1 file the last review couldn't read" in body
    assert publish.State.decode(body).unread == []


def test_a_cut_off_reply_is_read_again_on_the_same_commit(gh):
    cut = '{"summary": "Changes the user query.", "matches_description": "yes", "findings": ['
    review_pull(context(gh, FakeModel(cut)), 7)
    model = FakeModel(model_answer())
    review_pull(context(gh, model), 7)
    assert len(model.prompts) == 1
    assert "No files changed since" not in gh.issue_comments_[7][0]["body"]


PROSE = "Sorry, I can't answer that in JSON."  # the model answers, but never as asked: the same every time


def prose(n=10):
    return FakeModel(*[PROSE] * n)


def test_a_file_whose_answer_is_never_json_is_sent_twice_on_one_commit(gh):
    # #59: every event on a commit reviews it again, and paid for the same failed call each time.
    calls = []
    for _ in range(4):
        model = prose()
        review_pull(context(gh, model), 7)
        calls.append(len(model.prompts))
    assert calls == [1, 1, 0, 0]
    body = gh.issue_comments_[7][0]["body"]
    assert ("Stopped sending `app/users.py` to the model on this commit: its answer came back cut off or not the JSON asked "
            "for 2 times. Push a commit, or comment `/stratum review`, to try again.") in body
    assert "the next review reads it" not in body
    assert gh.checks[-1]["conclusion"] == "neutral"  # still not a complete review
    state = publish.State.decode(body)
    assert (state.unread, state.tries) == (["app/users.py"], [2])


def test_a_push_sends_a_stopped_file_again(gh):
    for _ in range(2):
        review_pull(context(gh, prose()), 7)
    gh.move_head(7, "b" * 40, DIFF)
    gh.compare[("a" * 40, "b" * 40)] = []  # the file didn't change, but the commit did
    model = FakeModel(model_answer())
    review_pull(context(gh, model), 7)
    assert len(model.prompts) == 1 and "app/users.py" in model.prompts[0][1]
    state = publish.State.decode(gh.issue_comments_[7][0]["body"])
    assert (state.unread, state.tries) == ([], [])


def test_the_review_command_sends_a_stopped_file_again(gh):
    for _ in range(2):
        review_pull(context(gh, prose()), 7)
    model = prose()
    gh.permissions["mona"] = "write"
    event, payload = comment_event("/stratum review", login="mona")
    assert handle(context(gh, model), event, payload) == "review"
    assert len(model.prompts) == 1
    # It starts the count again: one more review of the commit may send it, then it stops.
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).tries == [1]
    model = prose()
    review_pull(context(gh, model), 7)
    assert len(model.prompts) == 1
    model = prose()
    review_pull(context(gh, model), 7)
    assert model.prompts == []


def test_a_model_that_cant_be_reached_doesnt_use_up_the_tries(gh):
    from stratum_open.model import Reply

    for _ in range(3):
        review_pull(context(gh, FakeModel(Reply("", "m", 0, error="couldn't reach the model: ConnectTimeout"))), 7)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).tries == [0]
    model = prose()
    review_pull(context(gh, model), 7)  # the model is back, and answers in prose: the first try that counts
    assert len(model.prompts) == 1
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).tries == [1]


def test_the_tries_are_dropped_when_they_dont_match_the_unread_files():
    import base64

    def decode(data):
        return publish.State.decode(f"<!-- stratum:state {base64.b64encode(json.dumps(data).encode()).decode()} -->")

    assert decode({"unread": ["a.py", "b.py"], "tries": [2, 1]}).tries_by_path() == {"a.py": 2, "b.py": 1}
    assert decode({"unread": ["a.py", "b.py"], "tries": [2]}).tries_by_path() == {}
    assert decode({"unread": ["a.py"], "tries": [True]}).tries_by_path() == {}
    assert decode({"unread": ["a.py"]}).tries_by_path() == {}  # a summary from before the counts


SPLIT = parse_policy("review:\n  profile: small\n  context_tokens: 6000\n  max_chunks: 4\n")
DOWN = '{"bad": '  # not JSON and nothing to salvage: a failed call


def two_files():
    """Two new files too big to share a call, so each gets one of its own."""
    def new_file(stem):
        lines = "".join(f"+{stem}_value_{i:03} = compute({i})  # padding, so this file needs a call of its own\n" for i in range(80))
        return f"diff --git a/app/{stem}.py b/app/{stem}.py\nnew file mode 100644\n--- /dev/null\n+++ b/app/{stem}.py\n@@ -0,0 +1,80 @@\n{lines}"
    return new_file("alpha") + new_file("beta")


def test_when_one_call_of_two_fails_only_its_files_are_read_again(gh):
    gh.diff = two_files()
    first = review_pull(context(gh, FakeModel(model_answer(findings=[]), DOWN), policy=SPLIT), 7)
    assert first.review.packed.chunk_paths == [["app/alpha.py"], ["app/beta.py"]]
    assert gh.checks[-1]["conclusion"] == "neutral"
    state = publish.State.decode(gh.issue_comments_[7][0]["body"])
    assert (state.head, state.unread) == ("a" * 40, ["app/beta.py"])

    # The same commit again: one call, for the file the failed call held.
    model = FakeModel(model_answer(findings=[]))
    review_pull(context(gh, model, policy=SPLIT), 7)
    assert len(model.prompts) == 1
    assert "beta_value_000" in model.prompts[0][1] and "alpha_value_000" not in model.prompts[0][1]
    body = gh.issue_comments_[7][0]["body"]
    assert "Read again the 1 file the last review couldn't read" in body
    assert publish.State.decode(body).unread == [] and gh.checks[-1]["conclusion"] == "success"


def test_a_cut_off_reply_leaves_its_files_unread(gh):
    gh.diff = two_files()
    cut = '{"summary": "Adds values.", "matches_description": "yes", "findings": ['
    review_pull(context(gh, FakeModel(cut, model_answer(findings=[])), policy=SPLIT), 7)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).unread == ["app/alpha.py"]


def test_when_the_model_step_raises_every_file_it_was_given_is_unread(gh):
    class Raises(FakeModel):
        def complete(self, system, user, **kwargs):
            raise RuntimeError("the embedder's model broke")

    review_pull(context(gh, Raises()), 7)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).unread == ["app/users.py"]


def test_too_many_unread_files_keep_the_old_head(gh, monkeypatch):
    from stratum_open import runner

    monkeypatch.setattr(runner, "MAX_UNREAD_BYTES", 0)
    review_pull(context(gh, FakeModel(DOWN)), 7)
    state = publish.State.decode(gh.issue_comments_[7][0]["body"])
    assert (state.head, state.unread) == ("", [])  # so the next run reads the whole PR
    model = FakeModel(model_answer())
    review_pull(context(gh, model), 7)
    assert len(model.prompts) == 1


def test_a_file_level_finding_on_a_file_the_model_didnt_answer_for_isnt_called_fixed(gh):
    gh.diff = two_files()
    about_beta = [{"file": "app/beta.py", "severity": "medium", "category": "bug", "title": "No tests",
                   "detail": "Nothing exercises these values.", "fix": "Add a test."}]
    review_pull(context(gh, FakeModel(model_answer(findings=[]), model_answer(findings=about_beta)), policy=SPLIT), 7)
    assert "No tests" in gh.issue_comments_[7][0]["body"]

    # `/stratum review` reads everything again; beta's call fails, so its finding stays.
    review_pull(context(gh, FakeModel(model_answer(findings=[]), DOWN), policy=SPLIT), 7, full=True)
    body = gh.issue_comments_[7][0]["body"]
    assert "No tests" in body.split("<details>")[0]
    assert publish.State.decode(body).unread == ["app/beta.py"]


LOGO = "diff --git a/logo.png b/logo.png\nindex 1111111..2222222 100644\nBinary files a/logo.png and b/logo.png differ\n"


def test_a_file_that_is_never_sent_is_never_unread(gh):
    class Raises(FakeModel):
        def complete(self, system, user, **kwargs):
            raise RuntimeError("the embedder's model broke")

    gh.diff = DIFF + LOGO
    review_pull(context(gh, Raises()), 7)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).unread == ["app/users.py"]


def test_a_retry_with_nothing_left_to_send_doesnt_call_the_model_down(gh):
    review_pull(context(gh, FakeModel(DOWN)), 7)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).unread == ["app/users.py"]

    # The policy now keeps the file out of the model's view: there's nothing to send.
    model = FakeModel(model_answer())
    review_pull(context(gh, model, policy=parse_policy("review:\n  ignore_paths: ['app/**']\n")), 7)
    body = gh.issue_comments_[7][0]["body"]
    assert model.prompts == [] and "didn't answer" not in body and "Read again" not in body
    assert publish.State.decode(body).unread == []


def test_with_no_model_nothing_is_read_and_nothing_said_read_again(gh):
    review_pull(context(gh, FakeModel(DOWN)), 7)
    review_pull(context(gh, None), 7)
    body = gh.issue_comments_[7][0]["body"]
    assert "Read again" not in body and publish.State.decode(body).unread == ["app/users.py"]


def test_a_push_reviewed_with_no_model_is_read_by_the_next_with_one(gh):
    review_pull(context(gh, FakeModel(model_answer())), 7)
    gh.move_head(7, "b" * 40, FIXED)
    gh.compare[("a" * 40, "b" * 40)] = ["app/users.py"]
    review_pull(context(gh, None), 7)
    state = publish.State.decode(gh.issue_comments_[7][0]["body"])
    assert (state.head, state.unread) == ("b" * 40, ["app/users.py"])

    model = FakeModel(model_answer(findings=[]))
    review_pull(context(gh, model), 7)
    assert len(model.prompts) == 1 and "app/users.py" in model.prompts[0][1]
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).unread == []


def test_a_file_to_read_again_is_packed_before_new_changes(gh):
    gh.diff = two_files()
    review_pull(context(gh, FakeModel(model_answer(findings=[]), DOWN), policy=SPLIT), 7)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).unread == ["app/beta.py"]

    # A push changes alpha; with room for one call, beta (read again) takes it, and alpha
    # is left out as on any first read, past the size limit.
    gh.move_head(7, "b" * 40)
    gh.compare[("a" * 40, "b" * 40)] = ["app/alpha.py"]
    one_call = parse_policy("review:\n  profile: small\n  context_tokens: 6000\n  max_chunks: 1\n")
    outcome = review_pull(context(gh, FakeModel(model_answer(findings=[])), policy=one_call), 7)
    assert outcome.review.packed.reviewed == ["app/beta.py"] and outcome.review.packed.omitted == ["app/alpha.py"]
    body = gh.issue_comments_[7][0]["body"]
    assert "and the 1 file the last review couldn't read" in body and "past the review's size limit" in body
    assert publish.State.decode(body).unread == []


def small_and_big():
    """`app/small.py`, and a 600-line `app/big.py` too big for any one call."""
    big = "".join(f"+big_value_{i:03} = compute({i})  # padding, so this file is far too big for one call\n" for i in range(600))
    return ("diff --git a/app/small.py b/app/small.py\nnew file mode 100644\n--- /dev/null\n+++ b/app/small.py\n"
            "@@ -0,0 +1,1 @@\n+small_value = compute(1)\n"
            f"diff --git a/app/big.py b/app/big.py\nnew file mode 100644\n--- /dev/null\n+++ b/app/big.py\n@@ -0,0 +1,600 @@\n{big}")


@pytest.mark.parametrize("first", ["crash", "no model"])
def test_a_file_the_size_limit_never_sends_doesnt_stay_unread(gh, first):
    class Raises(FakeModel):
        def complete(self, system, user, **kwargs):
            raise RuntimeError("the embedder's model broke")

    gh.diff = small_and_big()
    skip = parse_policy("review:\n  profile: small\n  context_tokens: 6000\n  large_patch: skip\n")
    review_pull(context(gh, Raises() if first == "crash" else None, policy=skip), 7)
    assert set(publish.State.decode(gh.issue_comments_[7][0]["body"]).unread) == {"app/small.py", "app/big.py"}

    def healthy(**kwargs):
        outcome = review_pull(context(gh, FakeModel(model_answer(findings=[])), policy=skip), 7, **kwargs)
        assert "app/big.py" in outcome.review.packed.omitted if outcome.review else True
        assert publish.State.decode(gh.issue_comments_[7][0]["body"]).unread == []
        assert gh.checks[-1]["conclusion"] == "success"

    healthy()  # the same commit: small is read, big is left out as on a first read
    healthy()
    gh.move_head(7, "b" * 40)
    gh.compare[("a" * 40, "b" * 40)] = ["app/small.py", "app/big.py"]
    healthy()  # a push
    healthy(full=True)  # `/stratum review`


def test_an_ignore_makes_no_model_call_even_when_the_head_wasnt_kept(gh, monkeypatch):
    from stratum_open import runner

    monkeypatch.setattr(runner, "MAX_UNREAD_BYTES", 0)  # any unread list is too long: the old (empty) head is kept
    review_pull(context(gh, FakeModel(DOWN)), 7)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).head == ""

    gh.permissions["mona"] = "admin"
    model = FakeModel(model_answer())
    event, payload = comment_event("/stratum ignore code/sql", login="mona")
    assert handle(context(gh, model), event, payload) == "ignore"
    body = gh.issue_comments_[7][0]["body"]
    assert model.prompts == [] and publish.State.decode(body).head == ""
    assert "The model hasn't read 1 file yet" in body and gh.checks[-1]["conclusion"] == "neutral"


def test_a_full_review_with_no_model_leaves_what_was_read_read(gh):
    review_pull(context(gh, FakeModel(model_answer())), 7)
    review_pull(context(gh, None), 7, full=True)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).unread == []
    model = FakeModel(model_answer())
    review_pull(context(gh, model), 7)
    assert model.prompts == []

    # After a push, only what changed is left for the model.
    gh.move_head(7, "b" * 40, DIFF + two_files())
    gh.compare[("a" * 40, "b" * 40)] = ["app/alpha.py"]
    review_pull(context(gh, None), 7, full=True)
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).unread == ["app/alpha.py"]


def test_the_coverage_counts_only_the_files_the_model_read(gh):
    gh.diff = two_files()
    review_pull(context(gh, FakeModel(model_answer(findings=[]), DOWN), policy=SPLIT), 7)
    body = gh.issue_comments_[7][0]["body"]
    assert "Stratum sent the model 2 files in 2 calls; it read 1 of them in full." in body
    assert "The model read 2 files" not in body


def test_an_ignore_redraws_without_calling_the_model(gh):
    review_pull(context(gh, FakeModel(DOWN)), 7)
    gh.permissions["mona"] = "admin"
    model = FakeModel(model_answer())
    event, payload = comment_event("/stratum ignore code/sql", login="mona")
    assert handle(context(gh, model), event, payload) == "ignore"
    body = gh.issue_comments_[7][0]["body"]
    assert model.prompts == [] and publish.State.decode(body).unread == ["app/users.py"]
    assert "The model hasn't read 1 file yet" in body and gh.checks[-1]["conclusion"] == "neutral"


def test_a_cut_off_reply_never_passes_the_check(gh):
    cut = '{"summary": "Changes the user query.", "matches_description": "yes", "findings": ['
    outcome = review_pull(context(gh, FakeModel(cut)), 7)
    assert outcome.review.salvaged_calls == 1
    assert gh.checks[-1]["conclusion"] != "success"
    assert "were cut off; anything it found after the cut is missing" in gh.issue_comments_[7][0]["body"]


def test_only_findings_that_will_be_kept_are_named_to_the_model(gh):
    review_pull(context(gh, FakeModel(model_answer())), 7)
    # The push edits the Unknown-id line (still unfixed) and leaves the dead code as it was.
    edited = DIFF.replace('+    cur = conn.execute(f"SELECT * FROM users WHERE id = {user_id}")', '+    cur = conn.execute(f"SELECT * FROM users WHERE uid = {user_id}")')
    gh.move_head(7, "b" * 40, edited)
    gh.compare[("a" * 40, "b" * 40)] = ["app/users.py"]
    model = FakeModel(model_answer(findings=[]))
    review_pull(context(gh, model), 7)
    _, user = model.prompts[0]
    assert "Dead code" in user  # its code is unchanged: kept, so the model needn't repeat it
    assert "Unknown id" not in user  # its line changed: it isn't kept, so the model may report it again


def test_the_left_out_notice_stays_inside_its_budget():
    from stratum_open.review import LEFT_OUT_CHARS, _names

    paths = [f"services/{'x' * 140}/module_{i}.py" for i in range(21)]
    text = _names(paths)
    assert len(text) <= LEFT_OUT_CHARS + 20 and text.endswith("more")


@pytest.mark.parametrize(
    "policy_text, pull, action, expected",
    [
        ("", {"draft": False}, "opened", None),
        ("", {"draft": False}, "labeled", "Nothing to do for a labeled event."),
        ("", {"draft": True}, "opened", "Draft PR"),
        ("review:\n  skip_drafts: false\n", {"draft": True}, "opened", None),
        ("review:\n  events: [opened]\n", {}, "synchronize", "Nothing to do for a synchronize event."),
        ("review:\n  skip_bots: true\n", {"user": {"login": "dependabot[bot]", "type": "Bot"}}, "opened", "A PR by a bot"),
        ("review:\n  skip_authors: [Renovate]\n", {"user": {"login": "renovate"}}, "opened", "PRs by renovate"),
        ("review:\n  skip_labels: [wip]\n", {"labels": [{"name": "wip"}]}, "opened", "the label 'wip'"),
        ("review:\n  skip_branches: ['release/**']\n", {"head": {"ref": "release/1.2"}}, "opened", "PRs from release/1.2"),
    ],
)
def test_skip_reason(policy_text, pull, action, expected):
    from stratum_open.runner import skip_reason

    reason = skip_reason(pull, action, parse_policy(policy_text or None))
    assert (reason is None) if expected is None else (expected in reason)


def test_command_permissions_follow_the_policy(gh):
    ctx = context(gh, FakeModel(model_answer()), policy=parse_policy("commands:\n  triage: write\n  review: author\n"))
    review_pull(ctx, 7)
    event, payload = comment_event("/stratum triage", login="erin")
    assert handle(ctx, event, payload) == "refused"
    assert "only people with write access" in gh.issue_comments_[7][-1]["body"]
    ctx.model.replies.append(model_answer())
    event, payload = comment_event("/stratum review", login="carol")  # the PR's author, allowed by "author"
    assert handle(ctx, event, payload) == "review"


def test_the_profile_follows_the_model(gh):
    from stratum_open.profiles import ModelInfo

    class Described(FakeModel):
        def describe(self):
            return ModelInfo(billions=70, context=128_000, local=False)

    big = Described(model_answer())
    review_pull(context(gh, big), 7)
    system, user = big.prompts[0]
    assert "__new hunk__" in user  # large: split hunks
    assert big.context_tokens == 32768
    assert "Profile: `large` (70B parameters)." in gh.issue_comments_[7][0]["body"]


def test_the_summary_says_what_was_set_for_gpt_oss_and_why(gh):
    review_pull(context(gh, FakeModel(model_answer(), name="gpt-oss:20b")), 7)
    body = gh.issue_comments_[7][0]["body"]
    assert ("Set for `gpt-oss:20b`, since the policy file doesn't set them: `review.reasoning: low` (without it, the thinking uses up "
            "the output budget and most replies come back empty) and `review.verify: strict` (it halves the wrong comments and "
            "costs no real catches).") in body


def test_the_summary_says_nothing_of_model_settings_for_other_models(gh):
    review_pull(context(gh, FakeModel(model_answer(), name="qwen2.5-coder:7b")), 7)
    assert "Set for `" not in gh.issue_comments_[7][0]["body"]


# ── files moved into place ───────────────────────────────────────────────

PWN = """on: pull_request_target
permissions: write-all
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - run: echo "${{ github.event.pull_request.title }}" && make test
"""


def moved(old, new):
    return f"diff --git a/{old} b/{new}\nsimilarity index 100%\nrename from {old}\nrename to {new}\n"


def test_a_workflow_moved_into_place_is_checked_in_full():
    gh = FakeGitHub(diff=moved("docs/examples/ci.yml", ".github/workflows/ci.yml"), files={(".github/workflows/ci.yml", "a" * 40): PWN})
    outcome = review_pull(context(gh, None), 7)
    assert {"workflow/pwn-request", "workflow/write-all"} <= {f.rule for f in outcome.findings}
    assert gh.checks[-1]["conclusion"] == "failure"
    body = gh.issue_comments_[7][-1]["body"]
    assert "Checked in full, since they moved to where the checks do more: `.github/workflows/ci.yml`" in body
    assert gh.reviews == []  # no line of a move takes a comment: the findings are listed in the summary


def test_code_moved_out_of_the_tests_is_fetched_even_without_bandit(monkeypatch):
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: False)
    gh = FakeGitHub(diff=moved("tests/helper.py", "app/helper.py"), files={("app/helper.py", "a" * 40): "def f(request):\n    eval(request.args['x'])\n"})
    outcome = review_pull(context(gh, None), 7)
    assert "code/eval" in {f.rule for f in outcome.findings}


def test_a_moved_file_github_wont_give_is_named_in_the_review():
    outcome = review_pull(context(FakeGitHub(diff=moved("tests/helper.py", "app/helper.py")), None), 7)
    [finding] = [f for f in outcome.findings if f.rule == "checks/moved-unread"]
    assert "`app/helper.py`" in finding.detail


def test_a_moved_binary_file_is_left_alone():
    png = "\x89PNG\r\n\x1a\n\0\0\0\rIHDR password = 'hunter2hunter2'"
    gh = FakeGitHub(diff=moved("tests/fixtures/logo.png", "app/logo.png"), files={("app/logo.png", "a" * 40): png})
    outcome = review_pull(context(gh, None), 7)
    assert outcome.findings == []


def test_findings_on_a_moved_file_keep_their_lines_in_the_summary_and_the_check_run():
    gh = FakeGitHub(diff=moved("docs/examples/ci.yml", ".github/workflows/ci.yml"), files={(".github/workflows/ci.yml", "a" * 40): PWN})
    review_pull(context(gh, None), 7)
    assert "in `.github/workflows/ci.yml` line 9: pull_request_target runs the PR's own code" in gh.issue_comments_[7][-1]["body"]
    assert (".github/workflows/ci.yml", 9) in {(a["path"], a["start_line"]) for a in gh.checks[-1]["annotations"]}


def bandit_finding(path, line=1):
    return {"path": path, "test_id": "B301", "test_name": "pickle", "severity": "MEDIUM", "confidence": "HIGH",
            "text": "Pickle can run code.", "line": line, "lines": [line]}


def test_twenty_moves_dont_push_the_prs_own_code_out_of_bandit(monkeypatch):
    sent = []
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: True)
    monkeypatch.setattr("stratum_open.checks.bandit.run_bandit",
                        lambda sources: sent.append(set(sources)) or ([bandit_finding("app/load.py")] if "app/load.py" in sources else []))
    helper = "".join(f"x{i} = {i}\n" for i in range(80))
    diff = "".join(moved(f"tests/h{i}.py", f"src/h{i}.py") for i in range(20)) + (
        "diff --git a/app/load.py b/app/load.py\nnew file mode 100644\n--- /dev/null\n+++ b/app/load.py\n@@ -0,0 +1 @@\n+pickle.loads(data)\n")
    files = {**{(f"src/h{i}.py", "a" * 40): helper for i in range(20)}, ("app/load.py", "a" * 40): "pickle.loads(data)\n"}
    gh = FakeGitHub(diff=diff, files=files)
    outcome = review_pull(context(gh, None), 7)
    assert "bandit/B301" in {f.rule for f in outcome.findings}
    assert "app/load.py" in sent[0] and len(sent[0]) == 20
    assert "Not read by Bandit (1 file past its limit of 20, or without their text): `src/h19.py`" in gh.issue_comments_[7][-1]["body"]


def test_edited_tests_dont_push_the_code_out_of_bandits_files(monkeypatch):
    sent = []
    monkeypatch.setattr("stratum_open.checks.bandit.available", lambda: True)
    monkeypatch.setattr("stratum_open.checks.bandit.run_bandit", lambda sources: sent.append(set(sources)) or [])

    def added(path, lines):
        return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n"
                + "".join(f"+{line}\n" for line in lines))

    tests = {f"tests/test_t{i}.py": [f"t{j} = {j}" for j in range(5)] for i in range(25)}
    diff = "".join(added(p, lines) for p, lines in tests.items()) + added("app/a.py", ["x = 1"])
    files = {**{(p, "a" * 40): "\n".join(lines) + "\n" for p, lines in tests.items()}, ("app/a.py", "a" * 40): "x = 1\n"}
    review_pull(context(FakeGitHub(diff=diff, files=files), None), 7)
    assert sent == [{"app/a.py"}]


def test_a_moved_file_too_big_to_fetch_is_named_as_unread(monkeypatch):
    monkeypatch.setattr("stratum_open.runner.MAX_MOVED_BYTES", 10)
    gh = FakeGitHub(diff=moved("tests/helper.py", "app/helper.py"), files={("app/helper.py", "a" * 40): "def f(request):\n    eval(request.args['x'])\n"})
    outcome = review_pull(context(gh, None), 7)
    assert {f.rule for f in outcome.findings} == {"checks/moved-unread"}


def test_a_name_its_module_doesnt_define_is_reported_when_the_policy_asks(tmp_path):
    for path, text in {"app/__init__.py": "", "app/db.py": "def connect():\n    pass\n", "app/users.py": "import sqlite3\n"}.items():
        (tmp_path / path).parent.mkdir(exist_ok=True)
        (tmp_path / path).write_text(text)
    diff = ("diff --git a/app/users.py b/app/users.py\n--- a/app/users.py\n+++ b/app/users.py\n@@ -1 +1,2 @@\n import sqlite3\n"
            "+from app.db import connect, disconnect\n")
    on = parse_policy("checks:\n  unknown_names: true\n")

    def found(policy, **kwargs):
        gh = FakeGitHub(diff=diff)
        return gh, [f for f in review_pull(context(gh, None, policy, **kwargs), 7).findings if f.rule == "code/unknown-name"]

    gh, [finding] = found(on, checkout=str(tmp_path))
    assert (finding.path, finding.line_start, finding.severity, finding.title) == ("app/users.py", 2, "high", "`app/db.py` has no `disconnect`")
    [comment] = [c for review in gh.reviews for c in review["comments"]]
    assert comment["line"] == 2 and "`app/db.py` has no `disconnect`" in comment["body"]
    # Off by default, and nothing to read without a checkout.
    assert found(parse_policy(""), checkout=str(tmp_path))[1] == []
    assert found(on)[1] == []


def test_a_name_the_pr_removes_and_another_file_still_imports_is_reported_when_the_policy_asks(tmp_path):
    for path, text in {"app/__init__.py": "", "app/db.py": "import sqlite3\n\n\ndef connect():\n    pass\n", "app/users.py": "from app.db import connect\n"}.items():
        (tmp_path / path).parent.mkdir(exist_ok=True)
        (tmp_path / path).write_text(text)
    diff = ("diff --git a/app/db.py b/app/db.py\n--- a/app/db.py\n+++ b/app/db.py\n@@ -1,5 +1,5 @@\n import sqlite3\n \n \n"
            "-def connect():\n+def open_connection():\n     pass\n")
    on = parse_policy("checks:\n  removed_names: true\n")

    def found(policy, **kwargs):
        gh = FakeGitHub(diff=diff)
        return gh, [f for f in review_pull(context(gh, None, policy, **kwargs), 7).findings if f.rule == "code/removed-name"]

    gh, [finding] = found(on, checkout=str(tmp_path))
    assert (finding.path, finding.line_start, finding.severity) == ("app/db.py", 4, "high")
    assert finding.title == "`app/db.py` no longer has `connect`, which is still used"
    # The comment goes on the changed module, where the diff has a line for it, and names the file that breaks.
    [comment] = [c for review in gh.reviews for c in review["comments"]]
    assert (comment["path"], comment["line"]) == ("app/db.py", 4) and "`app/users.py:1` still imports it" in comment["body"]
    # Off by default, and nothing to read without a checkout.
    assert found(parse_policy(""), checkout=str(tmp_path))[1] == []
    assert found(on)[1] == []
