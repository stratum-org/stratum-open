"""Deterministic checks: problems found by rules over a PR's diff, with no
model call. They run on every PR, whatever the model does, and give the same
answer every time, so their findings are the ones a maintainer can rely on.

Every finding has `source="check"`, a stable `rule` id and, where it's about
a line, the new-file number of the added line it's about.
"""

from __future__ import annotations

from typing import Callable

from stratum_open.checks import actionlint, agents, awaits, bandit, common, dependencies, heuristics, paths, patterns, promises, ruff, shellcheck, tests_weakened, workflows
from stratum_open.checks.common import looks_binary
from stratum_open.checks.dependencies import PackageInfo, PackageLookup
from stratum_open.diff import FileDiff
from stratum_open.findings import Finding
from stratum_open.policy import Policy

__all__ = ["PackageInfo", "PackageLookup", "gained", "looks_binary", "moved_in", "run_checks"]

# The checks here whose rules depend on a file's path. Of the others, hidden
# Unicode looks at every path, and protected paths and coding-agent files at
# both sides of a rename. Module boundaries (repo/boundaries.py) also depend on
# the path and don't read moved files yet: #65.
_SCOPES = (patterns.scope, workflows.scope, dependencies.scope, bandit.scope, ruff.scope)
_MAX_LISTED = 10


def moved_in(file: FileDiff) -> bool:
    """Whether a rename brought the file to where some check does more than
    where it was, so that check reads all of it, and needs its text."""
    return any(common.moved_in(file, scope) for scope in _SCOPES)


def gained(file: FileDiff) -> int:
    """How many more rules the checks have for the file where it moved than
    where it was: 0 unless it moved in. Code moved out of `tests/` gains
    several; a data file of a type no check knows, only the secret rules. The
    files that gain most are the ones to read first."""
    if not moved_in(file):
        return 0
    return sum(len(scope(file.path) - scope(file.old_path or "")) for scope in _SCOPES)


def run_checks(
    files: list[FileDiff],
    policy: Policy,
    *,
    lookup: PackageLookup | None = None,
    contents: dict[str, str] | None = None,
    pr_text: str = "",
) -> list[Finding]:
    """Every check over the PR's files. `lookup` asks package registries
    about new dependencies, and OSV about pinned versions when
    `dependencies.known_vulnerabilities` is on (both only when
    `dependencies.registry_lookup` is on);
    `contents` maps a path to its full new text, when the caller has it, and
    lets the checks see past the diff's context. `pr_text` is the PR's title
    and description, for the heuristics.

    One check failing never stops the others: it becomes a low finding
    saying which check didn't run."""
    contents = contents or {}
    registry = lookup if policy.dependencies.registry_lookup else None
    checks: list[tuple[str, Callable[[], list[Finding]]]] = [
        ("protected paths", lambda: paths.check(files, policy)),
        ("risky code patterns", lambda: patterns.check(files, contents)),
        ("missing await", lambda: awaits.check(files, contents)),
        ("await on a plain file", lambda: awaits.check_sync_files(files, contents)),
        ("workflow", lambda: workflows.check(files, contents)),
        ("coding-agent files", lambda: agents.check_instruction_files(files)),
        ("hidden Unicode", lambda: agents.check_hidden_unicode(files)),
        ("dependency", lambda: dependencies.check(files, policy, lookup=registry, contents=contents)),
        ("Bandit", lambda: bandit.check(files, contents, keep_low=policy.checks.bandit_low)),
    ]
    if policy.checks.ruff:
        checks.append(("Ruff", lambda: ruff.check(files, contents)))
    if policy.checks.shellcheck:
        checks.append(("ShellCheck", lambda: shellcheck.check(files, contents)))
    if policy.checks.actionlint:
        checks.append(("actionlint", lambda: actionlint.check(files, contents)))
    if policy.checks.floating_promises:
        checks.append(("floating promise", lambda: promises.check(files, contents)))
    if policy.checks.heuristics:
        checks.append(("heuristics", lambda: heuristics.check(files, pr_text)))
    if policy.checks.weakened_tests:
        checks.append(("weakened tests", lambda: tests_weakened.check(files)))
    findings: list[Finding] = []
    for name, run in checks:
        try:
            findings.extend(run())
        except Exception as exc:  # one broken check mustn't cost the review the others
            message = f"{type(exc).__name__}: {exc}"[:200]
            findings.append(Finding(
                title=f"The {name} check didn't run",
                detail=f"The {name} check failed with an error ({message}), so this review is missing its findings. The other checks ran.",
                severity="low",
                category="workflow",
                suggestion="Re-run the review. If it fails again, please report it with this message.",
                source="check",
                rule="checks/error",
                confidence=1.0,
            ))
    findings.extend(_moved_unread(files, contents))
    return findings


def _moved_unread(files: list[FileDiff], contents: dict[str, str]) -> list[Finding]:
    """One low finding naming the files that moved into a check's scope
    without their text at hand: the checks saw only their added lines, if
    any, and a quiet review would otherwise look like a clean one."""
    unread = [file.path for file in files if moved_in(file) and file.path not in contents]
    if not unread:
        return []
    named = ", ".join(f"`{path}`" for path in unread[:_MAX_LISTED]) + (f" and {len(unread) - _MAX_LISTED} more" if len(unread) > _MAX_LISTED else "")
    return [Finding(
        title="Moved files weren't checked in full",
        detail=(
            f"This PR moves {'a file' if len(unread) == 1 else f'{len(unread)} files'} to where the checks do more than where "
            f"{'it was' if len(unread) == 1 else 'they were'}: {named}. Every line is new where it now is, but Stratum didn't have "
            "the text, so the checks read only the lines the diff marks as added."
        ),
        severity="low",
        category="security",
        suggestion="Review these files as if they were added in this PR.",
        source="check",
        rule="checks/moved-unread",
        confidence=1.0,
    )]
