from dataclasses import asdict, replace
from pathlib import Path

import pytest
import yaml

from stratum_open.policy import POLICY_PATH, Policy, forbidden_pairs, load_policy, matches_any, parse_policy


def test_no_file_means_defaults():
    policy = parse_policy(None)
    assert policy == Policy()
    assert policy.source == "defaults"
    assert policy.review.model == "local"
    assert policy.triage.enabled is False
    assert policy.track_record.affects_triage is False
    assert policy.triage.labels.needs_human == "triage: needs a maintainer"


def test_the_example_file_lists_every_key_with_its_default():
    example = (Path(__file__).parent.parent / "policy.example.yml").read_text(encoding="utf-8")
    parsed = parse_policy(example)
    assert replace(parsed, explicit=frozenset()) == replace(Policy(), source="file")

    def keys(value, prefix=""):
        if not isinstance(value, dict):
            return set()
        return {prefix + k for k in value} | {key for k, v in value.items() for key in keys(v, f"{prefix}{k}.")}

    expected = {k: v for k, v in asdict(Policy()).items() if k not in ("source", "warnings", "errors", "explicit")}
    assert keys(yaml.safe_load(example)) == keys(expected)


def test_the_file_records_which_keys_it_set():
    policy = parse_policy("review:\n  diff_format: split\ncommands:\n  explain: write\n")
    assert policy.explicit == frozenset({"review.diff_format", "commands.explain"})
    assert parse_policy("review:\n  events: [opened, closed]\n").errors == (
        "review.events can't include 'closed'; it can include: opened, synchronize, reopened, ready_for_review.",
    )
    assert parse_policy("review:\n  fail_at: 40\n  warn_at: 60\n").errors == ("review.warn_at must not be above review.fail_at.",)
    assert parse_policy("commands:\n  review: everyone\n").errors == ("commands.review must be one of: anyone, author, write.",)


def test_empty_file_means_defaults_from_the_file():
    policy = parse_policy("")
    assert policy.source == "file"
    assert policy.errors == policy.warnings == ()


def test_values_override_only_what_they_name():
    policy = parse_policy(
        """
version: 1
review:
  max_inline_comments: 5
triage:
  labels:
    ready: "good to go"
track_record:
  exclude: [dependabot, renovate]
"""
    )
    assert policy.source == "file"
    assert policy.review.max_inline_comments == 5
    assert policy.review.endpoint == "http://localhost:11434/v1"
    assert policy.triage.labels.ready == "good to go"
    assert policy.triage.labels.needs_work == "triage: needs work"
    assert policy.track_record.exclude == ("dependabot", "renovate")


def test_unknown_keys_are_warnings_not_errors():
    policy = parse_policy("review:\n  modle: hosted\nextra: 1\n")
    assert policy.source == "file"
    assert policy.warnings == ("Unknown key extra is ignored.", "Unknown key review.modle is ignored.")


def test_a_repeated_key_is_a_warning_that_names_both_lines():
    # YAML keeps the last copy of a repeated key and drops the others, so say so.
    text = "review:\n  max_chunks: 5\n  max_chunks: 6\ntriage:\n  enabled: true\nreview:\n  rules: [Use parameters.]\n"
    policy = parse_policy(text)
    assert policy.source == "file" and policy.errors == ()
    assert policy.warnings == (
        "review.max_chunks appears twice (lines 2 and 3); only the last one is read.",
        "review appears twice (lines 1 and 6); only the last one is read.",
    )
    assert parse_policy("extra: 1\nextra: 2\nextra: 3\n").warnings == (
        "extra appears 3 times (lines 1, 2 and 3); only the last one is read.",
        "Unknown key extra is ignored.",
    )
    assert policy.review.max_chunks == 3 and policy.review.rules == ("Use parameters.",) and policy.triage.enabled
    # A key given by YAML's merge key (<<) and again by hand isn't repeated.
    merged = parse_policy("base: &b {enabled: true}\ntriage:\n  <<: *b\n  enabled: false\n")
    assert not any("appears" in warning for warning in merged.warnings)
    # The warning stays when the file is unusable for another reason.
    unusable = parse_policy("review:\n  model: cloud\nreview:\n  model: cloud\n")
    assert unusable.errors and unusable.warnings == ("review appears twice (lines 1 and 3); only the last one is read.",)


def test_services_is_hosted_stratums_section_and_is_accepted_unread():
    policy = parse_policy("services:\n  web:\n    calls: [api]\n  api: 7\nreview:\n  mode: security\nextra: 1\n")
    assert (policy.source, policy.errors, policy.warnings) == ("file", (), ("Unknown key extra is ignored.",))
    assert policy.review.mode == "security" and policy.explicit == frozenset({"review.mode"})
    # Whatever it holds, the review runs with the rest of the file.
    assert replace(parse_policy("services: [not, a, mapping]\n"), explicit=frozenset()) == replace(Policy(), source="file")
    assert not hasattr(policy, "services")


@pytest.mark.parametrize(
    "text, error",
    [
        ("review: [1]", "review must be a mapping."),
        ("review:\n  model: cloud", "review.model must be one of: local, hosted."),
        ("review:\n  log_prompts: 1", "review.log_prompts must be true or false."),
        ("review:\n  max_inline_comments: -1", "review.max_inline_comments must be at least 0."),
        ("review:\n  max_inline_comments: true", "review.max_inline_comments must be a whole number."),
        ("review:\n  endpoint: 8080", "review.endpoint must be text."),
        ("track_record:\n  followup_days: 0", "track_record.followup_days must be at least 1."),
        ("always_human:\n  paths: .github/**", "always_human.paths must be a list of text values."),
        ("version: 2", f"{POLICY_PATH} has version 2; this release reads version 1."),
        ("- a\n- b", f"{POLICY_PATH} must be a mapping of keys, not a list."),
        ("review:\n  model: [", f"{POLICY_PATH} isn't valid YAML (line 2)."),
    ],
)
def test_an_unusable_file_falls_back_to_defaults_and_says_why(text, error):
    policy = parse_policy(text)
    assert policy.source == "defaults (file unusable)"
    assert policy.errors == (error,)
    assert policy.review == Policy().review


def test_boundaries():
    policy = parse_policy("boundaries:\n  modules: {api: ['server/api/**'], db: ['server/db/**']}\n  forbid: ['api -> db']\n")
    assert policy.boundaries.modules == (("api", ("server/api/**",)), ("db", ("server/db/**",)))
    assert forbidden_pairs(policy.boundaries) == [("api", "db")]

    assert parse_policy("boundaries:\n  forbid: ['api to db']\n").errors == ('boundaries.forbid entry "api to db" must look like "a -> b", or "a -> b: why".',)
    assert parse_policy("boundaries:\n  modules: {api: ['x/**']}\n  forbid: ['api -> db']\n").errors == (
        "boundaries.forbid entry \"api -> db\" names db, which boundaries.modules doesn't define.",
    )
    assert parse_policy("boundaries:\n  modules: {api: x}\n").errors == ("boundaries.modules must map names to lists of text values.",)


def test_one_bad_value_rejects_the_whole_file():
    policy = parse_policy("review:\n  max_inline_comments: 3\n  model: cloud\n")
    assert policy.review.max_inline_comments == 12


def test_load_policy_reads_only_the_policy_path():
    asked = []

    def read(path):
        asked.append(path)
        return "review:\n  model: hosted\n"

    assert load_policy(read).review.model == "hosted"
    assert asked == [POLICY_PATH]
    assert load_policy(lambda path: None).source == "defaults"


@pytest.mark.parametrize(
    "path, expected",
    [
        (".github/workflows/ci.yml", True),
        (".github/workflows/nested/ci.yml", True),
        ("/.github/workflows/ci.yml", True),
        (".github/dependabot.yml", False),
        (".stratum/policy.yml", True),
        ("src/.stratum/policy.yml", False),
    ],
)
def test_needs_human_default_paths(path, expected):
    assert Policy().needs_human(path) is expected


def test_glob_rules():
    policy = parse_policy("always_human:\n  paths: ['**/*.lock', '*.md', 'docs/?.txt']\n")
    assert policy.needs_human("poetry.lock")
    assert policy.needs_human("a/b/Cargo.lock")
    assert policy.needs_human("README.md")
    assert not policy.needs_human("docs/README.md")
    assert policy.needs_human("docs/a.txt")
    assert not policy.needs_human("docs/ab.txt")


def test_a_day_count_past_the_cap_is_read_as_the_cap_and_the_rest_still_applies():
    policy = parse_policy("data:\n  findings_retention_days: 99999\nreview:\n  ignore_paths: [vendor/**]\n")
    assert (policy.source, policy.errors) == ("file", ())
    assert policy.data.findings_retention_days == 36500 and policy.review.ignore_paths == ("vendor/**",)
    assert policy.warnings == ("data.findings_retention_days is read as 36500, the most it can be.",)


def test_a_pattern_ending_in_a_slash_is_a_folder():
    assert matches_any("docs/a.md", ("docs/",)) and matches_any("docs/x/a.md", ("/docs/",))
    assert not matches_any("docs.md", ("docs/",)) and not matches_any("src/docs/a.md", ("docs/",))


def test_profiles_fill_in_only_what_the_file_left_out():
    from stratum_open.profiles import PRESETS, ModelInfo, apply, choose, keys_of, size_from_name

    for preset in PRESETS.values():
        assert set(preset) <= keys_of(Policy)  # presets only name real keys
    assert size_from_name("qwen2.5-coder:7b") == 7
    assert size_from_name("nvidia/nemotron-3-super-120b-a12b:free") == 120
    assert size_from_name("north-mini-code") is None
    policy = parse_policy(None)
    assert choose(policy, ModelInfo(billions=7.6)) == "small"
    assert choose(policy, ModelInfo(billions=31)) == "large"
    assert choose(policy, ModelInfo(context=8192, local=False)) == "small"
    assert choose(policy, ModelInfo(local=False)) == "large"
    assert choose(policy, ModelInfo(local=True)) == "small"
    assert choose(parse_policy("review:\n  profile: small\n"), ModelInfo(billions=405)) == "small"

    large = apply(policy, "large", ModelInfo(context=20_000))
    assert (large.review.diff_format, large.checks.bandit_low, large.review.context_tokens) == ("split", True, 20_000)
    pinned = apply(parse_policy("review:\n  diff_format: interleaved\n"), "large")
    assert pinned.review.diff_format == "interleaved" and pinned.checks.bandit_low is True


GPT_OSS = {"review.reasoning": "low", "review.verify": "strict", "review.logic_pass": True, "review.context_lines": 10}


@pytest.mark.parametrize("name", ["gpt-oss:20b", "gpt-oss", "GPT-OSS:120b", "library/gpt-oss:20b", "registry.ollama.ai/library/gpt-oss:latest"])
def test_gpt_oss_gets_low_reasoning_strict_verify_and_the_logic_pass_with_context(name):
    from stratum_open.profiles import MODEL_SETTINGS, for_model, keys_of

    for _, settings in MODEL_SETTINGS:
        assert set(settings) <= keys_of(Policy)  # only real keys
    policy, applied = for_model(parse_policy(None), name)
    assert (policy.review.reasoning, policy.review.verify, policy.review.logic_pass, policy.review.context_lines) == ("low", "strict", True, 10)
    assert applied == GPT_OSS


def test_gpt_oss_20b_gets_the_logic_pass_and_ten_lines_of_context_over_the_small_profile():
    from stratum_open.profiles import ModelInfo, apply, for_model

    small = apply(parse_policy(None), "small", ModelInfo(billions=20.9))
    assert (small.review.logic_pass, small.review.context_lines) == (False, 0)
    policy, applied = for_model(small, "gpt-oss:20b")
    assert (policy.review.logic_pass, policy.review.context_lines) == (True, 10) and applied == GPT_OSS
    # The faster review is two settings away, and the policy file's values win.
    faster = parse_policy("review:\n  logic_pass: false\n  context_lines: 0\n")
    policy, applied = for_model(apply(faster, "small"), "gpt-oss:20b")
    assert (policy.review.logic_pass, policy.review.context_lines) == (False, 0)
    assert applied == {"review.reasoning": "low", "review.verify": "strict"}


def test_the_policy_files_own_values_win_for_gpt_oss():
    from stratum_open.profiles import for_model, model_settings_line

    policy, applied = for_model(parse_policy("review:\n  verify: none\n  logic_pass: false\n  context_lines: 3\n"), "gpt-oss:20b")
    assert (policy.review.reasoning, policy.review.verify) == ("low", "none") and applied == {"review.reasoning": "low"}
    assert model_settings_line("gpt-oss:20b", applied) == (
        "Set for `gpt-oss:20b`, since the policy file doesn't set it: `review.reasoning: low` (without it, the thinking uses up "
        "the output budget and most replies come back empty).")
    policy, applied = for_model(parse_policy("review:\n  verify: none\n"), "gpt-oss:20b")
    # Three or more are kept apart with semicolons, and a yes or no is written as the policy file would have it.
    assert model_settings_line("gpt-oss:20b", applied) == (
        "Set for `gpt-oss:20b`, since the policy file doesn't set them: `review.reasoning: low` (without it, the thinking uses up "
        "the output budget and most replies come back empty); `review.logic_pass: true` (with ten lines of context it found 27 of 39 "
        "planted bugs against 18, though a review takes about three times as long); `review.context_lines: 10` (the logic pass finds "
        "more with the code around each change, and either alone finds only one or two more).")
    pinned = parse_policy("review:\n  reasoning: default\n  verify: lenient\n  logic_pass: false\n  context_lines: 0\n")
    assert for_model(pinned, "gpt-oss:20b") == (pinned, {})


@pytest.mark.parametrize("name", ["qwen2.5-coder:7b", "", "openai/gpt-oss-20b", "gpt-oss-safeguard:20b", "my-gpt-oss:20b", "qwen3:14b"])
def test_other_models_keep_the_policy_as_it_is(name):
    from stratum_open.profiles import for_model, model_settings_line

    policy = parse_policy(None)
    assert for_model(policy, name) == (policy, {})
    assert model_settings_line(name, {}) == ""


def test_score_based_conclusions():
    from stratum_open.findings import Finding
    from stratum_open.github.publish import conclusion

    scored = parse_policy("review:\n  conclusion: score\n  fail_at: 70\n  warn_at: 50\n")
    high = [Finding("t", "d", "high")]
    assert conclusion(high, True) == "neutral"  # by severity: high is neutral
    assert conclusion(high, True, scored, score=75) == "failure"
    assert conclusion(high, True, scored, score=55) == "neutral"
    assert conclusion([], True, scored, score=10) == "success"
    assert conclusion([], False, scored, score=10) == "neutral"
