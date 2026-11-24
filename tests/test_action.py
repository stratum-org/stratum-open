import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from fakes import FakeGitHub, FakeModel
from test_runner import DIFF, model_answer

from stratum_open import action
from stratum_open.github import publish
from stratum_open.github.api import GitHubError
from stratum_open.github.commands import parse
from stratum_open.platform import CI
from stratum_open.policy import parse_policy
from stratum_open.runner import QUEUE_JOB, Context, review_pull


def run(tmp_path, monkeypatch, event_name, payload, gh, model=None):
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload))
    monkeypatch.setattr(action, "GitHub", lambda token, repo, api: gh)
    monkeypatch.setattr(action, "model_from_policy", lambda policy, env: model or FakeModel(model_answer()))
    env = {"GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "acme/widget",
           "STRATUM_GITHUB_TOKEN": "t", "STRATUM_BOT_LOGIN": "stratum[bot]"}
    return action.main(env)


def pr_payload(action_name="opened", draft=False):
    return {"action": action_name, "repository": {"default_branch": "main"}, "pull_request": {"number": 7, "draft": draft}}


def test_a_pr_event_reviews_the_pr(tmp_path, monkeypatch):
    gh = FakeGitHub(diff=DIFF)
    assert run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh) == 0
    assert gh.issue_comments_[7] and gh.checks


def test_drafts_and_other_actions_are_skipped(tmp_path, monkeypatch, capsys):
    gh = FakeGitHub(diff=DIFF)
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(draft=True), gh)
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload("labeled"), gh)
    assert gh.issue_comments_[7] == []
    out = capsys.readouterr().out
    assert "Draft PR" in out and "Nothing to do for a labeled event." in out


@pytest.mark.parametrize("policy, endpoint", [("review:\n  endpoint: off\n", ""), ("", "off")])
def test_with_the_endpoint_off_the_action_posts_the_checks_and_calls_no_model(tmp_path, monkeypatch, policy, endpoint):
    # Not run() and its stand-in model: model_from_policy itself has to build none. A model it built
    # would call localhost, where nothing answers here, and the summary would report a model error.
    gh = FakeGitHub(diff=DIFF)
    if policy:
        gh.files[(".stratum/policy.yml", "main")] = policy
    event = tmp_path / "event.json"
    event.write_text(json.dumps(pr_payload()))
    monkeypatch.setattr(action, "GitHub", lambda token, repo, api: gh)
    env = {"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "acme/widget",
           "STRATUM_GITHUB_TOKEN": "t", "STRATUM_BOT_LOGIN": "stratum[bot]", "STRATUM_MODEL_ENDPOINT": endpoint}
    assert action.main(env) == 0
    summary = gh.issue_comments_[7][-1]["body"]
    assert "Model: none configured; checks only." in summary
    assert "couldn't reach the model" not in summary and "model error" not in summary.lower()
    assert gh.checks


def test_comment_events_run_commands(tmp_path, monkeypatch):
    gh = FakeGitHub(diff=DIFF)
    payload = {"action": "created", "repository": {"default_branch": "main"}, "issue": {"number": 7, "pull_request": {}},
               "comment": {"id": 1, "body": "/stratum help", "user": {"login": "erin", "type": "User"}}}
    run(tmp_path, monkeypatch, "issue_comment", payload, gh)
    assert "Stratum commands" in gh.issue_comments_[7][-1]["body"]


def from_fork(gh, repo="mallory/widget"):
    gh.pulls[7]["head"]["repo"] = {"full_name": repo} if repo else None
    return gh


def inline_hints(gh):
    return [c["body"].rpartition("<sub>")[2] for c in gh.review_comments_]


@pytest.mark.parametrize("repo", ["mallory/widget", None])  # None: the fork was deleted
def test_on_a_pr_from_a_fork_the_inline_comments_name_their_fingerprint(tmp_path, monkeypatch, repo):
    """A reply can't reach the Action on a PR from a fork (#124), so each
    inline comment names the comment on the PR that does what the reply would."""
    gh = from_fork(FakeGitHub(diff=DIFF), repo)
    assert run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh) == 0
    hints = inline_hints(gh)
    assert hints and all(publish.HINT not in hint for hint in hints)
    for comment, hint in zip(gh.review_comments_, hints):
        fp = publish._FINDING.search(comment["body"]).group(1)
        assert publish.FINGERPRINT_HINT.format(fp=fp) in hint

    # What the hint says to do works: a comment on the PR, which runs with the workflow's token.
    gh.permissions["mona"] = "write"
    fp = publish._FINDING.search(gh.review_comments_[0]["body"]).group(1)
    payload = {"action": "created", "repository": {"default_branch": "main"}, "issue": {"number": 7, "pull_request": {}},
               "comment": {"id": 2, "body": f"/stratum ignore {fp}", "user": {"login": "mona", "type": "User"}}}
    assert run(tmp_path, monkeypatch, "issue_comment", payload, gh) == 0
    assert fp in publish.State.decode(gh.issue_comments_[7][0]["body"]).ignored


@pytest.mark.parametrize("fork", [False, True])
def test_a_reply_is_still_the_hint_where_it_can_be_read(tmp_path, monkeypatch, fork):
    """A PR from the repo itself, with the Action; and the App, which reads a reply on any PR."""
    gh = from_fork(FakeGitHub(diff=DIFF), "mallory/widget" if fork else "Acme/Widget")
    if fork:
        review_pull(Context(gh=gh, policy=parse_policy(None), model=FakeModel(model_answer()), bot="stratum[bot]",
                            log=lambda line: None, feedback=True), 7)
    else:
        run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    assert gh.review_comments_ and all(publish.HINT in hint for hint in inline_hints(gh))


def inline_reply(body, root_id, repo="mallory/widget", login="mona"):
    return {"action": "created", "repository": {"default_branch": "main", "full_name": "acme/widget"},
            "pull_request": {"number": 7, "head": {"repo": {"full_name": repo}}},
            "comment": {"id": 3, "body": body, "in_reply_to_id": root_id, "user": {"login": login, "type": "User"}}}


class Recording(FakeGitHub):
    """Refuses every write, as GitHub does to a read-only token, and keeps the attempts."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.writes: list[str] = []

    def refuse(self, name):
        def write(*args, **kwargs):
            self.writes.append(name)
            raise GitHubError(403, "Resource not accessible by integration")
        return write


@pytest.mark.parametrize("body, instead", [
    ("/stratum ignore", "`/stratum ignore {fp}`"),
    ("Not this one.\n/stratum explain", "`/stratum explain {fp}`"),
    ("/stratum review", "the same command"),
])
def test_a_reply_on_a_pr_from_a_fork_fails_the_run_and_says_what_to_use(tmp_path, monkeypatch, capsys, body, instead):
    """#124: nothing can be written, so nothing is tried; the job's error line says why and what works."""
    gh = Recording(diff=DIFF, permissions={"mona": "write"})
    from_fork(gh)
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    root = gh.review_comments_[0]
    fp = publish._FINDING.search(root["body"]).group(1)
    summary = gh.issue_comments_[7][0]["body"]
    capsys.readouterr()
    for name in ("react", "reply", "comment", "edit_comment", "edit_review_comment", "check_run", "create_review", "resolve_thread"):
        monkeypatch.setattr(gh, name, gh.refuse(name))

    assert run(tmp_path, monkeypatch, "pull_request_review_comment", inline_reply(body, root["id"]), gh) == 1
    out = capsys.readouterr().out
    [error] = [line for line in out.splitlines() if line.startswith("::error::")]
    assert "a reply in an inline thread on a PR from a fork" in error and instead.format(fp=fp) in error
    assert "as a comment on the PR instead" in error and "403" not in out
    assert gh.writes == [] and gh.issue_comments_[7][0]["body"] == summary


def test_a_reply_that_isnt_a_command_or_is_on_the_repos_own_pr_runs_as_before(tmp_path, monkeypatch, capsys):
    gh = FakeGitHub(diff=DIFF, permissions={"mona": "write"})
    from_fork(gh, "acme/widget")
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    root = gh.review_comments_[0]
    assert run(tmp_path, monkeypatch, "pull_request_review_comment", inline_reply("/stratum ignore", root["id"], "acme/widget"), gh) == 0
    assert publish.State.decode(gh.issue_comments_[7][0]["body"]).ignored

    from_fork(gh)
    capsys.readouterr()
    assert run(tmp_path, monkeypatch, "pull_request_review_comment", inline_reply("Thanks, see /stratum help.", root["id"]), gh) == 0
    assert "::error::" not in capsys.readouterr().out


def test_schedule_builds_the_queue(tmp_path, monkeypatch):
    gh = FakeGitHub(diff=DIFF)
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    run(tmp_path, monkeypatch, "schedule", {"repository": {"default_branch": "main"}}, gh)
    [queue] = gh.issues
    assert queue["title"] == "Stratum triage queue" and "#7 Add a thing" in queue["body"]


QUEUE_OFF = "triage:\n  queue: false\n"


def run_queue(tmp_path, monkeypatch, payload, gh, event_name="pull_request_target", waits=None, extra=None):
    """`stratum-open action --queue`, the workflow's queue job. `waits` gets the seconds it waited."""
    event = tmp_path / "queue-event.json"
    event.write_text(json.dumps(payload))
    monkeypatch.setattr(action, "GitHub", lambda token, repo, api: gh)
    monkeypatch.setattr(action.time, "sleep", (waits if waits is not None else []).append)
    return action.queue({"GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "acme/widget",
                         "STRATUM_GITHUB_TOKEN": "t", "STRATUM_BOT_LOGIN": "stratum[bot]", **(extra or {})})


@pytest.mark.parametrize("event_name, extra, waited", [
    ("pull_request_target", {}, [60]),
    ("issue_comment", {}, [60]),
    ("workflow_run", {}, [60]),
    ("pull_request_target", {"STRATUM_QUEUE_WAIT": "5"}, [5]),
    ("schedule", {}, []),  # no burst to wait for
    ("workflow_dispatch", {}, []),
])
def test_the_queue_job_waits_for_a_newer_event_before_it_makes_a_call(tmp_path, monkeypatch, event_name, extra, waited):
    """A newer event's rebuild cancels this one while it waits, so a burst of events reads the open PRs once."""
    calls = []

    class Counting(FakeGitHub):
        def open_pulls_with_comments(self, limit):
            calls.append(list(waits))  # what had been waited by the first read
            return super().open_pulls_with_comments(limit)

    gh, waits = Counting(diff=DIFF, permissions={"erin": "write"}), []
    payload = {**pr_payload(), "issue": {"number": 7, "pull_request": {}}, "workflow_run": {"pull_requests": [{"number": 7}]},
               "comment": {"id": 1, "body": "/stratum review", "user": {"login": "erin", "type": "User"}}}
    assert run_queue(tmp_path, monkeypatch, payload, gh, event_name, waits, extra) == 0
    assert waits == waited and calls == [waited] and gh.issues


@pytest.mark.parametrize("body, rebuilt", [
    ("/stratum review", True),
    ("/Stratum rereview", True),
    ("/stratum ignore code/sql", True),
    ("Not this one.\n/stratum ignore", True),
    ("/stratum help", False),
    ("/stratum explain 0123456789ab", False),
    ("/stratum record", False),
    ("thanks!", False),
    ("Reply `/stratum ignore` to hide it", False),  # Stratum's own footer: not a command
])
def test_a_comment_rebuilds_the_queue_only_when_its_command_can_change_a_label(tmp_path, monkeypatch, capsys, body, rebuilt):
    gh, waits = FakeGitHub(diff=DIFF, permissions={"erin": "write"}), []
    for event_name in ("issue_comment", "pull_request_review_comment"):
        payload = comment_event(body)
        assert run_queue(tmp_path, monkeypatch, payload, gh, event_name, waits) == 0
        if rebuilt:
            assert [i["title"] for i in gh.issues] == ["Stratum triage queue"]
        else:  # before any wait or API call
            assert gh.issues == [] and waits == [] and "can't change a PR's label" in capsys.readouterr().out
    assert waits == ([60, 60] if rebuilt else [])


def comment_event(body, login="erin", user_type="User", issue=None):
    """A new comment, as either comment event has it: on PR 7's conversation, or inline on PR 7."""
    return {"action": "created", "repository": {"default_branch": "main"}, "pull_request": {"number": 7},
            "issue": issue if issue is not None else {"number": 7, "pull_request": {}},
            "comment": {"id": 1, "body": body, "user": {"login": login, "type": user_type}}}


@pytest.mark.parametrize("why, payload, events", [
    ("no write access", comment_event("/stratum review", login="mallory"), ("issue_comment", "pull_request_review_comment")),
    ("no write access", comment_event("/stratum ignore code/sql", login="mallory"), ("issue_comment", "pull_request_review_comment")),
    ("a plain issue", comment_event("/stratum review", issue={"number": 3}), ("issue_comment",)),
    ("a bot", comment_event("/stratum review", login="renovate", user_type="Bot"), ("issue_comment", "pull_request_review_comment")),
    ("a bot", comment_event("/stratum review", login="helper[bot]"), ("issue_comment", "pull_request_review_comment")),
    ("Stratum itself", comment_event("/stratum review", login="stratum[bot]"), ("issue_comment",)),
    ("a deleted account", {**comment_event("/stratum review"), "comment": {"id": 1, "body": "/stratum review", "user": None}},
     ("issue_comment",)),
])
def test_a_command_that_never_ran_leaves_the_queue_alone(tmp_path, monkeypatch, capsys, why, payload, events):
    """The review job refuses or drops these, but succeeds, so the queue job starts: it rebuilds nothing, and doesn't wait."""
    gh, waits = FakeGitHub(diff=DIFF, permissions={"erin": "write", "mallory": "read"}), []
    for event_name in events:
        assert run_queue(tmp_path, monkeypatch, payload, gh, event_name, waits) == 0, why
        assert gh.issues == [] and waits == [], why
        assert "the triage queue stays as it is" in capsys.readouterr().out, why


def test_who_may_rebuild_follows_the_policys_commands(tmp_path, monkeypatch):
    """`commands.review: anyone` lets anyone's `/stratum review` run, and so rebuild."""
    gh, waits = FakeGitHub(diff=DIFF, files={(".stratum/policy.yml", "main"): "commands:\n  review: anyone\n"}), []
    assert run_queue(tmp_path, monkeypatch, comment_event("/stratum review", login="mallory"), gh, "issue_comment", waits) == 0
    assert [i["title"] for i in gh.issues] == ["Stratum triage queue"] and waits == [60]
    gh.issues.clear()
    # The PR's author, under `author`.
    gh.files[(".stratum/policy.yml", "main")] = "commands:\n  ignore: author\n"
    assert run_queue(tmp_path, monkeypatch, comment_event("/stratum ignore", login="carol"), gh, "issue_comment", waits) == 0
    assert gh.issues and waits == [60, 60]


def test_a_ci_run_with_no_pr_leaves_the_queue_alone(tmp_path, monkeypatch, capsys):
    """A push to main, which GitHub sends with no PRs, or CI on a fork's commit that no open PR has: nothing was re-sorted."""
    gh, waits = on_a_fork(FakeGitHub(diff=DIFF)), []
    for run_payload in ({"pull_requests": []}, {}, {**fork_ci(gh)["workflow_run"], "event": "push"},
                        {**fork_ci(gh)["workflow_run"], "head_repository": {"full_name": "eve/widget"}}):
        payload = {"repository": {"default_branch": "main"}, "workflow_run": run_payload}
        assert run_queue(tmp_path, monkeypatch, payload, gh, "workflow_run", waits) == 0
        assert gh.issues == [] and waits == [] and "re-sorted no PR" in capsys.readouterr().out


def test_ci_on_a_pr_from_a_fork_is_followed_by_the_queue(tmp_path, monkeypatch, capsys):
    """The review job re-sorts the fork's PR by the run's commit; the queue job finds it the same way, and rebuilds with its new label."""
    gh, waits = on_a_fork(FakeGitHub(diff=DIFF, ci=CI.HELD)), []
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    run_queue(tmp_path, monkeypatch, pr_payload(), gh)
    assert HELD in gh.issues[0]["body"]
    gh.ci = CI.PASSED
    assert run(tmp_path, monkeypatch, "workflow_run", fork_ci(gh), gh) == 0
    assert HELD in gh.issues[0]["body"]  # the review job leaves the queue to the queue job
    assert run_queue(tmp_path, monkeypatch, fork_ci(gh), gh, "workflow_run", waits) == 0
    assert HELD not in gh.issues[0]["body"] and waits == [60] and "Triage queue updated." in capsys.readouterr().out


def test_a_label_changing_command_is_followed_by_the_queue(tmp_path, monkeypatch):
    """The review job runs `/stratum ignore code/sql`, which re-sorts the PR; the queue job then rebuilds with its new label."""
    gh = FakeGitHub(diff=DIFF)
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    run_queue(tmp_path, monkeypatch, pr_payload(), gh)
    assert "### triage: needs work (1)" in gh.issues[0]["body"] and "SQL built from strings" in gh.issues[0]["body"]
    gh.permissions["mona"] = "write"
    payload = {"action": "created", "repository": {"default_branch": "main"}, "issue": {"number": 7, "pull_request": {}},
               "comment": {"id": 2, "body": "/stratum ignore code/sql", "user": {"login": "mona", "type": "User"}}}
    assert run(tmp_path, monkeypatch, "issue_comment", payload, gh) == 0
    assert "SQL built from strings" in gh.issues[0]["body"]  # the review job leaves the queue to the queue job
    assert run_queue(tmp_path, monkeypatch, payload, gh, "issue_comment") == 0
    assert "SQL built from strings" not in gh.issues[0]["body"]


def test_a_pr_turned_back_into_a_draft_leaves_the_queue_without_the_model(tmp_path, monkeypatch):
    gh = FakeGitHub(diff=DIFF)
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    run_queue(tmp_path, monkeypatch, pr_payload(), gh)
    assert "#7 Add a thing" in gh.issues[0]["body"]

    event = tmp_path / "draft.json"
    event.write_text(json.dumps(pr_payload("converted_to_draft", draft=True)))
    assert action.needs_model({"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(event)}) is False
    gh.pulls[7]["draft"] = True
    model, comments = FakeModel(), list(gh.issue_comments_[7])
    assert run(tmp_path, monkeypatch, "pull_request_target", pr_payload("converted_to_draft", draft=True), gh, model) == 0
    assert model.prompts == [] and gh.issue_comments_[7] == comments
    assert run_queue(tmp_path, monkeypatch, pr_payload("converted_to_draft", draft=True), gh) == 0
    assert "#7" not in gh.issues[0]["body"]


@pytest.mark.parametrize("policy, rebuilt", [(None, True), (QUEUE_OFF, False)])
def test_the_queue_job_rebuilds_the_queue_after_a_review_when_the_queue_is_on(tmp_path, monkeypatch, capsys, policy, rebuilt):
    gh = FakeGitHub(diff=DIFF)
    if policy:
        gh.files[(".stratum/policy.yml", "main")] = policy
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    assert gh.issues == []  # the review job leaves the queue to the queue job
    assert run_queue(tmp_path, monkeypatch, pr_payload(), gh) == 0
    if rebuilt:
        [queue] = gh.issues
        assert "#7 Add a thing" in queue["body"] and "rebuilt on a schedule" not in queue["body"]
    else:
        assert gh.issues == [] and "triage.queue is off" in capsys.readouterr().out


def test_the_daily_run_leaves_the_queue_to_the_queue_job_when_the_workflow_has_one(tmp_path, monkeypatch, capsys):
    """`triage-queue: false`: the review job's daily run would race the queue job's rebuilds, outside their group."""
    gh = FakeGitHub(diff=DIFF)
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    monkeypatch.setattr(action, "update_queue", lambda ctx, **kwargs: pytest.fail("the review job rebuilt the queue"))
    event = tmp_path / "schedule.json"
    event.write_text(json.dumps({"repository": {"default_branch": "main"}}))
    env = {"GITHUB_EVENT_NAME": "schedule", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "acme/widget",
           "STRATUM_GITHUB_TOKEN": "t", "STRATUM_BOT_LOGIN": "stratum[bot]", "STRATUM_TRIAGE_QUEUE": "false"}
    assert action.main(env) == 0
    assert "queue job rebuilds the triage queue" in capsys.readouterr().out
    monkeypatch.undo()
    schedule = {"repository": {"default_branch": "main"}}
    assert run_queue(tmp_path, monkeypatch, schedule, gh) == 0  # the queue job's own run of the same event
    assert "#7 Add a thing" in gh.issues[0]["body"]


def test_the_daily_run_leaves_the_queue_alone_when_it_is_off(tmp_path, monkeypatch, capsys):
    gh = FakeGitHub(diff=DIFF, files={(".stratum/policy.yml", "main"): QUEUE_OFF})
    assert run(tmp_path, monkeypatch, "schedule", {"repository": {"default_branch": "main"}}, gh) == 0
    assert gh.issues == [] and "triage.queue is off" in capsys.readouterr().out


def test_a_queue_failure_is_a_warning_and_leaves_the_prs_check_alone(tmp_path, monkeypatch, capsys):
    gh = FakeGitHub(diff=DIFF)
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    checks = list(gh.checks)

    def refuse(ctx, **kwargs):
        raise GitHubError(502, "Bad gateway")

    monkeypatch.setattr(action, "update_queue", refuse)
    assert run_queue(tmp_path, monkeypatch, pr_payload(), gh) == 0
    assert "::warning::Stratum couldn't rebuild the triage queue" in capsys.readouterr().out
    assert gh.checks == checks and gh.issues == []


def test_a_pr_closing_rebuilds_the_queue_without_the_model(tmp_path, monkeypatch):
    gh = FakeGitHub(diff=DIFF)
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    run_queue(tmp_path, monkeypatch, pr_payload(), gh)
    assert "#7 Add a thing" in gh.issues[0]["body"]

    closed = tmp_path / "closed.json"
    closed.write_text(json.dumps(pr_payload("closed")))
    assert action.needs_model({"GITHUB_EVENT_NAME": "pull_request_target", "GITHUB_EVENT_PATH": str(closed)}) is False
    model = FakeModel()
    del gh.pulls[7]  # merged: no longer open
    comments = list(gh.issue_comments_[7])
    assert run(tmp_path, monkeypatch, "pull_request_target", pr_payload("closed"), gh, model) == 0
    assert model.prompts == [] and gh.issue_comments_[7] == comments
    assert run_queue(tmp_path, monkeypatch, pr_payload("closed"), gh) == 0
    [queue] = gh.issues
    assert "#7" not in queue["body"]


def workflow(path: str, *, uncomment: bool = False) -> dict:
    text = (Path(__file__).parent.parent / path).read_text(encoding="utf-8")
    if uncomment:
        text = uncommented(text)
    return yaml.safe_load(text)


def uncommented(example: str) -> str:
    """examples/stratum.yml as the release after 0.3.0 will have it: `triage-queue: false`
    and the queue job, at the end of the file, uncommented together."""
    off, on = "          # triage-queue: false\n", "          triage-queue: false\n"
    assert example.count(off) == 1
    before, job = example.replace(off, on).split("\n  # queue:\n")
    # What an editor's "uncomment" does to "  # " at the job's indent.
    assert all(line.startswith("  # ") or line == "  #" for line in job.splitlines())  # the job runs to the end of the file
    lines = ["  queue:"] + ["  " + line[4:] for line in job.splitlines()]
    return before + "\n" + "\n".join(lines) + "\n"


def test_the_example_keeps_the_queue_job_and_triage_queue_commented_until_its_pin_has_them():
    """`stratum-org/stratum-open/queue` isn't in v0.3.0, and `triage-queue: false` without the job stops the daily rebuild."""
    shipped = workflow("examples/stratum.yml")
    assert list(shipped["jobs"]) == ["stratum"]
    [step] = [step for step in shipped["jobs"]["stratum"]["steps"] if "ollama-model" in (step.get("with") or {})]
    assert "triage-queue" not in step["with"] and step["uses"] == "stratum-org/stratum-open@v0.3.0"
    later = workflow("examples/stratum.yml", uncomment=True)
    [step] = [step for step in later["jobs"]["stratum"]["steps"] if "ollama-model" in (step.get("with") or {})]
    # Uncommented, the queue job is pinned to the review job's release.
    assert later["jobs"]["queue"]["steps"][-1]["uses"] == step["uses"].replace("stratum-open@", "stratum-open/queue@")


@pytest.mark.parametrize("path, uses", [("examples/stratum.yml", "stratum-org/stratum-open/queue@"), (".github/workflows/stratum.yml", "./queue")])
def test_the_workflow_rebuilds_the_queue_in_its_own_job_after_the_review(path, uses):
    flow = workflow(path, uncomment=path.startswith("examples/"))
    on = flow.get("on", flow.get(True))  # YAML 1.1 reads a bare `on` as true
    assert {"closed", "converted_to_draft"} <= set(on["pull_request_target"]["types"]) and "schedule" in on  # the daily run stays
    jobs = flow["jobs"]
    # A PR closing or turning back into a draft starts no review, so no runner and no model for it.
    assert "github.event.action != 'closed'" in jobs["stratum"]["if"]
    assert "github.event.action != 'converted_to_draft'" in jobs["stratum"]["if"]
    # The review job still runs on the schedule, which keeps Ollama in the cache, but leaves the queue to the queue job.
    assert "github.event_name == 'schedule'" in jobs["stratum"]["if"]
    [stratum_step] = [step for step in jobs["stratum"]["steps"] if "ollama-model" in (step.get("with") or {})]
    assert stratum_step["with"]["triage-queue"] is False
    queue = jobs["queue"]
    assert queue["name"] == QUEUE_JOB  # the name its check runs have, which a PR's CI leaves out
    assert queue["needs"] == "stratum"  # after the review, so the queue has its label
    # Every rebuild, the daily and manual ones too, runs in this job's group, so no two overlap.
    assert queue["if"].startswith("!cancelled()")
    for event in ("pull_request_target", "schedule", "workflow_dispatch"):
        assert starts_the_queue(queue["if"], event, "success") and starts_the_queue(queue["if"], event, "failure"), event
    assert starts_the_queue(queue["if"], "pull_request_target", "skipped")  # closed and converted_to_draft skip the review
    assert not starts_the_queue(queue["if"], "pull_request_target", "success", cancelled=True)
    # A comment or a CI result rebuilds it only when the review job ran: a comment without a
    # `/stratum ` command skips that job (the same filter as test_the_workflow_starts_a_job_...).
    for event in ("issue_comment", "pull_request_review_comment", "workflow_run"):
        assert starts_the_queue(queue["if"], event, "success"), event
        assert not starts_the_queue(queue["if"], event, "skipped"), event
        assert not starts_the_queue(queue["if"], event, "failure"), event
    assert not starts_the_queue(queue["if"], "push", "success")
    # One group for the repo: a burst of PRs cancels the older rebuilds rather than queueing one each.
    assert queue["concurrency"] == {"group": "stratum-queue-${{ github.repository }}", "cancel-in-progress": True}
    assert queue["permissions"] == {"contents": "read", "pull-requests": "read", "issues": "write"}
    assert queue["steps"][-1]["uses"].startswith(uses) and "with" not in queue["steps"][-1]
    assert all("ref" not in (step.get("with") or {}) or "default_branch" in step["with"]["ref"] for step in queue["steps"])


def starts_the_queue(condition: str, event_name: str, stratum: str, *, cancelled: bool = False) -> bool:
    """The queue job's `if:`, read the way GitHub reads it, for one event and the stratum job's result."""
    names = {"github.event_name": repr(event_name), "needs.stratum.result": repr(stratum), "!cancelled()": repr(not cancelled)}
    text = condition
    for name, value in names.items():
        text = text.replace(name, value)
    text = text.replace("&&", " and ").replace("||", " or ")
    assert set(re.findall(r"[a-z_.]+\(", text)) == set(), text  # no other function it would need to know
    return bool(eval(text, {"__builtins__": {}}))  # noqa: S307 (the workflow file's own expression)


def test_the_queue_action_runs_the_queue_mode_only():
    steps = workflow("queue/action.yml")["runs"]["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    assert runs[-1] == "stratum-open action --queue" and not any("ollama" in run.lower() for run in runs)


@pytest.mark.parametrize(
    "event_name, body, expected",
    [
        ("pull_request_target", None, True),
        ("schedule", None, True),
        ("issue_comment", "/stratum record", False),
        ("issue_comment", "/stratum  rereview", True),
        ("pull_request_review_comment", "/Stratum Ignore", True),
        ("issue_comment", "@stratum review", False),
        ("issue_comment", "/stratum triage", False),
        ("issue_comment", "/stratum review", True),
        ("pull_request_review_comment", "/Stratum explain", True),
        ("issue_comment", "/stratum ask why is this here?", True),
        ("pull_request_review_comment", "/stratum ask\nwhy?", True),
        ("issue_comment", "thanks!", False),
    ],
)
def test_needs_model_asks_the_command_parser(tmp_path, event_name, body, expected):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"comment": {"body": body}} if body is not None else {}))
    assert action.needs_model({"GITHUB_EVENT_NAME": event_name, "GITHUB_EVENT_PATH": str(event)}) is expected


def comment_filter(path: str) -> list[str]:
    """What the workflow's job filter looks for in a comment's body."""
    text = (Path(__file__).parent.parent / path).read_text(encoding="utf-8")
    return re.findall(r"contains\(github\.event\.comment\.body, '([^']*)'\)", text)


def starts_the_job(needles: list[str], body: str) -> bool:
    return any(needle.lower() in body.lower() for needle in needles)  # GitHub's contains() ignores case


@pytest.mark.parametrize("path, needles", [("examples/stratum.yml", ["/stratum "]), (".github/workflows/stratum.yml", ["/stratum "])])
def test_the_workflow_starts_a_job_for_every_comment_the_parser_reads_as_a_command(path, needles):
    assert comment_filter(path) == needles
    commands = ["/stratum review", "/Stratum REVIEW", "thanks!\n/stratum review", "/stratum review\n", "ok\r\n/stratum  ignore code/sql\r\n",
                "  /stratum record", "/stratum ask why is this here?", "/stratum ask\nwhy?"]
    for body in commands:
        assert parse(body) is not None and starts_the_job(needles, body), body
    # A slash command needs the space after it, the same text the filter looks for.
    for body in ["/stratum\treview", "/stratum\nreview"]:
        assert parse(body) is None and not starts_the_job(needles, body), body
    for body in ["@stratum review", "@Stratum\ttriage"]:
        assert parse(body) is None and starts_the_job(needles, body) is ("@stratum" in needles), body
    # Links to the project don't start a job.
    for body in ["Fixes https://github.com/stratum-org/stratum-open/issues/53", "/stratum-org/stratum-open", "see stratum-org/stratum-open#52"]:
        assert parse(body) is None and not starts_the_job(needles, body), body


def test_the_example_workflow_runs_gpt_oss_and_this_repo_reviews_itself_with_it():
    # gpt-oss gets review.reasoning: low and review.verify: strict from the engine, so the workflow names only the model.
    root = Path(__file__).parent.parent
    for path in ("examples/stratum.yml", ".github/workflows/stratum.yml"):
        models = re.findall(r"^\s*ollama-model:\s*(\S+)\s*$", (root / path).read_text(encoding="utf-8"), re.MULTILINE)
        assert models == ["gpt-oss:20b"], path


@pytest.mark.parametrize("fork", [False, True])
def test_ci_finishing_respects_the_skip_settings(tmp_path, monkeypatch, capsys, fork):
    from stratum_open import action as action_module

    gh = on_a_fork(FakeGitHub(diff=DIFF, author="renovate[bot]"))
    gh.files[(".stratum/policy.yml", "main")] = "review:\n  skip_bots: true\n"
    reviewed = []
    monkeypatch.setattr(action_module, "review_pull", lambda ctx, number: reviewed.append(number))
    payload = fork_ci(gh) if fork else {"repository": {"default_branch": "main"}, "workflow_run": {"pull_requests": [{"number": 7}]}}
    run(tmp_path, monkeypatch, "workflow_run", payload, gh)
    assert reviewed == [] and "review.skip_bots is on" in capsys.readouterr().out


def on_a_fork(gh, number=7, repo="mallory/widget", branch="patch-1"):
    gh.pulls[number]["head"].update(repo={"full_name": repo}, ref=branch)
    return gh


def fork_ci(gh, number=7, **changes):
    """CI finishing on PR `number` from a fork, the way GitHub sends it: no `pull_requests`, and the run's commit, fork and branch."""
    head = gh.pulls[number]["head"]
    run = {"event": "pull_request", "status": "completed", "conclusion": "success", "pull_requests": [],
           "head_sha": head["sha"], "head_branch": head["ref"], "head_repository": {"full_name": head["repo"]["full_name"]}, **changes}
    return {"action": "completed", "repository": {"default_branch": "main"}, "workflow_run": run}


HELD = "CI hasn't run: a maintainer needs to approve the workflows."


def summary_of(gh, number=7):
    return publish.find_summary(gh.issue_comments_[number], "stratum[bot]")["body"]


@pytest.mark.parametrize("event", ["pull_request", "pull_request_target"])
def test_ci_finishing_on_a_pr_from_a_fork_re_sorts_it_by_the_runs_commit(tmp_path, monkeypatch, capsys, event):
    """#201: the PR from a first-time contributor's fork was sorted while its CI was held; the workflows ran and passed."""
    gh = on_a_fork(FakeGitHub(diff=DIFF, ci=CI.HELD))
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    assert HELD in summary_of(gh)
    gh.ci = CI.PASSED
    model = FakeModel()
    capsys.readouterr()
    assert run(tmp_path, monkeypatch, "workflow_run", fork_ci(gh, event=event), gh, model) == 0
    assert "PR #7: re-sorted as needs_work" in capsys.readouterr().out
    assert HELD not in summary_of(gh) and "In its favour: CI passes." in summary_of(gh)
    assert model.prompts == []  # the same commit: the model read it all last time


@pytest.mark.parametrize("why, changes", [
    ("another commit", {"head_sha": "b" * 40}),
    ("another fork, with the same commit", {"head_repository": {"full_name": "eve/widget"}}),
    ("another branch of the fork", {"head_branch": "main"}),
    ("no fork named", {"head_repository": None}),
    ("no branch named", {"head_branch": None}),
])
def test_a_ci_run_that_matches_no_open_pr_re_sorts_nothing_and_says_so(tmp_path, monkeypatch, capsys, why, changes):
    gh = on_a_fork(FakeGitHub(diff=DIFF, ci=CI.HELD))
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    gh.ci = CI.PASSED
    before = summary_of(gh)
    capsys.readouterr()
    assert run(tmp_path, monkeypatch, "workflow_run", fork_ci(gh, **changes), gh) == 0, why
    out = capsys.readouterr().out
    assert "so there's no PR to re-sort." in out and "re-sorted as" not in out, why
    assert summary_of(gh) == before, why


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch", "merge_group", None])
def test_a_ci_run_that_isnt_a_prs_lists_no_prs(tmp_path, monkeypatch, capsys, event):
    """A push to the default branch, say: GitHub names the PRs of this repository's branches itself, so there's nothing to look up."""
    listed = []

    class Counting(FakeGitHub):
        def open_pulls(self):
            listed.append(1)
            return super().open_pulls()

    gh = on_a_fork(Counting(diff=DIFF))
    payload = fork_ci(gh, event=event)
    assert run(tmp_path, monkeypatch, "workflow_run", payload, gh) == 0
    assert listed == [] and gh.issue_comments_[7] == [] and f"This CI run ({event or 'no event'}) names no PR" in capsys.readouterr().out


def test_every_pr_from_the_runs_branch_is_re_sorted_and_no_other(tmp_path, monkeypatch, capsys):
    """The fork's branch is in two PRs, into main and into a release branch: GitHub files both PRs' CI under the one commit,
    so both read the same CI. Another fork's PR with the same commit has CI runs of its own, which re-sort it."""
    gh = on_a_fork(FakeGitHub(diff=DIFF, ci=CI.HELD))
    for number, repo in ((8, "mallory/widget"), (9, "eve/widget")):
        gh.pulls[number] = {**gh.pulls[7], "number": number, "head": dict(gh.pulls[7]["head"]), "labels": []}
        gh.issue_comments_[number], gh.labels[number] = [], []
        on_a_fork(gh, number, repo)
    for number in (7, 8, 9):
        run(tmp_path, monkeypatch, "pull_request_target", {**pr_payload(), "pull_request": {"number": number}}, gh)
    gh.ci = CI.PASSED
    capsys.readouterr()
    assert run(tmp_path, monkeypatch, "workflow_run", fork_ci(gh), gh) == 0
    out = capsys.readouterr().out
    assert "PR #7: re-sorted" in out and "PR #8: re-sorted" in out and "PR #9" not in out
    assert HELD not in summary_of(gh, 7) and HELD not in summary_of(gh, 8) and HELD in summary_of(gh, 9)


def test_ci_finishing_doesnt_start_a_review_of_a_forks_commit(tmp_path, monkeypatch, capsys):
    """Only the PR's own event reviews a commit, so a workflow that holds an outside PR's review for approval still holds it."""
    gh = on_a_fork(FakeGitHub(diff=DIFF))
    model = FakeModel(model_answer())
    unreviewed = "PR #7: Stratum hasn't reviewed this commit yet; that review reads the CI and sorts the PR."
    assert run(tmp_path, monkeypatch, "workflow_run", fork_ci(gh), gh, model) == 0
    assert unreviewed in capsys.readouterr().out
    assert gh.issue_comments_[7] == [] and gh.reviews == [] and gh.checks == [] and model.prompts == []

    # Reviewed at an older commit: the review of the new one is still to come.
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    gh.move_head(7, "c" * 40)
    before, prompts = summary_of(gh), len(model.prompts)
    capsys.readouterr()
    assert run(tmp_path, monkeypatch, "workflow_run", fork_ci(gh), gh, model) == 0
    assert unreviewed in capsys.readouterr().out and summary_of(gh) == before and len(model.prompts) == prompts

    # Only Stratum's own summary says what it reviewed: a copy of it, edited to name the new commit, doesn't.
    state = publish.State.decode(before)
    state.head = "c" * 40
    forged = publish.replace_state(before, state)
    assert publish.State.decode(forged).head == "c" * 40
    gh.issue_comments_[7].append({"id": 99, "body": forged, "user": {"login": "mallory", "type": "User"}})
    capsys.readouterr()
    assert run(tmp_path, monkeypatch, "workflow_run", fork_ci(gh), gh, model) == 0
    assert unreviewed in capsys.readouterr().out and len(model.prompts) == prompts


def test_a_push_just_after_the_check_is_left_to_its_own_review(tmp_path, monkeypatch, capsys):
    """The fork pushes between the PR list and the re-sort: what's re-sorted is the commit that was checked, so the new
    one isn't reviewed here, without the approval a workflow may hold its own review for."""

    class Pushed(FakeGitHub):
        push = False

        def open_pulls(self):
            listed = copy.deepcopy(super().open_pulls())  # GitHub's answer, which a later push doesn't change
            if self.push:
                self.move_head(7, "d" * 40)
            return listed

    gh = on_a_fork(Pushed(diff=DIFF, ci=CI.HELD))
    run(tmp_path, monkeypatch, "pull_request_target", pr_payload(), gh)
    gh.ci, gh.push = CI.PASSED, True
    before, reviews, checks, model = summary_of(gh), len(gh.reviews), len(gh.checks), FakeModel(model_answer())
    capsys.readouterr()
    assert run(tmp_path, monkeypatch, "workflow_run", fork_ci(gh), gh, model) == 0
    assert "PR #7: a newer commit arrived, so its own review sorts it." in capsys.readouterr().out
    assert summary_of(gh) == before and len(gh.reviews) == reviews and len(gh.checks) == checks and model.prompts == []


def test_the_runners_cpu_gets_small_calls_unless_the_policy_says_otherwise():
    from stratum_open.action import context_from_env
    from stratum_open.policy import parse_policy

    assert context_from_env(parse_policy(None), {"STRATUM_CONTEXT_TOKENS": "8192"}).review.context_tokens == 8192
    assert context_from_env(parse_policy(None), {}).review.context_tokens == 16384
    pinned = parse_policy("review:\n  context_tokens: 12000\n")
    assert context_from_env(pinned, {"STRATUM_CONTEXT_TOKENS": "8192"}).review.context_tokens == 12000
    # Explicit now, so the profile doesn't raise it again.
    assert "review.context_tokens" in context_from_env(parse_policy(None), {"STRATUM_CONTEXT_TOKENS": "8192"}).explicit


def test_the_main_action_passes_triage_queue_to_the_review_step():
    action_yml = workflow("action.yml")
    assert action_yml["inputs"]["triage-queue"]["default"] == "true"  # a workflow without a queue job rebuilds it as before
    [review] = [step for step in action_yml["runs"]["steps"] if step.get("id") == "review"]
    assert review["env"]["STRATUM_TRIAGE_QUEUE"] == "${{ inputs.triage-queue }}"
