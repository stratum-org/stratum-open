"""Clippy's correctness lints on the Rust crates a PR changes, when
`checks.clippy` is on and `cargo` is on PATH with the clippy component.

It runs in a checkout of the PR's head (see tools.py for where there is one,
and why a PR from a fork never gets one), as `cargo clippy` on the crate
whose `Cargo.toml` is nearest each changed `.rs` file. Only what it reports
on lines the PR adds is kept. Test files are skipped, and so are the test,
example and bench targets, which Clippy builds only with `--all-targets`.

Compiling the crate runs its code: its build scripts, the procedural macros
it uses, and any program `.cargo/config.toml` puts in rustc's place. That's
why a PR it mustn't build is refused. A crate that doesn't build gets a
coverage line, never a finding: a compile error is for the project's CI.

Only the lints of Clippy's `correctness` group are kept, CORRECTNESS, code
that is outright wrong or does nothing: `a == a`, an index past the end of
an array, `vec.resize(0, x)`. They're forced on with `--force-warn`, so an
`#[allow]` in the code, the PR's own included, can't hide one, and every
other Clippy lint is switched off, the style ones above all.
`--cap-lints warn` keeps the project's `#![deny(...)]` from turning a lint
into a build failure. With a `Cargo.lock` it runs `--locked`, so it never
rewrites one; without, cargo writes one as it builds, and it's removed after.

It hasn't been measured on merged PRs yet: building each project of the
benchmark's corpus takes minutes (benchmark/README.md).
"""

from __future__ import annotations

import json
import os

from stratum_open.checks import tools
from stratum_open.checks.common import extension, find_program, is_test_path
from stratum_open.diff import FileDiff
from stratum_open.findings import Finding

MAX_CRATES = 3
TIMEOUT_SECONDS = 600  # over every crate: a first build of a big crate's dependencies takes minutes
# Clippy's `correctness` group as of Clippy 1.100 (clippy_lints, `declare_clippy_lint!` with `correctness`).
# A lint a later Clippy adds to the group isn't kept until it's listed here.
CORRECTNESS = frozenset({
    "absurd_extreme_comparisons", "almost_swapped", "approx_constant", "async_yields_async", "bad_bit_mask",
    "cast_slice_different_sizes", "char_indices_as_byte_indices", "deprecated_semver", "derive_ord_xor_partial_ord",
    "derived_hash_with_manual_eq", "eager_transmute", "enum_clike_unportable_variant", "eq_op", "erasing_op", "if_let_mutex",
    "ifs_same_cond", "impl_hash_borrow_with_str_and_bytes", "impossible_comparisons", "ineffective_bit_mask", "infinite_iter",
    "inherent_to_string_shadow_display", "inline_fn_without_body", "invalid_regex", "inverted_saturating_sub",
    "invisible_characters", "iter_next_loop", "iter_skip_zero", "iterator_step_by_zero", "let_underscore_lock",
    "lint_groups_priority", "match_str_case_mismatch", "mem_replace_with_uninit", "min_max", "mistyped_literal_suffixes",
    "modulo_one", "mut_from_ref", "never_loop", "non_octal_unix_permissions", "nonsensical_open_options",
    "not_unsafe_ptr_arg_deref", "option_env_unwrap", "out_of_bounds_indexing", "panicking_overflow_checks", "panicking_unwrap",
    "possible_missing_comma", "read_line_without_trim", "recursive_format_impl", "redundant_comparisons", "reversed_empty_ranges",
    "self_assignment", "serde_api_misuse", "size_of_in_element_count", "suspicious_splitn", "transmute_null_to_fn",
    "transmuting_null", "uninit_assumed_init", "uninit_vec", "unit_cmp", "unit_hash", "unit_return_expecting_ord",
    "unsound_collection_transmute", "unused_io_amount", "useless_attribute", "vec_resize_to_zero", "while_immutable_condition",
    "wrong_transmute", "zst_offset",
})
LINT_FLAGS = ("-A", "clippy::all", "--force-warn", "clippy::correctness", "--cap-lints", "warn")


def available() -> bool:
    return find_program("cargo") is not None


def wanted(file: FileDiff) -> bool:
    """Rust code outside the tests and outside `vendor/`, where `cargo
    vendor` puts other projects' crates, each with its own `Cargo.toml`."""
    return extension(file.path) == ".rs" and not is_test_path(file.path) and "vendor" not in file.path.split("/")[:-1]


def check(files: list[FileDiff], worktree: tools.Worktree | None, notes: list[str]) -> list[Finding]:
    return tools.check(
        files, worktree, notes, name="Clippy", installed=available(), install="`cargo` isn't on PATH.",
        wanted=wanted, marker="Cargo.toml", max_roots=MAX_CRATES, timeout=TIMEOUT_SECONDS, run_root=run_crate,
    )


def run_crate(worktree: str, root: str, paths: list[str], seconds: float) -> tuple[list[tools.Issue], str]:
    """`cargo clippy` on the crate at `root`; its issues on `paths`."""
    program = find_program("cargo")
    if program is None:
        return [], "Clippy didn't run: `cargo` isn't on PATH any more."
    folder = os.path.join(worktree, *root.split("/")) if root else worktree
    # The crate's folder and the ones above it in the worktree: the workspace's root is one of them, and
    # the file names Clippy prints are relative to it.
    above = [os.path.join(worktree, *root.split("/")[:depth]) for depth in range(len(root.split("/")) if root else 0, -1, -1)]
    locked = {folder for folder in above if os.path.isfile(os.path.join(folder, "Cargo.lock"))}
    command = [program, "clippy", "--manifest-path", os.path.join(folder, "Cargo.toml"), "--message-format=json", "--quiet",
               *(["--locked"] if locked else []), "--", *LINT_FLAGS]
    try:
        result = tools.run(command, cwd=folder, timeout=seconds, name="Clippy", env=tools.build_env())
    finally:
        for unlocked in set(above) - locked:  # a Cargo.lock this run wrote
            try:
                os.remove(os.path.join(unlocked, "Cargo.lock"))
            except FileNotFoundError:
                pass
    targets = set(paths)
    issues, error = [], ""
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        message = item.get("message") if isinstance(item, dict) and item.get("reason") == "compiler-message" else None
        if not isinstance(message, dict):
            continue
        code = str((message.get("code") or {}).get("code") or "")
        if message.get("level") == "error":
            error = error or str(message.get("message") or "")
            continue
        lint = code.removeprefix("clippy::")
        if not code.startswith("clippy::") or lint not in CORRECTNESS:
            continue
        for span in message.get("spans") or []:
            if not isinstance(span, dict) or not span.get("is_primary") or not isinstance(span.get("line_start"), int):
                continue
            named = str(span.get("file_name") or "")
            path = next((p for p in (tools.relative(worktree, os.path.join(base, named)) for base in above) if p in targets), None)
            if path is None:
                continue
            text = str(message.get("message") or "")
            issues.append(tools.Issue(
                path=path, line=span["line_start"], end_line=span.get("line_end") or span["line_start"], rule=f"clippy/{lint}",
                title=tools.title("Clippy", lint, text), severity="high",
                detail=(f"Clippy reports `clippy::{lint}`: `{text.replace('`', chr(39)).strip()[:300]}`. It's one of Clippy's "
                        "correctness lints, for code that is outright wrong or does nothing."),
                suggestion=f"See https://rust-lang.github.io/rust-clippy/master/index.html#{lint} for what it means and how to fix it.",
            ))
            break
    problem = ""
    if error or result.returncode != 0:
        where = f"the crate in `{root}`" if root else "the crate"
        first = tools.first_line(error) or tools.first_line(result.stderr)
        reason = tools.code_span(first) if first else f"exit code {result.returncode}"
        problem = f"Clippy couldn't build {where} ({reason}), so its findings there are missing."
    return issues, problem
