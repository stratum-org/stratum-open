"""`stratum-open` (or `python -m stratum_open`): review a PR or local
changes, see a track record, check a policy file, sort the PR queue, manage
the App's data, run the App.

The CLI keeps nothing: results go to the terminal or a JSON file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import requests

from stratum_open import __version__
from stratum_open.diff import parse_diff
from stratum_open.findings import Finding
from stratum_open.github.api import API, GitHubError
from stratum_open.model import model_from_policy
from stratum_open.policy import POLICY_PATH, Policy, parse_policy
from stratum_open.profiles import model_settings_line
from stratum_open.track_record import author_identity, build_records, describe_subprocess_error

_PR = re.compile(r"(?:https://github\.com/)?([\w.-]+/[\w.-]+)(?:/pull/|#)(\d+)/?$")
MAX_LOCAL_CONTENTS = 40  # files whose full text a local review reads, once it has read what a review on GitHub would


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stratum-open", description="PR review and triage on a model you run yourself.")
    parser.add_argument("--version", action="version", version=f"stratum-open {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="review a GitHub PR, or your local changes")
    review.add_argument("target", nargs="?", help="a PR: https://github.com/owner/repo/pull/12 or owner/repo#12")
    review.add_argument("--local", metavar="BASE", nargs="?", const="origin/HEAD", help="review this checkout's changes since BASE (default origin/HEAD)")
    review.add_argument("--post", action="store_true", help="publish the review on the PR (default: print it only)")
    review.add_argument("--full", action="store_true", help="review the whole PR, not only what changed since the last review")
    review.add_argument("--checkout", help="a checkout of the PR's default branch, for history-based notes")
    review.add_argument("--no-model", action="store_true", help="run the checks only")
    review.add_argument("--json", metavar="FILE", help="also write the findings to FILE as JSON")

    record = sub.add_parser("record", help="a contributor's track record, from git history")
    record.add_argument("login", help="a GitHub login or a commit email")
    record.add_argument("--repo", default=".", help="the repo's checkout (default: here)")
    record.add_argument("--ref", default="HEAD", help="the default branch (default: HEAD)")

    policy = sub.add_parser("policy", help="check a policy file")
    policy.add_argument("action", choices=["check"])
    policy.add_argument("file", nargs="?", default=POLICY_PATH)

    replay = sub.add_parser("replay", help="what the checks and triage would have said on a repo's closed PRs; posts nothing")
    replay.add_argument("repo", help="owner/name")
    replay.add_argument("--last", type=int, default=50, metavar="N", help="how many closed PRs, newest first (default 50)")
    replay.add_argument("--state", choices=["merged", "closed", "all"], default="all", help="only merged PRs, only those closed without merging, or all (default)")
    replay.add_argument("--json", action="store_true", help="print the data as JSON instead of the report")
    replay.add_argument("--out", metavar="FILE", help="also write the report to FILE, as Markdown")

    queue = sub.add_parser("triage", help="the sorted queue of a repo's open PRs")
    queue.add_argument("repo", help="owner/name")
    queue.add_argument("--post", action="store_true", help="update the queue issue on GitHub")

    data = sub.add_parser("data", help="see or delete what the GitHub App stored")
    data.add_argument("action", choices=["delete", "purge", "export"])
    data.add_argument("--db", default=os.environ.get("STRATUM_DB", "stratum.db"))
    data.add_argument("--repo", help="owner/name")
    data.add_argument("--pr", type=int, help="with delete: one PR's findings and job log")
    data.add_argument("--feedback", action="store_true", help="with delete: thumbs-up/down votes and muted rules")
    data.add_argument("--all", action="store_true", help="with delete --repo: everything stored for the repo")
    data.add_argument("--days", type=_days, default=180, help="with purge: findings older than this go (jobs: 14 days)")

    serve = sub.add_parser("serve", help="run the GitHub App's webhook server")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))

    action = sub.add_parser("action", help="the GitHub Action's entry point")
    step = action.add_mutually_exclusive_group()
    step.add_argument("--early", action="store_true", help="post the checks' findings before the model starts, with review.early_checks on")
    step.add_argument("--stopped", action="store_true", help="after --early, when the review step didn't finish: say so on the PR")
    sub.add_parser("needs-model", help="the GitHub Action: print whether this event can call the model")
    gitlab = sub.add_parser("gitlab", help="GitLab CI's entry point: review the pipeline's merge request")
    gitlab.add_argument("--dry-run", action="store_true", help="print the review instead of posting it")

    args = parser.parse_args(argv)
    _printable_output()
    command = {
        "review": _review, "record": _record, "policy": _policy, "triage": _triage,
        "data": _data, "serve": _serve, "action": _action, "needs-model": _needs_model,
        "replay": _replay, "gitlab": _gitlab,
    }[args.command]
    # Exit codes: 0 done; 1 a usage problem, or `review --local` found something critical;
    # 2 GitHub or the network refused (the message says what); 130 stopped with Ctrl-C.
    try:
        return command(args)
    except GitHubError as exc:
        print(f"{exc}. {_github_hint(exc.status)}".strip(), file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"Couldn't reach {_host(exc)}: {type(exc).__name__}. Check the network, the URL and any proxy, then try again.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130


def _printable_output() -> None:
    """A model's text can hold any character; a pipe on Windows may only take
    cp1252. Print what can be printed and escape the rest, rather than die
    on the first arrow or emoji."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # a closed or exotic stream
                pass


def _github_hint(status: int) -> str:
    if status == 401:
        return "The token was refused: check GITHUB_TOKEN, or sign in again with `gh auth login`."
    if status == 403:
        return "The token can't do that here, or GitHub's rate limit is reached; try again later."
    if status == 404:
        return "Check the repo and PR number, and that the token can see the repo."
    return ""


def _host(exc: BaseException) -> str:
    request = getattr(exc, "request", None)
    url = getattr(request, "url", "") or ""
    return re.sub(r"^(https?://[^/]+).*$", r"\1", url) or "the server"


# ── review ───────────────────────────────────────────────────────────────


def _review(args) -> int:
    if args.local is not None or not args.target:
        return _review_local(args, args.local or "origin/HEAD")
    match = _PR.match(args.target)
    if not match:
        sys.exit(f"Not a PR: {args.target}. Use https://github.com/owner/repo/pull/12 or owner/repo#12.")
    from stratum_open.checks.dependencies import PackageLookup
    from stratum_open.github.api import GitHub
    from stratum_open.runner import Context, checkout_ref, load_context_policy, review_pull

    gh = GitHub(_token(), match.group(1), api=_api())
    default_branch = gh.repository().get("default_branch") or "main"
    policy = load_context_policy(gh, default_branch)
    _policy_notes(policy)
    bot = os.environ.get("STRATUM_BOT_LOGIN") or ((gh.get("/user") or {}).get("login") if args.post else "")
    ctx = Context(
        gh=gh, policy=policy, model=None if args.no_model else model_from_policy(policy), bot=bot or "-",
        checkout=args.checkout, history_ref=checkout_ref(args.checkout, default_branch) if args.checkout else "HEAD",
        lookup=PackageLookup(), log=lambda line: print(line, file=sys.stderr),
    )
    outcome = review_pull(ctx, int(match.group(2)), full=args.full, post=args.post)
    if outcome.status == "dry-run":
        print(re.sub(r"<!--.*?-->", "", outcome.summary, flags=re.DOTALL).strip())
        print()
        _print_findings(outcome.findings)
    else:
        print(f"PR #{outcome.number}: {outcome.status}" + (f" ({outcome.note})" if outcome.note else ""))
    _write_json(args.json, outcome.findings)
    return 0


def _review_local(args, base: str) -> int:
    """Review `git diff BASE...HEAD` here, with the policy as it is on BASE,
    through the same engine call as a review on GitHub."""
    from stratum_open.checks.common import looks_binary
    from stratum_open.checks.dependencies import PackageLookup
    from stratum_open.engine import analyze
    from stratum_open.repo.boundaries import boundary_findings
    from stratum_open.repo.removed_names import removed_name_findings
    from stratum_open.repo.unknown_names import unknown_name_findings
    from stratum_open.runner import read_contents, to_read

    try:
        # The flags keep out what the user's git config can change: an external
        # diff tool, textconv filters, other prefixes than a/ and b/, paths
        # relative to a subfolder, and the number of context lines.
        text = _git(".", "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/",
                    "--no-relative", "-U3", "--find-renames", f"{base}...HEAD")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"git couldn't compare {base} with HEAD: {(exc.stderr or '').strip() or 'is this a git repo, and does that branch exist?'}")
    files = parse_diff(text)
    if not files:
        print(f"No changes between {base} and HEAD.")
        return 0
    policy = parse_policy(_git_or_none(".", "show", f"{base}:{POLICY_PATH}"))
    _policy_notes(policy)
    model = None if args.no_model else model_from_policy(policy)
    # The checks read whole files where the diff isn't enough (workflows,
    # manifests, Bandit, moved files), in the order and to the limits they do
    # on GitHub; then the other changed files, up to MAX_LOCAL_CONTENTS in all.
    def read(file, max_bytes=None):
        text = _git_or_none(".", "show", f"HEAD:{file.path}")
        return None if text is None or (max_bytes is not None and len(text.encode("utf-8")) > max_bytes) else text

    fetch, moved = to_read(policy, files)
    contents = read_contents(fetch, moved, read)
    for file in [f for f in files if f.status != "deleted" and not f.binary and f not in fetch and f not in moved]:
        if len(contents) >= MAX_LOCAL_CONTENTS:
            break
        text = read(file)
        if text is not None and looks_binary(text):
            file.binary = True  # a move with no edits: the diff couldn't say it was binary
        elif text is not None:
            contents[file.path] = text
    top = (_git_or_none(".", "rev-parse", "--show-toplevel") or ".").strip()
    result = analyze(
        title=_git(".", "log", "-1", "--format=%s").strip(), description=_git(".", "log", "-1", "--format=%b").strip(),
        files=files, policy=policy, model=model, contents=contents or None,
        lookup=PackageLookup() if policy.dependencies.registry_lookup else None,
        # The working tree is HEAD, the change itself: the module baseline comes from where it branched off.
        extra_findings=boundary_findings(files, policy, ".", ref=(_git_or_none(".", "merge-base", base, "HEAD") or base).strip())
        # ... while a name is looked for in the modules as they'll be, and the working tree is that already.
        + unknown_name_findings(files, policy, top, contents=contents, applied=True)
        + removed_name_findings(files, policy, top, contents=contents, applied=True),
    )
    if model is not None:
        print(f"Profile: {result.profile} ({result.profile_why}).", file=sys.stderr)
        if result.model_settings:
            print(model_settings_line(model.name, result.model_settings), file=sys.stderr)
        if result.review is not None and result.review.calls and not result.review.ran:
            print("The model didn't answer: " + "; ".join(sorted({c['error'] for c in result.review.calls if c.get('error')})), file=sys.stderr)
        elif result.review is not None and result.review.summary:
            print(f"What this changes: {result.review.summary}\n")
    _print_findings(result.findings)
    _write_json(args.json, result.findings)
    return 1 if any(f.severity == "critical" for f in result.findings) else 0


def _print_findings(findings: list[Finding]) -> None:
    if not findings:
        print("No findings.")
        return
    for f in findings:
        where = f"{f.path}:{f.line_start}" if f.line_start else (f.path or "(the PR)")
        print(f"{where}  {f.severity.upper()} {f.category}  {f.title}  [{f.rule}]")
        print(f"    {' '.join(f.detail.split())}")
        if f.suggestion:
            print(f"    Fix: {f.suggestion}")
    print(f"\n{len(findings)} finding(s).")


def _write_json(path: str | None, findings: list[Finding]) -> None:
    if path:
        Path(path).write_text(json.dumps([f.to_dict() for f in findings], indent=2), encoding="utf-8")


# ── the rest ─────────────────────────────────────────────────────────────


def _record(args) -> int:
    # The project's settings, as `/stratum record` reads them: the policy on the default branch.
    policy = parse_policy(_git_or_none(args.repo, "show", f"{args.ref}:{POLICY_PATH}"))
    _policy_notes(policy)
    settings = policy.track_record
    if not settings.enabled:
        print("Track records are turned off in this project's policy.")
        return 0
    if author_identity(args.login) in {author_identity(e) for e in settings.exclude}:
        print(f"No record is built for {args.login}: the project's policy excludes that login.")
        return 0
    try:
        records = build_records(args.repo, args.ref, window_days=settings.window_days,
                                followup_days=settings.followup_days, exclude=settings.exclude)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        sys.exit(f"Couldn't read the history of {args.repo} at {args.ref}: {describe_subprocess_error(exc, 300)}")
    record = records.get(author_identity(args.login))
    if record is None:
        print(f"No merged PRs by {args.login} in the last {settings.window_days} days of {args.ref}. Records match a login only through "
              "GitHub's noreply emails; try the commit email too.")
        return 0
    print(f"{record.author}: {record.summary()}")
    for strike in record.strikes:
        how = "reverted" if strike.kind == "revert" else "fixed by a follow-up"
        print(f"  #{strike.pr.number} {strike.pr.title[:70]}: {how} in {strike.commit[:7]} ({strike.subject[:60]})")
    return 0


def _policy(args) -> int:
    path = Path(args.file)
    policy = parse_policy(path.read_text(encoding="utf-8") if path.exists() else None)
    if not path.exists():
        print(f"No {path}: the defaults apply.")
    for error in policy.errors:
        print(f"error: {error}")
    for warning in policy.warnings:
        print(f"warning: {warning}")
    if not policy.errors:
        print(f"{path} is usable." if path.exists() else "")
    return 1 if policy.errors else 0


def _replay(args) -> int:
    from stratum_open.replay import run

    return run(args.repo, token=_token(), api=_api(), last=args.last, state=args.state, as_json=args.json, out=args.out)


def _triage(args) -> int:
    from stratum_open.github.api import GitHub
    from stratum_open.runner import Context, load_context_policy
    from stratum_open.triage_queue import update_queue

    gh = GitHub(_token(), args.repo, api=_api())
    policy = load_context_policy(gh, gh.repository().get("default_branch") or "main")
    bot = os.environ.get("STRATUM_BOT_LOGIN") or "github-actions[bot]"
    poster = None
    if args.post and not os.environ.get("STRATUM_BOT_LOGIN"):
        try:  # the queue issue's author is the token's user, as for `review --post`
            poster = (gh.get("/user") or {}).get("login")
        except GitHubError:  # an Action's or App's token can't read /user; it posts as the bot
            pass
    print(update_queue(Context(gh=gh, policy=policy, model=None, bot=bot), post=args.post, poster=poster))
    return 0


def _days(value: str) -> int:
    """A day count for `data purge --days`, within the range the policy allows."""
    try:
        days = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if not 1 <= days <= 36500:
        raise argparse.ArgumentTypeError("must be between 1 and 36500")
    return days


def _data(args) -> int:
    from stratum_open.storage import SQLiteStorage

    if not Path(args.db).exists():
        sys.exit(f"No database at {args.db}. Only the GitHub App stores data; the Action and the CLI keep nothing.")
    try:
        storage = SQLiteStorage(args.db)
    except (sqlite3.Error, RuntimeError) as exc:
        sys.exit(f"Couldn't open the database at {args.db}: {exc}")
    if args.action == "purge":
        print(json.dumps(storage.purge(findings_days=args.days, repo=args.repo)))
    elif args.action == "export":
        if not args.repo:
            sys.exit("export needs --repo owner/name")
        print(json.dumps(storage.export(args.repo), indent=2, default=str))
    elif args.pr is not None:
        if not args.repo:
            sys.exit("delete --pr needs --repo owner/name")
        print(f"Deleted {storage.delete_pr(args.repo, args.pr)} row(s) for {args.repo}#{args.pr}.")
    elif args.feedback:
        print(f"Deleted {storage.delete_feedback(args.repo)} vote and mute row(s).")
    elif args.all and args.repo:
        print(f"Deleted {storage.delete_repo(args.repo)} row(s) for {args.repo}.")
    else:
        sys.exit("Say what to delete: --repo R --pr N, --feedback [--repo R], or --repo R --all.")
    return 0


def _serve(args) -> int:
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, make_server

    from stratum_open.app import from_env

    class Threaded(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    server = make_server(args.host, args.port, from_env(), server_class=Threaded)
    print(f"Stratum App listening on http://{args.host}:{args.port}/webhook")
    server.serve_forever()
    return 0


def _action(args) -> int:
    from stratum_open import action

    if getattr(args, "early", False):
        return action.early()
    if getattr(args, "stopped", False):
        return action.stopped()
    return action.main()


def _gitlab(args) -> int:
    from stratum_open.gitlab.ci import main as gitlab_main

    return gitlab_main(post=not args.dry_run)


def _needs_model(args) -> int:
    from stratum_open.action import needs_model

    print("true" if needs_model() else "false")
    return 0


def _policy_notes(policy: Policy) -> None:
    for error in policy.errors:
        print(f"policy: {error} Using the defaults.", file=sys.stderr)
    for warning in policy.warnings:
        print(f"policy: {warning}", file=sys.stderr)


def _api() -> str:
    """GitHub's API, or a GitHub Enterprise / test server named in STRATUM_GITHUB_API."""
    return os.environ.get("STRATUM_GITHUB_API") or API


def _token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        try:
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            token = ""
    if not token:
        sys.exit("Set GITHUB_TOKEN (or sign in with the GitHub CLI) to read PRs.")
    return token


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout


def _git_or_none(repo: str, *args: str) -> str | None:
    try:
        return _git(repo, *args)
    except subprocess.CalledProcessError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
