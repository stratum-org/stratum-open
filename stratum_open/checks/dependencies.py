"""Dependencies a PR adds, and whether each one is the package it seems to be.

A dependency is new when its name is on an added line of a manifest, and on
no removed line of any manifest of the same ecosystem nor on another line of
the same file. So bumping a version, or moving a package between sections or
files, doesn't count. Packages from a local path or a workspace aren't
third-party and aren't reported; ones from git or a URL are, but get no
registry or look-alike checks. Go's `// indirect` requirements are left out:
they come with another module.

Manifests are read line by line, so a line's section is known only when the
diff (or the file's full content, from `contents`) shows the section's
header. Without the header a line counts only if it looks unmistakably like
a dependency:

- `package.json`: a `"name": "^1.2.3"`-style line (a version range, a tag
  like `latest`, or `npm:`/`git+`/`github:` and the like) whose key isn't a
  known top-level field. A `devDependencies` entry and a `resolutions` entry
  look the same, so the second can be taken for a dependency.
- `pyproject.toml`: a quoted requirement with a version, extras or marker,
  such as `"httpx>=0.27"`. A bare `"httpx"` isn't told apart from a keyword.
  Poetry tables are read only when their header is shown.
- `Cargo.toml`: `name = "1.2"` or `name = { version = ... }`.
- `go.mod`: a module path and version.

With `dependencies.known_vulnerabilities` on, every dependency an added line
sets to one exact version, new or bumped, is also looked up in the OSV
database (osv.dev), and gets a finding when that version has published
advisories. Exact means `name==1.2.3` in a requirements file or
pyproject.toml, a bare `1.2.3` in a Poetry table or package.json, `=1.2.3`
in Cargo.toml (a bare version there is a range) and a go.mod requirement. A
range has no single answer, so it gets no finding. The finding's severity is
the highest OSV labels any of the advisories with; when it labels none, the
finding is medium.

The only thing sent to a registry is a dependency's name. OSV is sent the
name, the ecosystem and the version.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from stratum_open import __version__
from stratum_open.checks.common import basename, checked_lines, view
from stratum_open.checks.popular import BY_ECOSYSTEM
from stratum_open.diff import FileDiff
from stratum_open.findings import SEVERITIES, Finding
from stratum_open.policy import Policy

REGISTRY_NAMES = {"pypi": "PyPI", "npm": "npm", "crates": "crates.io", "go": "the Go module proxy"}
LOOKUP_ECOSYSTEMS = ("pypi", "npm", "crates")  # Go modules have no first-published date to ask for
MAX_LOOKUPS = 30  # registry calls per run; a PR adding more gets the rest unchecked
USER_AGENT = f"stratum-open/{__version__} (dependency check)"
OSV = "https://api.osv.dev/v1"
OSV_ECOSYSTEMS = {"pypi": "PyPI", "npm": "npm", "crates": "crates.io", "go": "Go"}
MAX_PINS = 100  # pinned versions OSV is asked about per run, in one request; the rest go unchecked
MAX_VULNERABLE = 20  # of those, how many have their advisories read, a request each
MAX_ADVISORIES_NAMED = 3
UNRATED = "medium"  # a finding's severity when OSV gives none of the advisories one


@dataclass
class Dependency:
    name: str  # as written in the manifest
    ecosystem: str  # pypi | npm | crates | go
    path: str
    line: int
    from_registry: bool = True  # False for git or URL sources

    @property
    def key(self) -> str:
        return normalise(self.name, self.ecosystem)


def normalise(name: str, ecosystem: str) -> str:
    """The form each registry compares names in: PEP 503 for PyPI, and
    crates.io treats `-` and `_` as the same."""
    if ecosystem == "pypi":
        return re.sub(r"[-_.]+", "-", name).lower()
    if ecosystem == "crates":
        return name.lower().replace("_", "-")
    if ecosystem == "npm":
        return name.lower()
    return name


def check(
    files: list[FileDiff],
    policy: Policy,
    *,
    lookup: Any = None,
    contents: dict[str, str] | None = None,
    now: datetime | None = None,
) -> list[Finding]:
    """`lookup` is a `PackageLookup` (or anything with its `info` method, and
    its `advisories` method for the known-vulnerability findings); None
    means no registry calls."""
    now = now or datetime.now(timezone.utc)
    findings: list[Finding] = []
    asked: set[tuple[str, str]] = set()
    for dep in find_new(files, contents):
        findings.append(_new(dep, policy))
        if not dep.from_registry:
            continue
        similar = lookalike(dep.name, dep.ecosystem)
        if similar:
            findings.append(_lookalike(dep, similar))
        if lookup is None or dep.ecosystem not in LOOKUP_ECOSYSTEMS:
            continue
        if (dep.ecosystem, dep.key) not in asked and len(asked) >= MAX_LOOKUPS:
            continue
        asked.add((dep.ecosystem, dep.key))
        try:
            info = lookup.info(dep.ecosystem, dep.name)
        except Exception:  # a lookup problem means "unknown", never a failed review
            info = None
        if info is None:
            continue
        if not info.exists:
            findings.append(_missing(dep))
        elif info.first_published is not None:
            days = (now - info.first_published).days
            if days < policy.dependencies.min_package_age_days:
                findings.append(_young(dep, days))
    # registry_lookup is the consent for any lookup, so OSV needs both keys.
    if lookup is not None and policy.dependencies.registry_lookup and policy.dependencies.known_vulnerabilities:
        findings.extend(_known_vulnerable(files, contents, lookup))
    return findings


def _known_vulnerable(files: list[FileDiff], contents: dict[str, str] | None, lookup: Any) -> list[Finding]:
    ask = getattr(lookup, "advisories", None)  # a lookup written before this check has no such method
    pinned = find_pinned(files, contents) if ask is not None else []
    if not pinned:
        return []
    wanted = list(dict.fromkeys((dep.ecosystem, dep.name, version) for dep, version in pinned))[:MAX_PINS]
    try:
        answers = ask(wanted)
    except Exception:  # as for the registries: a lookup problem means "unknown"
        return []
    if not isinstance(answers, dict):
        return []
    findings = []
    for dep, version in pinned:
        listed = answers.get((dep.ecosystem, dep.name, version))
        if isinstance(listed, Advisories) and listed.ids:
            findings.append(_vulnerable(dep, version, listed))
    return findings


def _finding(dep: Dependency, rule: str, title: str, detail: str, suggestion: str, severity: str,
             needs_human: bool, confidence: float) -> Finding:
    return Finding(
        title=title, detail=detail, severity=severity, category="supply-chain", path=dep.path,
        line_start=dep.line, line_end=dep.line, suggestion=suggestion, source="check", rule=rule,
        confidence=confidence, needs_human=needs_human,
    )


def _new(dep: Dependency, policy: Policy) -> Finding:
    where = REGISTRY_NAMES[dep.ecosystem] if dep.from_registry else "a git repository or URL"
    detail = (
        f"This PR adds `{dep.name}` (from {where}) as a dependency. Every new package is someone else's "
        "code running in this project, and its own dependencies come with it."
    )
    if not dep.from_registry:
        detail += " Code from git or a URL isn't checked by any registry and can change without a new version."
    return _finding(
        dep, "deps/new", f"New dependency: `{dep.name}`", detail,
        "Check it's needed, maintained, and published by someone you trust.",
        "low", policy.always_human.new_dependencies, 1.0,
    )


def _lookalike(dep: Dependency, similar: str) -> Finding:
    registry = REGISTRY_NAMES[dep.ecosystem]
    return _finding(
        dep, "deps/lookalike", f"`{dep.name}` looks like `{similar}`",
        (
            f"`{dep.name}` is a near-copy of the name `{similar}`, one of the most-used packages on {registry}, "
            "but it's a different package. Names one typo away from a popular one are a common way to get "
            "malware installed (typosquatting)."
        ),
        f"If you meant `{similar}`, fix the name. If `{dep.name}` is intended, a maintainer should confirm it's the right package.",
        "high", True, 0.6,
    )


def _missing(dep: Dependency) -> Finding:
    registry = REGISTRY_NAMES[dep.ecosystem]
    return _finding(
        dep, "deps/missing", f"`{dep.name}` isn't on {registry}",
        (
            f"No package called `{dep.name}` exists on {registry}. It may be a typo, or a private package that "
            "needs its own index. It may also be a name an AI assistant made up, which anyone could register "
            'with malicious code for the next person who installs it ("slopsquatting").'
        ),
        "Check the name. If it's private, make sure installs can only ever come from your own index.",
        "high", True, 0.9,
    )


def _young(dep: Dependency, days: int) -> Finding:
    registry = REGISTRY_NAMES[dep.ecosystem]
    age = "today" if days <= 0 else "1 day ago" if days == 1 else f"{days} days ago"
    return _finding(
        dep, "deps/young", f"`{dep.name}` is a brand-new package",
        (
            f"`{dep.name}` was first published to {registry} {age}. Most malicious packages are caught and "
            "removed within their first weeks, so a new one hasn't had that scrutiny yet."
        ),
        "Check who publishes it and that it's the package you meant, or wait until it has some history.",
        "high", True, 0.9,
    )


def _vulnerable(dep: Dependency, version: str, listed: Advisories) -> Finding:
    count = len(listed.ids)
    one = count == 1
    named = ", ".join(listed.ids[:MAX_ADVISORIES_NAMED]) + (f" and {count - MAX_ADVISORIES_NAMED} more" if count > MAX_ADVISORIES_NAMED else "")
    severity = listed.severity if listed.severity in SEVERITIES else None
    detail = (
        f"This PR sets `{dep.name}` to {version}, and the OSV database (osv.dev) lists "
        f"{'a security advisory' if one else f'{count} security advisories'} for that version: {named}. "
    )
    if severity:
        detail += f"{'It is' if one else 'The most severe is'} rated {severity}. "
    else:
        detail += f"OSV gives {'it' if one else 'them'} no severity. "
    if listed.fixed:
        detail += f"{listed.fixed} is the first version with {'it' if one else 'all of them'} fixed."
        suggestion = f"Use {listed.fixed}, or a later release that has {'the fix' if one else 'the fixes'}."
    else:
        detail += f"OSV names no version with {'it' if one else 'all of them'} fixed."
        suggestion = (
            f"Check whether {'it applies' if one else 'they apply'} to how this project uses `{dep.name}`, "
            "and pick another version or package if so."
        )
    return _finding(
        dep, "deps/vulnerable", f"`{dep.name}` {version} has {'a known vulnerability' if one else 'known vulnerabilities'}",
        detail, suggestion, severity or UNRATED, False, 0.9,
    )


# ── finding the new ones ─────────────────────────────────────────────────

_REQUIREMENTS = re.compile(r"(?:[\w.-]*[-_.])?requirements(?:[-_.][\w.-]*)?\.(?:txt|in)")
_IN_REQUIREMENTS_DIR = re.compile(r"(?:^|/)requirements/[^/]+\.(?:txt|in)$")
_ECOSYSTEM = {"requirements": "pypi", "pyproject": "pypi", "npm": "npm", "cargo": "crates", "go": "go"}


def manifest_kind(path: str) -> str | None:
    name = basename(path).lower()
    if name == "package.json":
        return "npm"
    if name == "pyproject.toml":
        return "pyproject"
    if name == "cargo.toml":
        return "cargo"
    if name == "go.mod":
        return "go"
    if _REQUIREMENTS.fullmatch(name) or _IN_REQUIREMENTS_DIR.search(path.lower()):
        return "requirements"
    return None


def scope(path: str) -> frozenset[str]:
    """The kind of manifest at `path`, if it's one."""
    kind = manifest_kind(path)
    return frozenset({kind}) if kind else frozenset()


def find_new(files: list[FileDiff], contents: dict[str, str] | None = None) -> list[Dependency]:
    """A file renamed to a manifest from a path that wasn't one of its kind
    (`examples/deps.txt` to `requirements.txt`) counts every line as added."""
    manifests = [(file, kind) for file in files if (kind := manifest_kind(file.path)) and not file.binary]
    removed: dict[str, set[str]] = {}
    for file, kind in manifests:
        ecosystem = _ECOSYSTEM[kind]
        removed.setdefault(ecosystem, set()).update(
            normalise(name, ecosystem) for text in file.removed() for name in _loose_names(kind, text)
        )
    found: list[Dependency] = []
    for file, kind in manifests:
        ecosystem = _ECOSYSTEM[kind]
        added = {number for number, _ in checked_lines(file, contents, scope)}
        entries = _SCANNERS[kind](view(file, contents))
        elsewhere = {normalise(name, ecosystem) for number, name, _ in entries if number not in added}
        seen: set[str] = set()
        for number, name, source in entries:
            key = normalise(name, ecosystem)
            if number not in added or source == "local" or key in seen:
                continue
            seen.add(key)
            if key in removed[ecosystem] or key in elsewhere:
                continue
            found.append(Dependency(name, ecosystem, file.path, number, from_registry=source == "registry"))
    return found


# Each scanner returns (line, name, source) for every dependency line it can
# see, added or not. source: "registry", "remote" (git or URL) or "local".
Entry = tuple[int, str, str]
# One alternative per quote, with the backslash kept out of the plain-character class, so a
# backslash is consumed one way only: the ambiguous form backtracks exponentially on long lines.
_STRING = re.compile(r"""\"((?:\\.|[^"\\])*)\"|'((?:\\.|[^'\\])*)'""")
_NAME = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(\[[^\]]*\])?\s*(.*)$", re.S)


def _quoted(match: re.Match) -> str:
    """The body of a `_STRING` match, whichever quote it used."""
    body = match.group(1)
    return body if body is not None else match.group(2)


def _requirement(text: str, strict: bool = False) -> tuple[str, str] | None:
    """(name, source) from a PEP 508 requirement. `strict` asks for a version,
    extras or marker as well, to tell a requirement from any other string."""
    text = re.sub(r"(?:^|\s)#.*$", "", text.lstrip("\ufeff")).strip()
    if not text or text.startswith(("-", ".", "/", "~")) or re.match(r"^[\w+.-]+://", text):
        return None
    match = _NAME.match(text)
    if not match:
        return None
    rest = match.group(3).strip()
    if rest and not re.match(r"^(?:[<>=!~;@,(]|\\$)", rest):
        return None
    if strict and not (rest or match.group(2)):
        return None
    return match.group(1), "remote" if rest.startswith("@") else "registry"


def _scan_requirements(lines: list[tuple[int, str]]) -> list[Entry]:
    entries = []
    for number, text in lines:
        requirement = _requirement(text)
        if requirement:
            entries.append((number, *requirement))
    return entries


# TOML, read a line at a time.
_TABLE = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_-].*?)\s*\]\]?\s*(?:#.*)?$")
_TOML_KEY = re.compile(r"""^\s*(?P<key>[A-Za-z0-9_.-]+|"[^"]*"|'[^']*')\s*=\s*(?P<value>.*)$""")
_HIDDEN = "?"  # a section or array whose start is in a gap the diff doesn't show


def _bare(text: str) -> str:
    """A line with its strings emptied and its comment cut, to count brackets."""
    return re.sub(r"\s*#.*$", "", _STRING.sub('""', text))


def _toml_depth(text: str) -> int:
    bare = _bare(text)
    return bare.count("[") + bare.count("{") - bare.count("]") - bare.count("}")


def _toml_lines(lines: list[tuple[int, str]]):
    """Walk TOML lines as (number, text, table, array_key, key, value):
    `table` is None when a gap hides the header, `array_key` names the
    multi-line array the line sits in (`_HIDDEN` when that's hidden too),
    and key/value are set on `key = value` lines."""
    table: str | None = ""
    array: str | None = None
    depth = 0
    previous: int | None = None
    for number, text in lines:
        if number != (previous or 0) + 1:
            table, array, depth = None, _HIDDEN, 0
        previous = number
        if array == _HIDDEN and (_TABLE.match(text) or _TOML_KEY.match(text)):
            array = None  # keys and headers can't be inside an array
        if array is not None:
            yield number, text, table, array, None, None
            depth += _toml_depth(text)
            if (array == _HIDDEN and "]" in _bare(text)) or (array != _HIDDEN and depth <= 0):
                array, depth = None, 0
            continue
        header = _TABLE.match(text)
        if header:
            table = re.sub(r"\s*\.\s*", ".", header.group(1)).replace('"', "").replace("'", "")
            yield number, text, table, None, None, None
            continue
        key_line = _TOML_KEY.match(text)
        if key_line:
            key = key_line.group("key").strip("\"'")
            value = key_line.group("value")
            yield number, text, table, None, key, value
            if value.lstrip().startswith("["):
                depth = _toml_depth(value)
                if depth > 0:
                    array = key
            continue
        yield number, text, table, None, None, None


def _pyproject_deps_array(table: str | None, key: str | None) -> bool:
    if table is None:
        return key in ("dependencies", "requires", "dev-dependencies")
    return (
        (table == "project" and key == "dependencies")
        or table in ("project.optional-dependencies", "dependency-groups", "tool.pdm.dev-dependencies")
        or (table == "build-system" and key == "requires")
        or (table == "tool.uv" and key == "dev-dependencies")
    )


_POETRY_TABLE = re.compile(r"tool\.poetry\.(?:dependencies|dev-dependencies|group\.[^.]+\.dependencies)")


def _scan_pyproject(lines: list[tuple[int, str]]) -> list[Entry]:
    entries = []
    for number, text, table, array, key, value in _toml_lines(lines):
        if array is not None:
            # Inside a multi-line array: its items, if it's one of dependencies.
            # When even the array's key is hidden, only clear requirements count.
            hidden = array == _HIDDEN
            if hidden or _pyproject_deps_array(table, array):
                entries.extend(_strings_as_requirements(number, text, strict=hidden))
        elif key is not None and value.lstrip().startswith("["):
            if _pyproject_deps_array(table, key):
                entries.extend(_strings_as_requirements(number, value, strict=False))
        elif key is not None and table and _POETRY_TABLE.fullmatch(table) and key.lower() != "python":
            entries.append((number, key, _inline_source(value)))
    return entries


_TOML_STRING_OR_COMMENT = re.compile(_STRING.pattern + r"|(?P<comment>#)")


def _strings_as_requirements(number: int, text: str, strict: bool) -> list[Entry]:
    found = []
    for match in _TOML_STRING_OR_COMMENT.finditer(text):
        if match.group("comment"):
            break
        if text[: match.start()].rstrip().endswith("="):
            continue  # a value in an inline table, like {include-group = "test"}
        requirement = _requirement(_quoted(match), strict=strict)
        if requirement:
            found.append((number, *requirement))
    return found


def _inline_source(value: str) -> str:
    """Where a Poetry or Cargo entry comes from, by the keys of its inline table."""
    if re.search(r"\b(?:path|workspace)\s*=", value):
        return "local"
    if re.search(r"\b(?:git|url)\s*=", value):
        return "remote"
    return "registry"


_CARGO_TABLE = re.compile(r"(?:target\..+\.|workspace\.)?(?:dev-|build-)?dependencies")
_CARGO_SUBTABLE = re.compile(r"(?:target\..+\.|workspace\.)?(?:dev-|build-)?dependencies\.([A-Za-z0-9_-]+)")
_CARGO_FIELDS = {
    "name", "version", "edition", "rust-version", "authors", "description", "license", "license-file",
    "repository", "homepage", "documentation", "readme", "keywords", "categories", "publish", "build",
    "links", "exclude", "include", "resolver", "members", "default-members", "workspace", "autobins",
    "opt-level", "debug", "lto", "codegen-units", "panic", "strip", "incremental", "overflow-checks",
}
_CARGO_VERSION = re.compile(r"""^\s*(?P<q>["'])(?:[\^~=]|[<>]=?)?\s*(?:\d+(?:\.\d+){0,2}(?:-[\w.]+)?|\*)(?:\s*,.*)?(?P=q)""")


def _scan_cargo(lines: list[tuple[int, str]]) -> list[Entry]:
    entries: list[Entry] = []
    subtable: int | None = None  # index in entries of the `[dependencies.name]` being read
    for number, text, table, array, key, value in _toml_lines(lines):
        if array is not None:
            continue
        if key is None:
            sub = _CARGO_SUBTABLE.fullmatch(table or "") if _TABLE.match(text) else None
            if sub:
                entries.append((number, sub.group(1), "registry"))
                subtable = len(entries) - 1
            elif _TABLE.match(text):
                subtable = None
            continue
        if subtable is not None and table and _CARGO_SUBTABLE.fullmatch(table):
            line, name, source = entries[subtable]
            if key == "package":
                quoted = _STRING.search(value)
                name = _quoted(quoted) if quoted else name
            elif key in ("path", "workspace"):
                source = "local"
            elif key == "git" and source != "local":
                source = "remote"
            entries[subtable] = (line, name, source)
            continue
        if table is not None and not _CARGO_TABLE.fullmatch(table):
            continue
        if table is None and (key in _CARGO_FIELDS or not (_CARGO_VERSION.match(value) or re.search(r"\b(?:version|git)\s*=", value))):
            continue
        name, _, field = key.partition(".")  # a dotted key: serde.workspace = true
        if value.lstrip().startswith("{"):
            package = re.search(r"""\bpackage\s*=\s*["']([^"']+)["']""", value)
            name, source = (package.group(1) if package else name), _inline_source(value)
        else:
            source = {"path": "local", "workspace": "local", "git": "remote"}.get(field, "registry")
        entries.append((number, name, source))
    return entries


# package.json
_JSON_KEY = re.compile(r'^\s*"((?:\\.|[^"\\])*)"\s*:\s*(.*)$')
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_NPM_SECTIONS = {"dependencies", "devDependencies", "peerDependencies", "optionalDependencies"}
_NPM_NAME = re.compile(r"(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*")
_NPM_SPEC = re.compile(
    r"^(?:(?:[\^~]|[<>]=?|=)?\s*v?\d+(?:\.(?:\d+|[xX*]))*(?:[-+][\w.-]+)?(?:\s.*)?|[*xX]|latest|next|beta|alpha|canary|rc"
    r"|(?:workspace|npm|file|link|portal|patch|catalog|git|git\+[a-z]+|github|gitlab|bitbucket):.*)$"
)
_NPM_FIELDS = {
    "name", "version", "description", "main", "module", "browser", "types", "typings", "type", "license",
    "author", "homepage", "private", "node", "npm", "yarn", "pnpm", "bun", "deno", "vscode", "tag", "access",
    "registry",
}


def _scan_package_json(lines: list[tuple[int, str]]) -> list[Entry]:
    entries = []
    stack: list[str] = []  # the key of each open object; "" for the root
    previous: int | None = None
    for number, text in lines:
        if number != (previous or 0) + 1:
            stack = [_HIDDEN]  # a gap: where this line sits is hidden
        previous = number
        key_line = _JSON_KEY.match(text)
        if key_line and stack:
            key, value = key_line.group(1), key_line.group(2).strip().rstrip(",").strip()
            spec = value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else None
            section = stack[-1]
            if spec is not None and (
                (section in _NPM_SECTIONS and len(stack) == 2)
                or (section == _HIDDEN and key not in _NPM_FIELDS and _NPM_NAME.fullmatch(key) and _NPM_SPEC.match(spec))
            ):
                entries.append((number, *_npm_entry(key, spec)))
        opened = key_line.group(1) if key_line else ""
        for char in _JSON_STRING.sub('""', text):
            if char in "{[":
                stack.append(opened)
                opened = ""
            elif char in "}]" and stack:
                stack.pop()
                if not stack:
                    stack = [_HIDDEN]  # closed past what's shown
    return entries


def _npm_entry(key: str, spec: str) -> tuple[str, str]:
    if spec.startswith("npm:"):  # an alias: "lodash4": "npm:lodash@^4"
        target = spec[4:]
        return (target.rsplit("@", 1)[0] if "@" in target.lstrip("@") else target), "registry"
    if spec.startswith(("workspace:", "file:", "link:", "portal:", "catalog:")):
        return key, "local"
    if re.match(r"^(?:git|git\+[a-z]+|github|gitlab|bitbucket|https?):|^[\w.-]+/[\w.-]+(?:#.*)?$", spec):
        return key, "remote"
    return key, "registry"


# go.mod
_GO_BLOCK = re.compile(r"^\s*(?P<keyword>require|replace|exclude|retract|tool|godebug)\s*\(\s*(?://.*)?$")
_GO_REQUIRE = re.compile(r"^\s*require\s+(?P<path>\S+)\s+(?P<version>v\S+)(?P<rest>.*)$")
_GO_ENTRY = re.compile(r"^\s*(?P<path>[^\s/()]+\.[^\s/()]+(?:/\S*)?)\s+(?P<version>v\d\S*)(?P<rest>.*)$")


def _scan_go_mod(lines: list[tuple[int, str]]) -> list[Entry]:
    entries = []
    block: str | None = None
    previous: int | None = None
    for number, text in lines:
        if number != (previous or 0) + 1:
            block = _HIDDEN
        previous = number
        if text.strip() == ")":
            block = None
            continue
        opened = _GO_BLOCK.match(text)
        if opened:
            block = opened.group("keyword")
            continue
        match = _GO_REQUIRE.match(text) if block in (None, _HIDDEN) else None
        if match is None and block in ("require", _HIDDEN):
            match = _GO_ENTRY.match(text)
        if match and "=>" not in match.group("rest") and "// indirect" not in match.group("rest"):
            entries.append((number, match.group("path"), "registry"))
    return entries


_SCANNERS = {
    "requirements": _scan_requirements,
    "pyproject": _scan_pyproject,
    "npm": _scan_package_json,
    "cargo": _scan_cargo,
    "go": _scan_go_mod,
}


def _loose_names(kind: str, text: str) -> list[str]:
    """Every name a removed line could have been a dependency entry for. The
    old side's sections aren't known, so this errs towards finding a name:
    a name it finds wrongly only keeps an added line from counting as new."""
    if kind == "requirements":
        requirement = _requirement(text)
        return [requirement[0]] if requirement else []
    if kind == "npm":
        key = _JSON_KEY.match(text)
        if not key:
            return []
        alias = re.search(r'"npm:(@?[^@"]+)', text)
        return [key.group(1)] + ([alias.group(1)] if alias else [])
    if kind == "go":
        match = _GO_REQUIRE.match(text) or _GO_ENTRY.match(text)
        return [match.group("path")] if match else []
    names = [requirement[0] for match in _STRING.finditer(text) if (requirement := _requirement(_quoted(match)))]
    key = _TOML_KEY.match(text)
    if key:
        names.append(key.group("key").strip("\"'"))
        package = re.search(r"""\bpackage\s*=\s*["']([^"']+)["']""", text)
        if package:
            names.append(package.group(1))
    header = _TABLE.match(text)
    if header:
        names.append(header.group(1).rsplit(".", 1)[-1])
    return names


# ── exact versions ───────────────────────────────────────────────────────


def find_pinned(files: list[FileDiff], contents: dict[str, str] | None = None) -> list[tuple[Dependency, str]]:
    """Each registry dependency a checked line sets to one exact version,
    with that version: new ones and bumps alike."""
    found: list[tuple[Dependency, str]] = []
    for file in files:
        kind = manifest_kind(file.path)
        if not kind or file.binary:
            continue
        ecosystem = _ECOSYSTEM[kind]
        lines = view(file, contents)
        text = dict(lines)
        added = {number for number, _ in checked_lines(file, contents, scope)}
        seen: set[tuple[str, str]] = set()
        for number, name, source in _SCANNERS[kind](lines):
            if number not in added or source != "registry":
                continue
            version = _exact_version(kind, text[number], name)
            if version and (normalise(name, ecosystem), version) not in seen:
                seen.add((normalise(name, ecosystem), version))
                found.append((Dependency(name, ecosystem, file.path, number), version))
    return found


# OSV matches a version it can't read loosely (`banana` gets advisories), so
# only well-formed versions are sent: PEP 440 for PyPI, full semver elsewhere.
_PEP440 = re.compile(
    r"^v?(?:(\d+)!)?(\d+(?:\.\d+)*)"
    r"(?:[-_.]?(a|b|c|rc|alpha|beta|pre|preview)[-_.]?(\d*))?"
    r"(?:-(\d+)|[-_.]?(?:post|rev|r)[-_.]?(\d*))?"
    r"(?:[-_.]?dev[-_.]?(\d*))?"
    r"(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?$",
    re.I,
)
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_PRE = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}
_PIN = re.compile(r"^==\s*([\w.!+-]+)(?:\s*;.*|\s*\\|\s+--.*)?$", re.S)


def version_key(version: str, ecosystem: str) -> tuple | None:
    """A key that sorts versions of one ecosystem as the ecosystem does (PEP
    440, or semver); None for text that isn't a whole version."""
    if ecosystem == "pypi":
        match = _PEP440.match(version)
        if not match:
            return None
        epoch, release, pre, pre_number, post_bare, post, dev = match.groups()
        numbers = [int(part) for part in release.split(".")]
        while len(numbers) > 1 and numbers[-1] == 0:
            numbers.pop()  # 2.0 and 2.0.0 are the same version
        post = post_bare if post_bare is not None else post
        if pre is not None:
            before = (1, _PRE[pre.lower()], int(pre_number or 0))
        else:  # 1.0.dev1 comes before 1.0a1, and both before 1.0
            before = (0,) if dev is not None and post is None else (2,)
        return (
            int(epoch or 0), tuple(numbers), before,
            (0,) if post is None else (1, int(post or 0)),
            (1,) if dev is None else (0, int(dev or 0)),
        )
    match = _SEMVER.match(version)
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    if pre is None:
        return int(major), int(minor), int(patch), (1,)
    # A pre-release comes before its release; numeric parts sort as numbers, below the rest.
    return int(major), int(minor), int(patch), (0, tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split(".")))


def _exact_version(kind: str, text: str, name: str) -> str | None:
    """The one version a manifest line sets dependency `name` to, or None
    when the line gives a range, a tag or nothing."""
    ecosystem = _ECOSYSTEM[kind]
    version: str | None = None
    if kind == "requirements":
        version = _pinned(text)
    elif kind == "pyproject":
        for match in _STRING.finditer(text):
            requirement = _requirement(_quoted(match))
            if requirement and requirement[0] == name:
                version = _pinned(_quoted(match))
                break
        else:  # a Poetry entry, where a bare version means exactly that version
            spec = _toml_version(text)
            version = re.sub(r"^==?\s*", "", spec) if spec else None
    elif kind == "npm":
        key_line = _JSON_KEY.match(text)
        spec = key_line.group(2).strip().rstrip(",").strip().strip('"') if key_line else ""
        if spec.startswith("npm:"):  # an alias: the version is after the target's name
            spec = spec[4:].rpartition("@")[2] if "@" in spec[4:].lstrip("@") else ""
        version = re.sub(r"^=?\s*v?", "", spec)
    elif kind == "cargo":
        spec = _toml_version(text)
        version = spec[1:].strip() if spec and spec.startswith("=") else None  # a bare "1.2.3" is ^1.2.3
    elif kind == "go":
        match = _GO_REQUIRE.match(text) or _GO_ENTRY.match(text)
        version = match.group("version").removeprefix("v") if match else None
    return version if version and version_key(version, ecosystem) is not None else None


def _pinned(text: str) -> str | None:
    """The version of a PEP 508 requirement that is `name==version` and nothing else."""
    match = _NAME.match(re.sub(r"(?:^|\s)#.*$", "", text.lstrip("\ufeff")).strip())
    pin = _PIN.match(match.group(3).strip()) if match else None
    return pin.group(1) if pin else None


def _toml_version(text: str) -> str | None:
    """The version text of a `name = "..."` or `name = { version = "..." }` line."""
    key_line = _TOML_KEY.match(text)
    if not key_line:
        return None
    value = key_line.group("value").lstrip()
    found = re.search(r"""\bversion\s*=\s*["']([^"']*)["']""", value) if value.startswith("{") else re.match(r"""["']([^"']*)["']""", value)
    return found.group(1).strip() if found else None


# ── look-alike names ─────────────────────────────────────────────────────

_POPULAR = {
    ecosystem: {normalise(name, ecosystem): name for name in names} for ecosystem, names in BY_ECOSYSTEM.items()
}
_SEPARATORS = re.compile(r"[-_.]")
_PARTS = re.compile(r"[-_./]")


def lookalike(name: str, ecosystem: str) -> str | None:
    """The popular package `name` is probably a typo of, if any.

    Names are compared a part at a time (split on `-_./`), so plugin families
    like `pytest-env` and `pytest-cov` aren't taken for each other: only
    names that differ in one part count, and that part has to be within one
    edit of the popular one's (two if it's 8+ characters long), or have two
    neighbouring characters swapped. Parts under five characters count only
    for a swap. Names that differ only in `-_.` always count."""
    popular = _POPULAR.get(ecosystem)
    if not popular:
        return None
    key = normalise(name, ecosystem)
    if key in popular:
        return None
    squashed = _SEPARATORS.sub("", key)
    for candidate, shown in popular.items():
        if _SEPARATORS.sub("", candidate) == squashed:
            return shown
    parts = _PARTS.split(key)
    for candidate, shown in popular.items():
        theirs = _PARTS.split(candidate)
        if len(theirs) != len(parts):
            continue
        differing = [(mine, other) for mine, other in zip(parts, theirs, strict=True) if mine != other]
        if len(differing) == 1 and _close(*differing[0]):
            return shown
    return None


def _close(mine: str, theirs: str) -> bool:
    if min(len(mine), len(theirs)) < 3:
        return False
    if _swapped(mine, theirs):
        return True
    limit = 2 if len(theirs) >= 8 else 1 if len(theirs) >= 5 else 0
    return limit > 0 and abs(len(mine) - len(theirs)) <= limit and _distance(mine, theirs) <= limit


def _swapped(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    differing = [i for i in range(len(a)) if a[i] != b[i]]
    return len(differing) == 2 and differing[1] == differing[0] + 1 and a[differing[0]] == b[differing[1]] and a[differing[1]] == b[differing[0]]


def _distance(a: str, b: str) -> int:
    """Edit distance, counting a swap of neighbours as one edit."""
    rows = [list(range(len(b) + 1))]
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            row[j] = min(rows[-1][j] + 1, row[j - 1] + 1, rows[-1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], rows[-2][j - 2] + 1)
        rows.append(row)
    return rows[-1][-1]


# ── registries ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PackageInfo:
    exists: bool
    first_published: datetime | None = None  # None when the registry doesn't say


@dataclass(frozen=True)
class Advisories:
    """What OSV lists for one version of a package."""
    ids: tuple[str, ...]  # one per advisory, the most severe first
    severity: str | None = None  # the highest OSV labels any with: low | medium | high | critical
    fixed: str | None = None  # the first version with all of them fixed, when OSV's ranges say


class PackageLookup:
    """Asks PyPI, npm and crates.io whether a package exists and when it was
    first published. Only the name is sent. Answers are kept for the run, and
    anything that goes wrong (no network, a timeout, a rate limit, a reply it
    can't read) is "unknown": `info` returns None, never raises.

    `advisories` asks OSV about pinned versions, and is as forgiving."""

    def __init__(self, session: Any = None, timeout: float = 10.0):
        self.session = session or requests.Session()
        self.timeout = timeout
        self._cache: dict[tuple[str, str], PackageInfo | None] = {}

    def info(self, ecosystem: str, name: str) -> PackageInfo | None:
        key = (ecosystem, normalise(name, ecosystem))
        if key not in self._cache:
            self._cache[key] = self._fetch(ecosystem, name)
        return self._cache[key]

    def _fetch(self, ecosystem: str, name: str) -> PackageInfo | None:
        if ecosystem == "pypi":
            url = f"https://pypi.org/pypi/{quote(normalise(name, 'pypi'), safe='')}/json"
        elif ecosystem == "npm":
            url = f"https://registry.npmjs.org/{quote(name, safe='@')}"  # @scope/name -> @scope%2Fname
        elif ecosystem == "crates":
            url = f"https://crates.io/api/v1/crates/{quote(name, safe='')}"
        else:
            return None
        try:
            response = self.session.get(
                url, timeout=self.timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            if response.status_code == 404:
                return PackageInfo(exists=False)
            if response.status_code != 200:
                return None
            data = response.json()
        except (requests.RequestException, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return PackageInfo(exists=True, first_published=_first_published(ecosystem, data))

    def advisories(self, pins: Iterable[tuple[str, str, str]]) -> dict[tuple[str, str, str], Advisories]:
        """What OSV lists for each (ecosystem, name, version) that has
        advisories; one that's missing from the answer is clean, or unknown.
        Those three things are all that is sent, and a version that isn't
        well-formed isn't sent at all. One request asks which of the versions
        have any, and each of the first `MAX_VULNERABLE` that do costs one
        more for the records. Never raises: a subclass built for `info`
        alone, or a session that can't post, gets an empty answer."""
        try:
            known = [pin for pin in pins if pin[0] in OSV_ECOSYSTEMS and version_key(pin[2], pin[0]) is not None]
            return self._advisories(list(dict.fromkeys(known)))
        except Exception:
            return {}

    def _advisories(self, pins: list[tuple[str, str, str]]) -> dict[tuple[str, str, str], Advisories]:
        retries, down = 1, False  # one retry for the whole call, so OSV being down costs two timeouts, not two per package

        def ask(path: str, body: dict) -> dict | None:
            nonlocal retries, down
            while True:
                try:
                    response = self.session.post(
                        f"{OSV}/{path}", json=body, timeout=self.timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
                    )
                    if response.status_code == 200:
                        data = response.json()
                        return data if isinstance(data, dict) else None
                    if response.status_code != 429 and response.status_code < 500:
                        return None  # OSV didn't like the question; asking again won't help
                except ValueError:
                    return None
                except requests.RequestException:
                    pass
                if not retries:
                    down = True
                    return None
                retries -= 1

        if not pins:
            return {}
        queries = [
            {
                "package": {"name": normalise(name, ecosystem) if ecosystem == "pypi" else name, "ecosystem": OSV_ECOSYSTEMS[ecosystem]},
                "version": version,
            }
            for ecosystem, name, version in pins
        ]
        results = (ask("querybatch", {"queries": queries}) or {}).get("results")
        if not isinstance(results, list) or len(results) != len(pins):
            return {}
        listed = [
            (pin, query) for pin, query, result in zip(pins, queries, results, strict=True)
            if isinstance(result, dict) and (result.get("vulns") or result.get("next_page_token"))
        ]
        found: dict[tuple[str, str, str], Advisories] = {}
        for pin, query in listed[:MAX_VULNERABLE]:
            data = None if down else ask("query", query)
            # A second page would make the count from this one wrong: unknown rather than understated.
            if data and not data.get("next_page_token") and isinstance(data.get("vulns"), list):
                summary = _summary(*pin, data["vulns"])
                if summary:
                    found[pin] = summary
        return found


def _first_published(ecosystem: str, data: dict) -> datetime | None:
    if ecosystem == "pypi":
        releases = data.get("releases")
        times = [
            _time(upload.get("upload_time_iso_8601") or upload.get("upload_time"))
            for uploads in (releases.values() if isinstance(releases, dict) else ())
            for upload in (uploads if isinstance(uploads, list) else ())
            if isinstance(upload, dict)
        ]
        times = [t for t in times if t is not None]
        return min(times) if times else None
    if ecosystem == "npm":
        published = data.get("time")
        return _time(published.get("created")) if isinstance(published, dict) else None
    crate = data.get("crate")
    return _time(crate.get("created_at")) if isinstance(crate, dict) else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ── advisories ───────────────────────────────────────────────────────────

_OSV_SEVERITY = {"LOW": "low", "MODERATE": "medium", "MEDIUM": "medium", "HIGH": "high", "CRITICAL": "critical"}
# (from, to, fixed): versions from `from` up to `to` are affected. `to` is the
# fixed version itself when `fixed`, the last affected one otherwise; None is
# no upper end. () sorts below every version key, for "from the first release".
_Span = tuple[tuple, tuple | None, str | None]


def _summary(ecosystem: str, name: str, version: str, records: list) -> Advisories | None:
    """OSV's records for one version as one answer. Records that name each
    other as aliases (a GHSA and a PYSEC entry for one CVE) are one advisory.
    The severity is the label the advisory database gave (GitHub's does); a
    CVSS vector on its own isn't turned into one."""
    records = [r for r in records if isinstance(r, dict) and isinstance(r.get("id"), str) and not r.get("withdrawn")]
    parent: dict[str, str] = {}

    def root(key: str) -> str:
        while parent.setdefault(key, key) != key:
            key = parent[key]
        return key

    for record in records:
        for alias in _aliases(record):
            parent[root(alias)] = root(record["id"])
    groups: dict[str, list[dict]] = {}
    for record in records:
        groups.setdefault(root(record["id"]), []).append(record)
    if not groups:
        return None

    at = version_key(version, ecosystem)
    advisories: list[tuple[int, str]] = []
    spans: list[_Span] | None = []
    for members in groups.values():
        own = sorted(r["id"] for r in members)
        known = sorted(alias for r in members for alias in _aliases(r) if alias.startswith("CVE-"))
        shown = next((i for i in own if i.startswith("GHSA-")), None) or (known[0] if known else own[0])
        advisories.append((max(_severity_rank(r) for r in members), shown))
        mine = [span for r in members for span in _spans(r, ecosystem, name) or ()]
        # An advisory with no range around this version that ends in a fix: no version is known to fix them all.
        if spans is not None and at is not None and any(_covers(span, at) and span[2] for span in mine):
            spans.extend(mine)
        else:
            spans = None
    advisories.sort(key=lambda item: (-item[0], item[1]))
    top = advisories[0][0]
    return Advisories(
        ids=tuple(shown for _, shown in advisories),
        severity=SEVERITIES[top] if top >= 0 else None,
        fixed=_first_fixed(at, spans) if spans and at is not None else None,
    )


def _aliases(record: dict) -> list[str]:
    aliases = record.get("aliases")
    return [alias for alias in aliases if isinstance(alias, str)] if isinstance(aliases, list) else []


def _severity_rank(record: dict) -> int:
    """Where the record's severity label sits in SEVERITIES; -1 without one."""
    specific = record.get("database_specific")
    label = _OSV_SEVERITY.get(str(specific.get("severity")).upper()) if isinstance(specific, dict) else None
    return SEVERITIES.index(label) if label else -1


def _spans(record: dict, ecosystem: str, name: str) -> list[_Span] | None:
    """The affected version ranges a record gives for this package; None when
    one of them has a version that can't be compared."""
    spans: list[_Span] = []
    affected = record.get("affected")
    for entry in affected if isinstance(affected, list) else []:
        package = entry.get("package") if isinstance(entry, dict) else None
        if not isinstance(package, dict) or package.get("ecosystem") != OSV_ECOSYSTEMS[ecosystem]:
            continue
        if normalise(str(package.get("name", "")), ecosystem) != normalise(name, ecosystem):
            continue
        ranges = entry.get("ranges")
        for ranged in ranges if isinstance(ranges, list) else []:
            if not isinstance(ranged, dict) or ranged.get("type") not in ("ECOSYSTEM", "SEMVER") or not isinstance(ranged.get("events"), list):
                continue
            events = []
            for event in ranged["events"]:
                kind = next((k for k in ("introduced", "fixed", "last_affected") if isinstance(event, dict) and k in event), None)
                if kind is None:
                    continue
                text = str(event[kind])
                key = () if (kind, text) == ("introduced", "0") else version_key(text, ecosystem)
                if key is None:
                    return None
                events.append((key, kind != "introduced", kind, text))
            start: tuple | None = None
            for key, _, kind, text in sorted(events):  # in version order, as OSV's own matching reads them
                if kind == "introduced":
                    start = key if start is None else start
                elif start is not None:
                    spans.append((start, key, text if kind == "fixed" else None))
                    start = None
            if start is not None:
                spans.append((start, None, None))
    return spans


def _covers(span: _Span, at: tuple) -> bool:
    start, end, fixed = span
    return start <= at and (end is None or at < end or (not fixed and at == end))


def _first_fixed(at: tuple, spans: list[_Span]) -> str | None:
    """The lowest version at or above `at` that no span covers: each fix is
    checked against the other advisories' ranges, since a release that fixes
    one can still be inside another's."""
    fixed: str | None = None
    for _ in range(len(spans) + 1):
        covering = [span for span in spans if _covers(span, at)]
        if not covering:
            return fixed
        if not all(span[2] for span in covering):
            return None
        _, at, fixed = max(covering, key=lambda span: span[1])
    return None
