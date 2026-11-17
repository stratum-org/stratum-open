"""CODEOWNERS: who owns each changed file, read the way GitHub reads it.

- GitHub uses the first CODEOWNERS it finds in `.github/`, the root, then
  `docs/`.
- In it, the last line that matches a path wins, and a pattern with no owners
  takes ownership away again.
- Patterns follow .gitignore's rules:
  - one with a slash at its start or in its middle is anchored to the root;
  - a trailing slash means a folder;
  - `*` stays inside one folder and `**` crosses them;
  - a match on a folder covers everything inside it, except that `dir/*`
    covers only the files directly in `dir`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

from stratum_open.util import count

LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
MAX_BYTES = 3 * 1024 * 1024  # GitHub ignores a larger CODEOWNERS
MAX_GROUPS = 3  # owner groups named in the note


@dataclass(frozen=True)
class Rule:
    pattern: str
    owners: tuple[str, ...]  # @user, @org/team or an email; () takes ownership away
    regex: re.Pattern


def read(checkout: str) -> str | None:
    """The CODEOWNERS GitHub would use in this checkout, if there is one."""
    for location in LOCATIONS:
        full = os.path.join(checkout, *location.split("/"))
        try:
            if os.path.isfile(full) and not os.path.islink(full) and os.path.getsize(full) <= MAX_BYTES:
                with open(full, encoding="utf-8", errors="replace") as handle:
                    return handle.read()
        except OSError:
            continue
    return None


def parse(text: str) -> list[Rule]:
    rules = []
    for raw in text.splitlines():
        line = re.split(r"(?<!\\)#", raw, maxsplit=1)[0].strip()  # "#" starts a comment unless escaped
        if not line:
            continue
        pattern, *owners = line.split()
        try:
            rules.append(Rule(pattern, tuple(o for o in owners if "@" in o), _compile(pattern.replace("\\#", "#"))))
        except re.error:
            continue  # GitHub skips a line it can't read, too
    return rules


def owners_for(rules: list[Rule], path: str) -> tuple[str, ...]:
    """The owners of `path`: the last matching rule's, () when none match."""
    for rule in reversed(rules):
        if rule.regex.match(path):
            return rule.owners
    return ()


def owners_note(rules: list[Rule], paths: Iterable[str]) -> list[str]:
    """One line saying who owns the changed files, owners in code style so
    nobody is notified by it."""
    groups: dict[tuple[str, ...], list[str]] = {}
    for path in sorted(set(paths)):
        groups.setdefault(owners_for(rules, path), []).append(path)
    owned = sorted(((o, p) for o, p in groups.items() if o), key=lambda item: (-len(item[1]), item[0]))
    if not owned:
        return []
    parts = []
    for owners, paths_ in owned[:MAX_GROUPS]:
        names = " and ".join(f"`{o}`" for o in owners)
        more = f" and {len(paths_) - 1} more" if len(paths_) > 1 else ""
        parts.append(f"{names} for `{paths_[0]}`{more}")
    if len(owned) > MAX_GROUPS:
        parts.append(count(len(owned) - MAX_GROUPS, "more owner group"))
    unowned = groups.get((), [])
    tail = f" No owner is listed for {len(unowned)} of the changed files." if unowned else ""
    return ["CODEOWNERS: " + "; ".join(parts) + "." + tail]


def _compile(pattern: str) -> re.Pattern:
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    folder = pattern.endswith("/")
    body = pattern.strip("/")
    direct = not folder and body.endswith("/*") and not body.endswith("/**")  # src/*/ names folders, so it covers their contents
    out, i = [], 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    prefix = "" if anchored else "(?:.*/)?"
    suffix = "" if direct else "/.*" if folder else "(?:/.*)?"
    return re.compile(f"^{prefix}{''.join(out)}{suffix}$")
