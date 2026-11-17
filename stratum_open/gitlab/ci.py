"""`stratum-open gitlab`: review the merge request of a GitLab CI pipeline.

GitLab's counterpart of the Action. It reads the MR from GitLab's predefined
variables (CI_MERGE_REQUEST_IID, CI_MERGE_REQUEST_PROJECT_ID, CI_API_V4_URL),
posts as the user behind STRATUM_GITLAB_TOKEN (a project or group access
token: CI_JOB_TOKEN can't write MR notes), and reads the MR only through the
API: the job doesn't need, and shouldn't run, anything of the MR's code. The
rules come from `.stratum/policy.yml` on the default branch, and history from
a checkout of the default branch named in STRATUM_CHECKOUT.

Optional: STRATUM_GITLAB_API (another API URL than the job's), STRATUM_BOT_LOGIN
(the token user's username, saving a call), STRATUM_GITLAB_STATUS=off (no
commit status), and the model variables the Action uses (STRATUM_MODEL_ENDPOINT,
STRATUM_MODEL_NAME, STRATUM_MODEL_API_KEY, STRATUM_CONTEXT_TOKENS).
"""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import replace
from typing import Mapping

import requests

from stratum_open.action import _checkout, context_from_env
from stratum_open.checks.dependencies import PackageLookup
from stratum_open.github.api import GitHubError
from stratum_open.gitlab.api import GitLab
from stratum_open.model import model_from_policy
from stratum_open.policy import Policy
from stratum_open.runner import Context, checkout_ref, load_context_policy, review_pull, skip_reason
from stratum_open.util import redact


def main(env: Mapping[str, str] = os.environ, *, post: bool = True) -> int:
    """Exit 0 when the MR was reviewed or there was nothing to do (not an MR
    pipeline, a fork's pipeline without the token); 1 when it couldn't be,
    with one line saying why."""
    iid = (env.get("CI_MERGE_REQUEST_IID") or "").strip()
    if not iid.isdigit():
        print("Not a merge request pipeline (no CI_MERGE_REQUEST_IID): nothing to review. "
              "Run the job with `rules: - if: $CI_PIPELINE_SOURCE == \"merge_request_event\"`.")
        return 0
    project = env.get("CI_MERGE_REQUEST_PROJECT_ID") or env.get("CI_PROJECT_ID") or ""
    api = env.get("STRATUM_GITLAB_API") or env.get("CI_API_V4_URL") or ""
    if not project or not api:
        print("Stratum couldn't tell which project and GitLab to use: CI_MERGE_REQUEST_PROJECT_ID and CI_API_V4_URL are unset.")
        return 1
    token = env.get("STRATUM_GITLAB_TOKEN") or ""
    here = env.get("CI_PROJECT_ID") or project
    fork = bool(env.get("CI_MERGE_REQUEST_SOURCE_PROJECT_ID")) and env.get("CI_MERGE_REQUEST_SOURCE_PROJECT_ID") != project
    if not token:
        if fork and here != project:
            print("This MR comes from a fork, and its pipeline runs in the fork, which can't see the project's STRATUM_GITLAB_TOKEN. "
                  "Nothing was posted. A maintainer can run the MR's pipeline in the project once they've read its changes.")
            return 0
        print("Stratum needs STRATUM_GITLAB_TOKEN: a project access token with the api scope, in a masked CI/CD variable.")
        return 1
    pipeline = env.get("CI_PIPELINE_ID") or ""
    gl = GitLab(
        token, project, api=api,
        # The status goes in this job's pipeline, unless the pipeline is a fork's, which the project's commit can't take.
        pipeline_id=int(pipeline) if pipeline.isdigit() and here == project else None,
        target_url=env.get("CI_JOB_URL") or None,
        statuses=(env.get("STRATUM_GITLAB_STATUS") or "on").strip().lower() not in ("off", "false", "0", "no"),
    )
    gl.repo = env.get("CI_MERGE_REQUEST_PROJECT_PATH") or (env.get("CI_PROJECT_PATH") if here == project else "") or project
    try:
        return _run(env, gl, int(iid), post=post)
    except (GitHubError, requests.RequestException) as exc:
        reason = str(exc) if isinstance(exc, GitHubError) else f"couldn't reach GitLab ({type(exc).__name__})"
        print(f"Stratum couldn't finish: {reason}")
        return 1
    except Exception as exc:  # anything else is a bug: one line for the job, the details in the log
        print(f"Stratum hit a bug and couldn't finish: {type(exc).__name__}: {exc}")
        print(redact(traceback.format_exc()), file=sys.stderr)
        return 1


def _run(env: Mapping[str, str], gl: GitLab, iid: int, *, post: bool) -> int:
    default_branch = gl.repository().get("default_branch") or env.get("CI_DEFAULT_BRANCH") or "main"
    policy = gitlab_policy(context_from_env(load_context_policy(gl, default_branch), env))
    for error in policy.errors:
        print(f"warning: .stratum/policy.yml: {error} Using the defaults.")
    for warning in policy.warnings:
        print(f"note: .stratum/policy.yml: {warning}")
    checkout = _checkout(env.get("STRATUM_CHECKOUT"))
    ctx = Context(
        gh=gl, policy=policy, model=model_from_policy(policy, env),
        bot=env.get("STRATUM_BOT_LOGIN") or gl.current_user().get("username") or "-",
        checkout=checkout, history_ref=checkout_ref(checkout, default_branch) if checkout else "HEAD",
        lookup=PackageLookup(), own_checks=tuple(name for name in (env.get("CI_JOB_NAME"), env.get("STRATUM_JOB_NAME")) if name),
    )
    # GitLab CI doesn't say what happened to the MR, only that a pipeline runs for it, so review.events doesn't apply.
    merge_request = gl.pull(iid)
    if merge_request.get("draft") and policy.review.skip_drafts:
        print("Draft merge request: run the pipeline again once it is ready for review.")
        return 0
    reason = skip_reason(merge_request, None, policy)
    if reason:
        print(reason)
        return 0
    outcome = review_pull(ctx, iid, post=post)
    print(f"MR !{outcome.number}: {outcome.status}, {len(outcome.findings)} finding(s)" + (f", {outcome.triage.key}" if outcome.triage else ""))
    if outcome.status == "dry-run":
        print(outcome.summary)
    return 0


def gitlab_policy(policy: Policy) -> Policy:
    """The policy as GitLab can follow it, each setting it can't follow
    turned off and named in the summary's policy warnings:

    - `review.learn_from_history` reads GitHub's reactions and comment
      history, which the GitLab client doesn't.
    - `review.early_checks` needs a step that posts the checks before the
      model starts and one that completes the post if the model's step fails;
      the GitLab job has neither, so the review is posted once, when it's done."""
    off = []
    review = policy.review
    if review.learn_from_history:
        review = replace(review, learn_from_history=False)
        off.append("review.learn_from_history isn't read on GitLab yet, so it's off for this review.")
    if review.early_checks:
        review = replace(review, early_checks=False)
        off.append("review.early_checks isn't run on GitLab yet, so the review is posted once, when the model has finished.")
    return replace(policy, review=review, warnings=policy.warnings + tuple(off)) if off else policy
