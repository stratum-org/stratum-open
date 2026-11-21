"""The GitHub Action: one run per PR event, nothing kept between runs.

The workflow checks out the **default branch** (never the PR) so Stratum can
read the project's history; the PR itself is only ever read through the API.
That's what makes `pull_request_target` safe here: no code from the PR runs.

The one exception is opt-in: `checks.go_vet` and `checks.clippy` build the
PR's code, and run only when the workflow also checks the PR out somewhere
else and passes that folder as `head-checkout` (STRATUM_HEAD_CHECKOUT). A PR
from a fork, or one a bot opened, isn't built (`runner.worktree_for`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, replace
from typing import Mapping

import requests

from stratum_open.checks.dependencies import PackageLookup
from stratum_open.github import publish
from stratum_open.github.api import API, GitHub, GitHubError
from stratum_open.github.commands import LABEL_COMMANDS, NEEDS_MODEL, handle, may_run, parse, unreadable_reply
from stratum_open.model import model_from_policy
from stratum_open.policy import Policy
from stratum_open.runner import Context, checkout_ref, load_context_policy, post_early, review_pull, skip_reason, stop_early
from stratum_open.triage_queue import update_queue
from stratum_open.util import redact

DEFAULT_BOT = "github-actions[bot]"
QUEUE_OFF = "triage.queue is off in .stratum/policy.yml, so there's no queue issue to rebuild."
QUEUE_WAIT = 60  # seconds the queue job waits for a newer event before rebuilding (STRATUM_QUEUE_WAIT)
# PR events that only change the triage queue: a PR leaving it. No review, no model.
QUEUE_ONLY = ("closed", "converted_to_draft")


def main(env: Mapping[str, str] = os.environ) -> int:
    """Exit 0 when the event was handled, 1 when it couldn't be. A failure is
    one `::error::` line for the job's log, never a traceback; when the event
    names a commit, a neutral check run says the review didn't finish, so
    nothing is left waiting on it."""
    event_name = env.get("GITHUB_EVENT_NAME", "")
    try:
        with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as handle_file:
            payload = json.load(handle_file)
    except (KeyError, OSError, ValueError) as exc:
        print(f"::error::Stratum couldn't read the event payload: {type(exc).__name__}: {exc}")
        return 1
    if not isinstance(payload, dict):
        print("::error::Stratum couldn't read the event payload: it isn't a JSON object.")
        return 1
    token = env.get("STRATUM_GITHUB_TOKEN") or env.get("GITHUB_TOKEN")
    if not token:
        print("::error::Stratum needs a GitHub token: pass github-token to the action.")
        return 1
    repo = env.get("GITHUB_REPOSITORY") or (payload.get("repository") or {}).get("full_name") or ""
    gh = GitHub(token, repo, api=env.get("GITHUB_API_URL") or API)
    loaded = Loaded()  # filled in by _run once the policy is read, so a failure check matches the policy's check name
    try:
        return _run(env, event_name, payload, gh, loaded)
    except (GitHubError, requests.RequestException) as exc:
        print(f"::error::Stratum couldn't finish: {_describe(exc)}")
        _fail_check(gh, payload, _describe(exc), loaded.check_name, _early_run(env))
        return 1
    except Exception as exc:  # anything else is a bug: one line for the job, the details in the log
        print(f"::error::Stratum hit a bug and couldn't finish: {type(exc).__name__}: {exc}")
        print(redact(traceback.format_exc()), file=sys.stderr)
        _fail_check(gh, payload, f"Stratum hit a bug ({type(exc).__name__}) and couldn't finish this review.", loaded.check_name,
                    _early_run(env))
        return 1


@dataclass
class Loaded:
    """What `_run` has learned by the time it fails, for the failure path."""
    check_name: str = publish.CHECK_NAME


def _describe(exc: BaseException) -> str:
    if isinstance(exc, GitHubError):
        return str(exc)
    return f"couldn't reach GitHub ({type(exc).__name__})"


def _fail_check(gh: GitHub, payload: dict, message: str, name: str = publish.CHECK_NAME, run_id: int = 0) -> None:
    """Best effort: a completed, neutral check run on the PR's head saying the
    review didn't happen, so nobody waits on a check that never comes. `name`
    is the policy's `review.check_name` when the policy was read, which is the
    name branch protection waits for. `run_id`: the run `review.early_checks`
    left in progress, which this completes instead of posting another."""
    head = ((payload.get("pull_request") or {}).get("head") or {}).get("sha")
    if not head or not gh.repo:
        return
    try:
        gh.check_run(head, name, "neutral", "Stratum couldn't finish", redact(message)[:1000], "", [], **({"run_id": run_id} if run_id else {}))
    except (GitHubError, requests.RequestException) as exc:
        print(f"::warning::Stratum couldn't post its check run either: {_describe(exc)}")


def _run(env: Mapping[str, str], event_name: str, payload: dict, gh: GitHub, loaded: Loaded | None = None) -> int:
    default_branch = (payload.get("repository") or {}).get("default_branch") or gh.repository().get("default_branch") or "main"
    policy = context_from_env(load_context_policy(gh, default_branch), env)
    if loaded is not None:
        loaded.check_name = policy.review.check_name
    for error in policy.errors:
        print(f"::warning::.stratum/policy.yml: {error} Using the defaults.")
    for warning in policy.warnings:
        print(f"::notice::.stratum/policy.yml: {warning}")

    ctx = _context(env, gh, policy, model_from_policy(policy, env), default_branch)

    if event_name in ("pull_request", "pull_request_target"):
        pull = payload.get("pull_request") or {}
        if not pull.get("number"):
            print("::error::The event has no pull request in it.")
            return 1
        reason = skip_reason(pull, payload.get("action", ""), policy)
        if reason:
            print(reason)
        else:
            outcome = review_pull(ctx, pull["number"])
            print(f"PR #{outcome.number}: {outcome.status}, {len(outcome.findings)} finding(s)" + (f", {outcome.triage.key}" if outcome.triage else ""))
            if outcome.status != "published" and _early_run(env):  # the early post waits for a review that won't come
                stop_early(gh, pull["number"], (pull.get("head") or {}).get("sha", ""), bot=ctx.bot, name=policy.review.check_name,
                           run_id=_early_run(env), conclusion=env.get("STRATUM_EARLY_CONCLUSION", ""))
    elif event_name in ("issue_comment", "pull_request_review_comment"):
        unreadable = unreadable_reply(ctx, event_name, payload)
        if unreadable:  # the job fails: GitHub tells whoever started a run when it fails, and that is who wrote the reply
            print(f"::error::{unreadable}")
            return 1
        done = handle(ctx, event_name, payload)
        print(f"Command: {done or 'none in this comment'}")
    elif event_name == "workflow_run":
        # CI finished: sort the PR again with its result. Same head, so the
        # model reads only files the last review couldn't (usually none). The
        # skip settings apply here too; review.events doesn't, since this isn't
        # a PR event.
        for ref in (payload.get("workflow_run") or {}).get("pull_requests") or []:
            if not isinstance(ref, dict) or not ref.get("number"):
                continue
            reason = skip_reason(gh.pull(ref["number"]), None, policy)
            if reason:
                print(f"PR #{ref['number']}: {reason}")
                continue
            outcome = review_pull(ctx, ref["number"])
            print(f"PR #{outcome.number}: re-sorted as {outcome.triage.key if outcome.triage else outcome.status}")
    elif event_name in ("schedule", "workflow_dispatch"):
        if (env.get("STRATUM_TRIAGE_QUEUE") or "").strip().lower() == "false":
            # Its own job rebuilds it, in one concurrency group with the rebuilds after reviews.
            print("The workflow's queue job rebuilds the triage queue.")
        elif policy.triage.queue:
            update_queue(ctx)
            print("Triage queue updated.")
        else:
            print(QUEUE_OFF)
    else:
        print(f"Stratum doesn't handle {event_name} events.")
    return 0


def _context(env: Mapping[str, str], gh: GitHub, policy: Policy, model, default_branch: str) -> Context:
    checkout = _checkout(env.get("GITHUB_WORKSPACE"))
    return Context(
        gh=gh,
        policy=policy,
        model=model,
        bot=env.get("STRATUM_BOT_LOGIN") or DEFAULT_BOT,
        checkout=checkout,
        history_ref=checkout_ref(checkout, default_branch) if checkout else "HEAD",
        head_checkout=_head_checkout(env.get("STRATUM_HEAD_CHECKOUT")),
        lookup=PackageLookup(),
        own_checks=tuple(name for name in (env.get("GITHUB_JOB"), env.get("STRATUM_JOB_NAME")) if name),
        fork_replies=False,
    )


def _early_run(env: Mapping[str, str]) -> int:
    """The check run `stratum-open action --early` left in progress in this job, or 0."""
    raw = (env.get("STRATUM_EARLY_CHECK_RUN") or "").strip()
    return int(raw) if raw.isdigit() else 0


def early(env: Mapping[str, str] = os.environ) -> int:
    """`stratum-open action --early`, the step before the model starts: with
    `review.early_checks` on, a PR event that will be reviewed by a model
    gets the checks' findings posted now (see `runner.post_early`). The
    step's outputs tell the later steps what it left in progress. It never
    fails the job: whatever goes wrong, the review step runs as it would
    without it."""
    try:
        return _early(env)
    except Exception as exc:  # the full review still comes; this step only goes first
        print(f"::warning::Stratum couldn't post the checks first ({type(exc).__name__}: {redact(str(exc))[:300]}); the full review follows.")
        return 0


def _early(env: Mapping[str, str]) -> int:
    if env.get("GITHUB_EVENT_NAME") not in ("pull_request", "pull_request_target"):
        return 0
    with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as handle_file:
        payload = json.load(handle_file)
    token = env.get("STRATUM_GITHUB_TOKEN") or env.get("GITHUB_TOKEN")
    pull = (payload.get("pull_request") or {}) if isinstance(payload, dict) else {}
    if not token or not isinstance(pull, dict) or not pull.get("number"):
        return 0  # the review step says what's wrong
    repo = env.get("GITHUB_REPOSITORY") or (payload.get("repository") or {}).get("full_name") or ""
    gh = GitHub(token, repo, api=env.get("GITHUB_API_URL") or API)
    default_branch = (payload.get("repository") or {}).get("default_branch") or gh.repository().get("default_branch") or "main"
    policy = context_from_env(load_context_policy(gh, default_branch), env)
    if not policy.review.early_checks:
        return 0
    model = model_from_policy(policy, env)
    if model is None:  # a review with only the checks takes no longer than this would
        return 0
    reason = skip_reason(pull, payload.get("action", ""), policy)
    if reason:
        return 0
    outcome = post_early(_context(env, gh, policy, model, default_branch), pull["number"])
    if outcome.early is None:
        print(f"PR #{outcome.number}: {outcome.status}; nothing posted early")
        return 0
    print(f"PR #{outcome.number}: the checks' {len(outcome.findings)} finding(s) posted; the model's review follows")
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"posted=true\ncheck-run={outcome.early.check_run}\nconclusion={outcome.early.conclusion}\n")
    return 0


def stopped(env: Mapping[str, str] = os.environ) -> int:
    """`stratum-open action --stopped`, after the review step failed, timed
    out or was cancelled: the early post says the model's review didn't
    finish, and its check run is completed. Best effort; never fails."""
    try:
        with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as handle_file:
            payload = json.load(handle_file)
        pull = payload.get("pull_request") or {}
        head = (pull.get("head") or {}).get("sha", "")
        token = env.get("STRATUM_GITHUB_TOKEN") or env.get("GITHUB_TOKEN")
        if not token or not pull.get("number") or not head:
            return 0
        repo = env.get("GITHUB_REPOSITORY") or (payload.get("repository") or {}).get("full_name") or ""
        gh = GitHub(token, repo, api=env.get("GITHUB_API_URL") or API)
        default_branch = (payload.get("repository") or {}).get("default_branch") or gh.repository().get("default_branch") or "main"
        name = load_context_policy(gh, default_branch).review.check_name
        changed = stop_early(gh, pull["number"], head, bot=env.get("STRATUM_BOT_LOGIN") or DEFAULT_BOT, name=name,
                             run_id=_early_run(env), conclusion=env.get("STRATUM_EARLY_CONCLUSION", ""))
        print("The model's review didn't finish; the summary says so." if changed else "The model's review didn't finish.")
    except Exception as exc:
        print(f"::warning::Stratum couldn't say that the model's review didn't finish ({type(exc).__name__}: {redact(str(exc))[:300]}).")
    return 0


def queue(env: Mapping[str, str] = os.environ) -> int:
    """`stratum-open action --queue`, the workflow's queue job: after a PR's
    review, when a PR closes or turns back into a draft, after a command or
    a CI result that can change a PR's label, and on the daily and manual
    runs. No model. It never fails the job, so a PR's checks don't go red
    over the queue: the next rebuild catches up."""
    try:
        with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as handle_file:
            payload = json.load(handle_file)
        payload = payload if isinstance(payload, dict) else {}
        event_name = env.get("GITHUB_EVENT_NAME", "")
        bot = env.get("STRATUM_BOT_LOGIN") or DEFAULT_BOT
        command = None
        if event_name in ("issue_comment", "pull_request_review_comment"):
            # Anyone can comment, and each rebuild reads every open PR: only a command that ran rebuilds it.
            command = _label_command(event_name, payload, bot)
            if command is None:
                return 0
        if event_name == "workflow_run" and not (payload.get("workflow_run") or {}).get("pull_requests"):
            print("This CI run re-sorted no PR, so the triage queue stays as it is.")
            return 0
        token = env.get("STRATUM_GITHUB_TOKEN") or env.get("GITHUB_TOKEN")
        if not token:
            print("::warning::Stratum needs a GitHub token to rebuild the triage queue: pass github-token to the action.")
            return 0
        repo = env.get("GITHUB_REPOSITORY") or (payload.get("repository") or {}).get("full_name") or ""
        gh = GitHub(token, repo, api=env.get("GITHUB_API_URL") or API)

        def context() -> Context | None:
            default_branch = (payload.get("repository") or {}).get("default_branch") or gh.repository().get("default_branch") or "main"
            policy = load_context_policy(gh, default_branch)
            if not policy.triage.queue:
                print(QUEUE_OFF)
                return None
            return Context(gh=gh, policy=policy, model=None, bot=bot)

        ctx = None
        if command is not None:  # a call or two to the policy and the commenter's access, rather than a rebuild
            ctx = context()
            if ctx is None:
                return 0
            name, login, number = command
            if not may_run(ctx, name, login, number):
                print(f"{login} may not run that command (commands.{name}), so the triage queue stays as it is.")
                return 0
        if event_name not in ("schedule", "workflow_dispatch"):
            # A rebuild reads every open PR. While this one waits, a newer event's rebuild in the
            # job's concurrency group cancels it, before it has read them, so a burst costs one.
            raw = (env.get("STRATUM_QUEUE_WAIT") or "").strip()
            time.sleep(int(raw) if raw.isdigit() else QUEUE_WAIT)
        ctx = ctx or context()
        if ctx is None:
            return 0
        update_queue(ctx)
        print("Triage queue updated.")
    except Exception as exc:
        print(f"::warning::Stratum couldn't rebuild the triage queue ({type(exc).__name__}: {redact(str(exc))[:300]}); the next run will.")
    return 0


def _label_command(event_name: str, payload: dict, bot: str) -> tuple[str, str, int] | None:
    """(the policy's command, the commenter, the PR) for a comment that can
    have changed a PR's label, as `handle()` reads it; else None, said why."""
    comment = payload.get("comment") or {}
    parsed = parse(comment.get("body") or "")
    if parsed is None or parsed[0] not in LABEL_COMMANDS:
        print("This comment can't change a PR's label, so the triage queue stays as it is.")
        return None
    if event_name == "issue_comment":
        issue = payload.get("issue") or {}
        number = issue.get("number") if "pull_request" in issue else None
    else:
        number = (payload.get("pull_request") or {}).get("number")
    user = comment.get("user") or {}  # None for a deleted account
    login = user.get("login") or ""
    if not isinstance(number, int) or not login or user.get("type") == "Bot" or login.lower().endswith("[bot]") \
            or login.lower() == bot.lower():
        print("This comment can't have run a command on a PR, so the triage queue stays as it is.")
        return None
    return LABEL_COMMANDS[parsed[0]], login, number


def context_from_env(policy: Policy, env: Mapping[str, str]) -> Policy:
    """STRATUM_CONTEXT_TOKENS, which the action sets when Ollama runs on the
    runner's CPU: a big prompt there takes longer to read than a model call
    may last, so calls stay small. The policy file's own value wins."""
    raw = (env.get("STRATUM_CONTEXT_TOKENS") or "").strip()
    if not raw.isdigit() or "review.context_tokens" in policy.explicit:
        return policy
    return replace(policy, review=replace(policy.review, context_tokens=max(2048, int(raw))),
                   explicit=policy.explicit | {"review.context_tokens"})


def needs_model(env: Mapping[str, str] = os.environ) -> bool:
    """Whether this event can call the model, so the workflow only starts
    Ollama when it has to. It asks the same parser that runs the command.
    A PR closing or turning back into a draft only rebuilds the triage queue."""
    event_name = env.get("GITHUB_EVENT_NAME")
    if event_name in ("pull_request", "pull_request_target"):
        with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as handle_file:
            payload = json.load(handle_file)
        return not (isinstance(payload, dict) and payload.get("action") in QUEUE_ONLY)
    if event_name not in ("issue_comment", "pull_request_review_comment"):
        return True
    with open(env["GITHUB_EVENT_PATH"], encoding="utf-8") as handle_file:
        payload = json.load(handle_file)
    parsed = parse((payload.get("comment") or {}).get("body", ""))
    return parsed is not None and parsed[0] in NEEDS_MODEL


def _head_checkout(path: str | None) -> str | None:
    """The folder `head-checkout` names, made absolute against the workspace
    as the workflow's paths are; None when it's unset."""
    path = (path or "").strip()
    return os.path.abspath(path) if path else None


def _checkout(workspace: str | None) -> str | None:
    if not workspace:
        return None
    inside = subprocess.run(["git", "-C", workspace, "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    return workspace if inside.returncode == 0 and inside.stdout.strip() == "true" else None


if __name__ == "__main__":
    raise SystemExit(main())
