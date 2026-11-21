"""The triage queue: one issue listing every open PR, sorted.

It's rebuilt from each PR's last Stratum summary, so it costs no model calls
and needs no database.
"""

from __future__ import annotations

from stratum_open.github import publish
from stratum_open.runner import Context
from stratum_open.triage import PRFacts, Triage, render_queue

TITLE = "Stratum triage queue"
MAX_PULLS = 100


def update_queue(ctx: Context, *, post: bool = True, poster: str | None = None) -> str:
    """`poster` is the login the queue issue is posted as, when that isn't
    `ctx.bot`, the login whose PR summaries are read (the CLI with a personal
    token reads the Action's summaries and posts as the token's user)."""
    gh = ctx.gh
    rows: list[tuple[PRFacts, Triage | None]] = []
    for pull in gh.open_pulls_with_comments(MAX_PULLS):
        if pull.get("draft"):
            continue
        summary = publish.find_summary(pull["comments"], ctx.bot)
        found = publish.State.decode(summary["body"]).triage if summary else {}
        facts = PRFacts.from_pull(pull, [])
        facts.additions, facts.deletions = found.get("additions", 0), found.get("deletions", 0)
        result = None
        if found.get("key"):
            result = Triage(str(found["key"]), _strings(found.get("reasons")), _strings(found.get("evidence")), _number(found.get("score")))
        rows.append((facts, result))
    body = render_queue(rows, ctx.policy)
    return _post(ctx, body, poster or ctx.bot) if post else body


def _strings(value) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _post(ctx: Context, body: str, poster: str) -> str:
    """Edit the queue issue in place when there is one, else create it."""
    gh = ctx.gh
    issue = next((i for i in gh.paginate(f"/repos/{gh.repo}/issues", state="open")
                  if i.get("title") == TITLE and "pull_request" not in i
                  and publish._is_bot(i, poster)), None)
    if issue:
        gh.edit_issue(issue["number"], body)
    else:
        gh.create_issue(TITLE, body)
    return body
