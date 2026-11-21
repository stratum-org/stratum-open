"""How much a review asks of its model: `small` for 7–14B models on the
maintainers' own hardware, `large` for big hosted ones.

A profile is a set of defaults, not separate code: it fills in the settings a
policy file didn't set, and anything the file sets explicitly wins. Callers
that embed the engine (Stratum) pass their settings explicitly, so changing a
profile's defaults here never changes them.

`review.profile: auto` picks the profile from the model the review runs on:
its size where it's known (from the server, or the "7b" in its name), else its
context window, else whether it runs locally.

Whatever the profile, a model family can need settings of its own
(`MODEL_SETTINGS`): gpt-oss gets `review.reasoning: low`,
`review.verify: strict`, `review.logic_pass: true` and
`review.context_lines: 10`, again unless the policy file set them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Any

from stratum_open.policy import Policy

# "About 20B": gpt-oss:20b is 20.9B as Ollama reports it, and its numbers in the
# README were measured on `small`. The 22B and 24B dense models stay `large`.
SMALL_MAX_BILLIONS = 21.0
SMALL_MAX_CONTEXT = 32_000

# Dotted policy keys each profile sets, unless the policy file set them.
PRESETS: dict[str, dict[str, Any]] = {
    "small": {
        "review.diff_format": "interleaved",
        "review.context_tokens": 16384,
        "review.context_lines": 0,
        "checks.bandit_low": False,
        "checks.heuristics": False,
        "review.file_reviews": 0,
        "review.lens_files": 0,
        "review.walkthrough": False,
        "review.parallel_calls": 1,
    },
    "large": {
        "review.diff_format": "split",
        "review.context_tokens": 32768,
        "review.context_lines": 10,
        "checks.bandit_low": True,
        "checks.heuristics": True,
        # About 29 calls a PR: 15 files one at a time, 5 of them again per
        # lens, one synthesis, then the chunked review.
        "review.file_reviews": 15,
        "review.lens_files": 5,
        "review.walkthrough": True,
        "review.parallel_calls": 4,
    },
}

# Settings one model family needs whatever the profile, unless the policy file
# set them: the pattern its name matches, then each key's value and why. On the
# free runner gpt-oss:20b found 16 of 39 planted bugs with the first two
# (benchmark/results/2026-11-08-gpt-oss-actions-cpu). With the logic pass and
# ten lines of context as well, it found 27 of 39 against 18 for the first two
# alone (2026-11-13-logic-pass-actions-cpu). On the 20 cases kept back from
# tuning, they found 13 against 9 that day, and 14 against 5 when run again on
# 21 Nov (2026-11-21-gpt-oss-test-split-actions-cpu). Either one alone found one
# or two more. The cost is the wait: a median of about 11.5 minutes a PR on the
# free runner instead of about 3.5, and up to 43 minutes on a large PR.
MODEL_SETTINGS: tuple[tuple[re.Pattern[str], dict[str, tuple[Any, str]]], ...] = (
    # Ollama's names for it: gpt-oss, gpt-oss:20b, library/gpt-oss:120b.
    (re.compile(r"(?:^|/)gpt-oss(?::|$)", re.IGNORECASE), {
        "review.reasoning": ("low", "without it, the thinking uses up the output budget and most replies come back empty"),
        "review.verify": ("strict", "it halves the wrong comments and costs no real catches"),
        "review.logic_pass": (True, "with ten lines of context it found 27 of 39 planted bugs against 18, though a review takes about three times as long"),
        "review.context_lines": (10, "the logic pass finds more with the code around each change, and either alone finds only one or two more"),
    }),
)

_SIZE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)b(?![a-z])", re.IGNORECASE)


@dataclass(frozen=True)
class ModelInfo:
    billions: float | None = None  # parameters, in billions
    context: int | None = None  # the context window, in tokens
    local: bool = True  # runs on the maintainers' own hardware


def size_from_name(name: str) -> float | None:
    """The parameter count in a model's name: "qwen2.5-coder:7b" is 7,
    "nemotron-3-super-120b-a12b" is 120 (its total, not its active size)."""
    sizes = [float(match) for match in _SIZE.findall(name or "")]
    return max(sizes) if sizes else None


def choose(policy: Policy, info: ModelInfo) -> str:
    """The profile for a review: the policy's, unless it says `auto`."""
    if policy.review.profile != "auto":
        return policy.review.profile
    if info.billions is not None:
        return "small" if info.billions <= SMALL_MAX_BILLIONS else "large"
    if info.context is not None:
        return "small" if info.context < SMALL_MAX_CONTEXT else "large"
    return "small" if info.local else "large"


def apply(policy: Policy, profile: str, info: ModelInfo | None = None) -> Policy:
    """The policy with the profile's defaults filled in where the file was
    silent. The context window never goes past what the model has."""
    preset = dict(PRESETS[profile])
    if info is not None and info.context and "review.context_tokens" in preset:
        preset["review.context_tokens"] = min(preset["review.context_tokens"], info.context)
    for key, value in preset.items():
        if key not in policy.explicit:
            policy = _set(policy, key.split("."), value)
    return policy


def for_model(policy: Policy, name: str) -> tuple[Policy, dict[str, Any]]:
    """The policy with the settings the model's family needs filled in where
    the file was silent (`MODEL_SETTINGS`), and the ones filled in. Any other
    model gets the policy back as it was."""
    applied: dict[str, Any] = {}
    for pattern, settings in MODEL_SETTINGS:
        if isinstance(name, str) and pattern.search(name):
            for key, (value, _) in settings.items():
                if key not in policy.explicit:
                    policy = _set(policy, key.split("."), value)
                    applied[key] = value
    return policy, applied


def model_settings_line(name: str, applied: dict[str, Any]) -> str:
    """What `for_model` filled in and why, for the review's summary; empty
    when it filled in nothing."""
    reasons = {key: why for pattern, settings in MODEL_SETTINGS if pattern.search(name or "") for key, (_, why) in settings.items()}
    # Written as the policy file would have it: `review.logic_pass: true`, not True.
    parts = [f"`{key}: {str(value).lower() if isinstance(value, bool) else value}` ({reasons[key]})" for key, value in applied.items()]
    if not parts:
        return ""
    # The reasons have commas of their own, so three or more are kept apart with semicolons.
    joined = " and ".join(parts) if len(parts) < 3 else "; ".join(parts)
    return f"Set for `{name}`, since the policy file doesn't set {'them' if len(parts) > 1 else 'it'}: " + joined + "."


def _set(obj: Any, path: list[str], value: Any) -> Any:
    head, rest = path[0], path[1:]
    if not rest:
        return replace(obj, **{head: value})
    return replace(obj, **{head: _set(getattr(obj, head), rest, value)})


def keys_of(cls: type, prefix: str = "") -> set[str]:
    """Every dotted settings key a policy dataclass has (used by the tests to
    check the presets name real keys)."""
    out = set()
    for spec in fields(cls):
        default = getattr(cls(), spec.name)
        if is_dataclass(default):
            out |= keys_of(type(default), f"{prefix}{spec.name}.")
        else:
            out.add(f"{prefix}{spec.name}")
    return out
