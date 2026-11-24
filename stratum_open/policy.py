"""The maintainers' rules: `.stratum/policy.yml`, read from the default branch.

A PR is always judged by the rules already on the default branch, never by a
policy it edits itself; its change applies once merged. No file means the
defaults below. A file that can't be used is never ignored silently: the run
falls back to the defaults and `Policy.errors` / `Policy.warnings` say why, for
the check run to show.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import Any, Callable

import yaml

POLICY_PATH = ".stratum/policy.yml"
VERSION = 1
DEFAULT_MODEL = "qwen2.5-coder:7b"
DEFAULT_IGNORED = (
    "**/*.lock", "**/package-lock.json", "**/pnpm-lock.yaml", "**/go.sum",
    "**/*.min.js", "**/*.min.css", "**/*.map", "**/*.svg", "**/*.snap",
    "**/vendor/**", "**/node_modules/**", "**/dist/**",
    ".github/**",  # the workflow check reads these; a small model mostly adds noise
)


REVIEW_EVENTS = ("opened", "synchronize", "reopened", "ready_for_review")


@dataclass(frozen=True)
class Review:
    model: str = "local"  # local | hosted
    endpoint: str = "http://localhost:11434/v1"  # any OpenAI-compatible server; this is Ollama's. `off`: the checks only
    model_name: str = ""  # empty = DEFAULT_MODEL
    profile: str = "auto"  # auto | small | large: how much the review asks of the model
    context_tokens: int = 16384  # the model's context window; the diff is split to fit it
    max_chunks: int = 3  # model calls per review at most; files past that are listed as not reviewed
    diff_format: str = "interleaved"  # interleaved | split (new and old hunks apart)
    large_patch: str = "clip"  # clip | skip a file too big for one call
    context_lines: int = 0  # unchanged lines shown around each hunk, from the file (large: 10)
    mode: str = "general"  # general | security | performance | maintainability: what the per-file passes put first
    file_reviews: int = 0  # files the model reviews one at a time before the chunked review (large: 15)
    lens_files: int = 0  # of those, how many get a focused pass per lens of the mode (large: 5)
    walkthrough: bool = False  # one more call for a summary, priorities and a walkthrough (large: on)
    parallel_calls: int = 1  # model calls at once in the per-file passes (large: 4)
    max_inline_comments: int = 12
    min_severity: str = "medium"  # the model's findings below this aren't shown; the checks' always are
    verify: str = "none"  # none | strict | lenient: one more short call per model finding, dropping those the model rejects
    reasoning: str = "default"  # default | low | off: how much a thinking model (gpt-oss, Qwen3) thinks before answering
    logic_pass: bool = False  # one more call per chunk that looks only for logic errors, one decision at a time
    agent_prs_logic_pass: bool = False  # logic_pass, only on a PR a coding agent likely wrote (agent_prs.py)
    rules: tuple[str, ...] = ()  # the project's own review rules, in plain words, added to the prompt
    ignore_paths: tuple[str, ...] = DEFAULT_IGNORED
    log_prompts: bool = False
    check_name: str = "Stratum"  # the check run's name, as branch protection requires it
    conclusion: str = "severity"  # severity: critical fails, high is neutral | score: by risk score
    fail_at: int = 70  # with conclusion: score, a risk score at or above this fails the check
    warn_at: int = 50  # ... and at or above this is neutral
    events: tuple[str, ...] = REVIEW_EVENTS  # PR events that start a review
    skip_drafts: bool = True
    skip_bots: bool = False
    skip_authors: tuple[str, ...] = ()  # logins whose PRs aren't reviewed
    skip_labels: tuple[str, ...] = ()  # PRs with any of these labels aren't reviewed
    skip_branches: tuple[str, ...] = ()  # globs; PRs from a matching branch aren't reviewed
    quiet: bool = False  # fewer of the model's comments, and a summary that keeps them apart from the checks' findings
    learn_from_history: bool = False  # leave out findings like ones maintainers rejected (👎, `/stratum ignore`) on recent PRs
    learn_lookback: int = 100  # how many of the repo's latest comments of each kind that reads (one API call per 100)
    early_checks: bool = False  # the Action posts the checks' findings before starting the model, then completes that comment


@dataclass(frozen=True)
class Checks:
    bandit_low: bool = False  # keep Bandit's low-severity and low-confidence results
    heuristics: bool = False  # a finding per file in a risky area (auth, migrations, CI...), and for weakened tests
    ruff: bool = False  # errors Ruff finds on added Python lines (an undefined name, a format string that raises), when it's installed
    floating_promises: bool = False  # JavaScript and TypeScript: a call whose promise is dropped; needs the `js` extra
    weakened_tests: bool = False  # a finding on the line where a PR switches a test off, removes its assertions or widens a tolerance
    unknown_names: bool = False  # in Python, an import or a call of a name its module doesn't define; needs a checkout
    removed_names: bool = False  # in Python, a name a PR removes from a module while another file still imports or uses it; needs a checkout
    shellcheck: bool = False  # ShellCheck's errors, and the warnings that name a real bug, on added lines of shell scripts; needs `shellcheck` on PATH
    actionlint: bool = False  # actionlint's errors on added lines of GitHub workflows (a key GitHub doesn't know, a `needs:` with no such job); needs `actionlint` on PATH
    # The two below build the PR's code, which runs it, so they need a checkout of the PR's head: the CLI's
    # `review --local`, or the Action's `head-checkout`, which is refused for a PR from a fork.
    go_vet: bool = False  # go vet's findings (and staticcheck's SA checks, if it's installed) on added lines of Go code; needs `go` on PATH
    clippy: bool = False  # Clippy's correctness lints on added lines of Rust code; needs `cargo` with the clippy component
    build_bot_prs: bool = False  # let those two build a PR a bot opened (Dependabot, Renovate), which runs the releases it brings in


@dataclass(frozen=True)
class Commands:
    """Who may run each `/stratum` command: anyone | author (the PR's author,
    or anyone with write access) | write (write access)."""
    review: str = "write"
    ignore: str = "write"
    mute: str = "write"
    explain: str = "author"  # it's a model call; anyone would let strangers use the model
    triage: str = "anyone"
    record: str = "anyone"


@dataclass(frozen=True)
class AlwaysHuman:
    paths: tuple[str, ...] = (".github/workflows/**", ".stratum/**")
    new_dependencies: bool = True


@dataclass(frozen=True)
class Labels:
    ready: str = "triage: ready"
    needs_work: str = "triage: needs work"
    low_effort: str = "triage: low effort"
    needs_human: str = "triage: needs a maintainer"
    agent_written: str = "triage: agent-written"  # with agent_prs.label, beside the one above


@dataclass(frozen=True)
class AgentPRs:
    """PRs a coding agent likely wrote (agent_prs.py). Any key on also has each
    review read the PR's commits, one more API call, and say what it found."""
    label: bool = False  # add labels.agent_written (with triage.enabled)
    needs_maintainer: bool = False  # sort them as "needs a maintainer"
    require_tests: bool = False  # sort one that changes code and no test as "needs work"


@dataclass(frozen=True)
class Triage:
    enabled: bool = False  # labels and closing happen only when true
    labels: Labels = field(default_factory=Labels)
    label_low_effort: bool = True
    require_linked_issue: bool = False
    close_without_issue: bool = False
    queue: bool = True  # the Action keeps an issue listing every open PR, sorted
    agent_prs: AgentPRs = field(default_factory=AgentPRs)


@dataclass(frozen=True)
class TrackRecord:
    enabled: bool = True
    affects_triage: bool = False
    window_days: int = 180
    followup_days: int = 14
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dependencies:
    registry_lookup: bool = True
    min_package_age_days: int = 30
    known_vulnerabilities: bool = False  # ask OSV (osv.dev) about each pinned version; needs registry_lookup


@dataclass(frozen=True)
class Boundaries:
    # module name -> path globs, e.g. {"api": ("server/api/**",)}
    modules: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # "a -> b", or "a -> b: why": code in module a must not import module b. "*" is any module.
    forbid: tuple[str, ...] = ()
    severity: str = "medium"  # a forbidden import's finding
    new_edges: str = "off"  # off | warn | fail: a module importing one it didn't import on the default branch


@dataclass(frozen=True)
class Data:
    findings_retention_days: int = 180


@dataclass(frozen=True)
class Policy:
    version: int = VERSION
    review: Review = field(default_factory=Review)
    always_human: AlwaysHuman = field(default_factory=AlwaysHuman)
    triage: Triage = field(default_factory=Triage)
    track_record: TrackRecord = field(default_factory=TrackRecord)
    dependencies: Dependencies = field(default_factory=Dependencies)
    boundaries: Boundaries = field(default_factory=Boundaries)
    checks: Checks = field(default_factory=Checks)
    commands: Commands = field(default_factory=Commands)
    data: Data = field(default_factory=Data)
    source: str = "defaults"  # "defaults" | "file" | "defaults (file unusable)"
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    explicit: frozenset[str] = frozenset()  # dotted keys the file set; a profile never overrides them

    def needs_human(self, path: str) -> bool:
        """Whether a changed file falls under `always_human.paths`."""
        return matches_any(path, self.always_human.paths)

    def ignored(self, path: str) -> bool:
        """Whether a file is kept out of the model's view."""
        return matches_any(path, self.review.ignore_paths)

    @property
    def model_name(self) -> str:
        return self.review.model_name or DEFAULT_MODEL


def matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_glob(pattern).fullmatch(path.lstrip("/")) for pattern in patterns)


# Values that are checked beyond their type.
_WHO = ("anyone", "author", "write")
_CHOICES = {
    ("review", "model"): ("local", "hosted"),
    ("review", "min_severity"): ("low", "medium", "high", "critical"),
    ("review", "profile"): ("auto", "small", "large"),
    ("review", "diff_format"): ("interleaved", "split"),
    ("review", "large_patch"): ("clip", "skip"),
    ("review", "mode"): ("general", "security", "performance", "maintainability"),
    ("review", "conclusion"): ("severity", "score"),
    ("review", "verify"): ("none", "strict", "lenient"),
    ("review", "reasoning"): ("default", "low", "off"),
    ("boundaries", "severity"): ("low", "medium", "high", "critical"),
    ("boundaries", "new_edges"): ("off", "warn", "fail"),
    **{("commands", name): _WHO for name in ("review", "ignore", "mute", "explain", "triage", "record")},
}
_MINIMUMS = {
    ("review", "max_inline_comments"): 0,
    ("review", "context_tokens"): 2048,
    ("review", "max_chunks"): 1,
    ("review", "file_reviews"): 0,
    ("review", "context_lines"): 0,
    ("review", "lens_files"): 0,
    ("review", "parallel_calls"): 1,
    ("review", "fail_at"): 0,
    ("review", "warn_at"): 0,
    ("track_record", "window_days"): 1,
    ("track_record", "followup_days"): 1,
    ("dependencies", "min_package_age_days"): 0,
    ("data", "findings_retention_days"): 1,
    ("review", "learn_lookback"): 1,
}
# Day counts past a century would overflow the date arithmetic.
_MAXIMUMS = {path: 36500 for path in _MINIMUMS if path[1].endswith("_days")} | {("review", "learn_lookback"): 500}
_MAPPINGS = {("boundaries", "modules")}  # name -> list of text values
_META = {"source", "warnings", "errors", "explicit"}
# Top-level sections that hosted Stratum reads from this file and this engine
# doesn't: accepted unread, so one file serves both. Their reader checks them.
_HOSTED = {"services"}  # hosted Stratum's service map: which service calls, uses, publishes to or consumes from which


def parse_policy(text: str | None) -> Policy:
    """The policy for this file's text (None = no file)."""
    if text is None:
        return Policy()
    try:
        raw = yaml.safe_load(text)
        repeated = _repeated_keys(yaml.compose(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        return _unusable(f"{POLICY_PATH} isn't valid YAML ({_yaml_problem(exc)}).")
    if raw is None:
        return Policy(source="file")
    if not isinstance(raw, dict):
        return _unusable(f"{POLICY_PATH} must be a mapping of keys, not a {type(raw).__name__}.")
    if raw.get("version", VERSION) != VERSION:
        return _unusable(f"{POLICY_PATH} has version {raw.get('version')!r}; this release reads version {VERSION}.")

    warnings: list[str] = repeated
    errors: list[str] = []
    explicit: set[str] = set()
    policy = _build(Policy, {k: v for k, v in raw.items() if k not in _HOSTED}, (), warnings, errors, explicit)
    if not errors:
        errors.extend(_boundary_errors(policy.boundaries))
        errors.extend(f"review.events can't include {e!r}; it can include: {', '.join(REVIEW_EVENTS)}."
                      for e in policy.review.events if e not in REVIEW_EVENTS)
        if policy.review.warn_at > policy.review.fail_at:
            errors.append("review.warn_at must not be above review.fail_at.")
    if errors:
        return _unusable(*errors, warnings=warnings)
    return replace(policy, source="file", warnings=tuple(warnings), explicit=frozenset(explicit))


def load_policy(read_default_branch_file: Callable[[str], str | None]) -> Policy:
    """`read_default_branch_file(path)` returns a file's text on the default
    branch, or None when it doesn't exist. Never pass the PR's head here."""
    return parse_policy(read_default_branch_file(POLICY_PATH))


_FORBID = re.compile(r"^\s*([\w.-]+|\*)\s*->\s*([\w.-]+|\*)\s*(?::\s*(.*?))?\s*$", re.S)


def forbidden_pairs(boundaries: Boundaries) -> list[tuple[str, str]]:
    return [(source, target) for source, target, _ in forbid_rules(boundaries)]


def forbid_rules(boundaries: Boundaries) -> list[tuple[str, str, str]]:
    """(from, to, why) for each forbid entry; either module may be "*"."""
    return [(m.group(1), m.group(2), m.group(3) or "") for m in map(_FORBID.match, boundaries.forbid) if m]


def _forbid_entries(value: Any) -> tuple[str, ...] | None:
    """The forbid list as "a -> b: why" text. YAML turns an unquoted
    `- a -> b: why` into a mapping, and `{from, to, reason}` is a mapping
    too; both come back as text. None when an entry is neither."""
    if not isinstance(value, list):
        return None
    entries = []
    for item in value:
        if isinstance(item, str):
            entries.append(item)
        elif isinstance(item, dict) and {"from", "to"} <= set(item) and set(item) <= {"from", "to", "reason"} \
                and all(isinstance(v, str) for v in item.values()):
            entries.append(f"{item['from']} -> {item['to']}" + (f": {item['reason']}" if item.get("reason") else ""))
        elif isinstance(item, dict) and len(item) == 1 and all(isinstance(k, str) and isinstance(v, str) for k, v in item.items()):
            [(rule, why)] = item.items()
            entries.append(f"{rule}: {why}")
        else:
            return None
    return tuple(entries)


def _boundary_errors(boundaries: Boundaries) -> list[str]:
    names = {name for name, _ in boundaries.modules}
    errors = []
    for rule in boundaries.forbid:
        match = _FORBID.match(rule)
        if not match:
            errors.append(f'boundaries.forbid entry "{rule}" must look like "a -> b", or "a -> b: why".')
            continue
        for name in match.groups()[:2]:
            if name != "*" and name not in names:
                errors.append(f"boundaries.forbid entry \"{rule}\" names {name}, which boundaries.modules doesn't define.")
    return errors


def _unusable(*errors: str, warnings: list[str] | None = None) -> Policy:
    return Policy(source="defaults (file unusable)", errors=tuple(errors), warnings=tuple(warnings or ()))


def _build(cls: type, raw: dict, path: tuple[str, ...], warnings: list[str], errors: list[str], explicit: set[str]) -> Any:
    known = {f.name: f for f in fields(cls) if f.name not in _META}
    for key in raw:
        if key not in known:
            warnings.append(f"Unknown key {_dotted(path + (str(key),))} is ignored.")
    values: dict[str, Any] = {}
    defaults = cls()
    for name, spec in known.items():
        if name not in raw:
            continue
        key_path = path + (name,)
        default = getattr(defaults, name)
        value = raw[name]
        if is_dataclass(default):
            if not isinstance(value, dict):
                errors.append(f"{_dotted(key_path)} must be a mapping.")
                continue
            values[name] = _build(type(default), value, key_path, warnings, errors, explicit)
        else:
            checked = _check(key_path, value, default, warnings, errors)
            if checked is not _INVALID:
                values[name] = checked
                explicit.add(_dotted(key_path))
    return replace(defaults, **values) if values else defaults


_INVALID = object()


def _check(path: tuple[str, ...], value: Any, default: Any, warnings: list[str], errors: list[str]) -> Any:
    name = _dotted(path)
    if path in (("boundaries", "new_edges"), ("review", "reasoning"), ("review", "endpoint")) and value is False:
        return "off"  # YAML reads an unquoted `off` as false
    if path == ("boundaries", "forbid"):
        entries = _forbid_entries(value)
        if entries is None:
            errors.append(f'{name} must be a list of rules like "a -> b" or "a -> b: why".')
            return _INVALID
        return entries
    if path in _MAPPINGS:
        if isinstance(value, dict) and all(
            isinstance(k, str) and isinstance(v, list) and all(isinstance(i, str) for i in v) for k, v in value.items()
        ):
            return tuple((k, tuple(v)) for k, v in value.items())
        errors.append(f"{name} must map names to lists of text values.")
        return _INVALID
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        errors.append(f"{name} must be true or false.")
    elif isinstance(default, int):
        if isinstance(value, int) and not isinstance(value, bool):
            minimum, maximum = _MINIMUMS.get(path), _MAXIMUMS.get(path)
            if minimum is not None and value < minimum:
                errors.append(f"{name} must be at least {minimum}.")
            elif maximum is not None and value > maximum:
                # Read as the cap, so one large number doesn't lose the rest of the file.
                warnings.append(f"{name} is read as {maximum}, the most it can be.")
                return maximum
            else:
                return value
        else:
            errors.append(f"{name} must be a whole number.")
    elif isinstance(default, str):
        if isinstance(value, str):
            choices = _CHOICES.get(path)
            if choices and value not in choices:
                errors.append(f"{name} must be one of: {', '.join(choices)}.")
            else:
                return value
        else:
            errors.append(f"{name} must be text.")
    elif isinstance(default, tuple):
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return tuple(value)
        errors.append(f"{name} must be a list of text values.")
    return _INVALID


def _repeated_keys(node: yaml.Node | None, path: tuple[str, ...] = (), seen: set[int] | None = None) -> list[str]:
    """YAML keeps the last copy of a key given twice in one mapping and drops
    the others without a word; name each one, with its lines."""
    seen = set() if seen is None else seen
    if node is None or id(node) in seen:  # an alias points back at a node already walked
        return []
    seen.add(id(node))
    found: list[str] = []
    if isinstance(node, yaml.SequenceNode):
        for item in node.value:
            found.extend(_repeated_keys(item, path, seen))
    elif isinstance(node, yaml.MappingNode):
        lines: dict[tuple[str, str], list[int]] = {}
        for key, value in node.value:
            if key.tag == "tag:yaml.org,2002:merge":  # `<<: *base`: the keys after it are meant to override
                continue
            key_path = path + (str(key.value),)
            if isinstance(key, yaml.ScalarNode):
                lines.setdefault((key.tag, key.value), []).append(key.start_mark.line + 1)
            found.extend(_repeated_keys(value, key_path, seen))
        for (_, name), at in lines.items():
            if len(at) > 1:
                times = "twice" if len(at) == 2 else f"{len(at)} times"
                found.append(f"{_dotted(path + (name,))} appears {times} (lines {', '.join(map(str, at[:-1]))} and {at[-1]}); "
                             "only the last one is read.")
    return found


def _dotted(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _yaml_problem(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    return f"line {mark.line + 1}" if mark is not None else "unreadable"


_GLOB_CACHE: dict[str, re.Pattern] = {}


def _glob(pattern: str) -> re.Pattern:
    """`**` crosses folders, `*` and `?` stay inside one, like .gitignore."""
    compiled = _GLOB_CACHE.get(pattern)
    if compiled is None:
        out, i, key = [], 0, pattern
        pattern = pattern.lstrip("/")
        if pattern.endswith("/"):  # a folder: everything in it
            pattern += "**"
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            elif pattern[i] == "*":
                out.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                out.append("[^/]")
                i += 1
            else:
                out.append(re.escape(pattern[i]))
                i += 1
        compiled = _GLOB_CACHE[key] = re.compile("".join(out))
    return compiled
