import json
import re
from pathlib import Path

import pytest

from fakes import FakeGitHub, FakeModel
from test_runner import DIFF, model_answer

from stratum_open import action
from stratum_open.github import publish
from stratum_open.github.api import GitHubError
from stratum_open.github.commands import parse
from stratum_open.policy import parse_policy
from stratum_open.runner import Context, review_pull


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
    event.write_text(json.dumps({"issue": {"number": 7, "pull_request": {"url": "example"}}, "comment": {"body": body}} if body is not None else {}))
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


def test_ci_finishing_respects_the_skip_settings(tmp_path, monkeypatch, capsys):
    from stratum_open import action as action_module

    gh = FakeGitHub(diff=DIFF, author="renovate[bot]")
    gh.files[(".stratum/policy.yml", "main")] = "review:\n  skip_bots: true\n"
    reviewed = []
    monkeypatch.setattr(action_module, "review_pull", lambda ctx, number: reviewed.append(number))
    payload = {"repository": {"default_branch": "main"}, "workflow_run": {"pull_requests": [{"number": 7}]}}
    run(tmp_path, monkeypatch, "workflow_run", payload, gh)
    assert reviewed == [] and "review.skip_bots is on" in capsys.readouterr().out


def test_the_runners_cpu_gets_small_calls_unless_the_policy_says_otherwise():
    from stratum_open.action import context_from_env
    from stratum_open.policy import parse_policy

    assert context_from_env(parse_policy(None), {"STRATUM_CONTEXT_TOKENS": "8192"}).review.context_tokens == 8192
    assert context_from_env(parse_policy(None), {}).review.context_tokens == 16384
    pinned = parse_policy("review:\n  context_tokens: 12000\n")
    assert context_from_env(pinned, {"STRATUM_CONTEXT_TOKENS": "8192"}).review.context_tokens == 12000
    # Explicit now, so the profile doesn't raise it again.
    assert "review.context_tokens" in context_from_env(parse_policy(None), {"STRATUM_CONTEXT_TOKENS": "8192"}).explicit


def test_a_plain_issue_comment_does_not_start_a_review_model(tmp_path):
    import json

    event = tmp_path / "plain-issue.json"
    event.write_text(json.dumps({"issue": {"number": 7}, "comment": {"body": "/stratum review"}}))
    assert not action.needs_model({
        "GITHUB_EVENT_NAME": "issue_comment",
        "GITHUB_EVENT_PATH": str(event),
    })
