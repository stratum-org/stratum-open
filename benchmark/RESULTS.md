# Results

Every number the benchmarks give: its sample size, its 95% interval, how many runs it comes from, and how much of it a person has marked. How each benchmark works is in the [README](README.md).

This page is generated from the files in [results/](results/), so don't edit it. After adding or marking a run, run `python benchmark/results_page.py`. A test fails when the page is out of date, or when a README quotes a figure the results don't give.

**The intervals** are 95% Wilson score intervals, which hold up for small samples and for counts near none or all. Each takes the cases as a sample of the PRs a project might get, and says how far a count this small can be trusted. It can't say how well these cases stand for yours.

## The model's review: planted bugs

Real merged PRs, each with one bug planted on a line it adds. Where a run has the clean control, the same PRs were reviewed as merged too. Each row is one run of one model and variant: no row pools runs.

- **Variant:** the run's name for what it changed in the Action's settings. `-` is the settings as they come.
- **Cases:** how many PRs the run reviewed with the bug. *(dev)* is a run on the dev set alone, the cases a change is tuned on.
- **Found:** a finding within 3 lines of the bug, which a person read and marked as describing it.
- **At least:** a finding near a bug isn't marked yet, so the count is a lower bound. Its interval runs up to where it would end if every such case were marked found.
- **Near the line when clean:** clean PRs with a finding within 3 lines of where the bug goes.
- **Comments on the clean PRs:** the model's findings on the PRs as merged. Each is a possible false alarm, and all are counted, marked or not.
- **Failed calls** and **Median time per PR** cover the reviews with the bug and the clean ones together.
- **Not marked:** findings on the PRs with the bug that have no verdict yet, and how many of those are near a bug.
- **Runs:** how many runs of that model and variant are on this page. Where there's more than one, the next table sets them side by side.

| Model | Variant | Run | Cases | Found | 95% interval | Near the line when clean | Comments on the clean PRs | Per clean PR | Failed calls | Median time per PR | Not marked | Runs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `qwen2.5-coder:1.5b` | - | [2026-11-07-planted-actions-cpu](results/2026-11-07-planted-actions-cpu/) | 39 | 0 of 39 | 0% to 9.0% | - | - | - | 1 of 43 | 43s | 0 | 1 |
| `qwen2.5-coder:3b` | - | [2026-11-07-planted-actions-cpu](results/2026-11-07-planted-actions-cpu/) | 39 | 0 of 39 | 0% to 9.0% | - | - | - | 0 of 43 | 101s | 0 | 1 |
| `qwen2.5-coder:7b` | - | [2026-11-07-planted-actions-cpu](results/2026-11-07-planted-actions-cpu/) | 39 | 1 of 39 | 0.5% to 13% | - | - | - | 0 of 43 | 169s | 0 | 2 |
| `qwen2.5-coder:7b` | - | [2026-11-07-verify-actions-cpu](results/2026-11-07-verify-actions-cpu/) | 39 | at least 0 of 39 | 0% to 13% | 0 of 39 | 14 on 39 | 0.36 | 0 of 86 | 148s | 18 (1 near a bug) | 2 |
| `qwen2.5-coder:7b` | reasoning-low | [2026-11-08-gpt-oss-actions-cpu](results/2026-11-08-gpt-oss-actions-cpu/) | 19 (dev) | 0 of 19 | 0% to 17% | - | - | - | 0 of 21 | 98s | 6 | 1 |
| `qwen2.5-coder:7b` | verify | [2026-11-07-verify-actions-cpu](results/2026-11-07-verify-actions-cpu/) | 39 | 0 of 39 | 0% to 9.0% | 0 of 39 | 6 on 39 | 0.15 | 0 of 110 | 126s | 3 | 1 |
| `gemma3:12b` | reasoning-off | [2026-11-08-cache-fit-actions-cpu](results/2026-11-08-cache-fit-actions-cpu/) | 19 (dev) | 1 of 19 | 0.9% to 25% | 3 of 19 | 11 on 19 | 0.58 | 5 of 42 | 537s | 9 | 1 |
| `phi4:14b` | reasoning-off | [2026-11-08-cache-fit-actions-cpu](results/2026-11-08-cache-fit-actions-cpu/) | 19 (dev) | 3 of 19 | 5.5% to 38% | 7 of 19 | 37 on 19 | 1.95 | 8 of 42 | 572s | 19 | 1 |
| `qwen2.5-coder:14b` | - | [2026-11-07-planted-actions-cpu](results/2026-11-07-planted-actions-cpu/) | 39 | 10 of 39 | 15% to 41% | - | - | - | 7 of 43 | 409s | 0 | 2 |
| `qwen2.5-coder:14b` | - | [2026-11-07-verify-actions-cpu](results/2026-11-07-verify-actions-cpu/) | 39 | at least 0 of 39 | 0% to 54% | 13 of 39 | 57 on 39 | 1.46 | 5 of 86 | 353s | 51 (17 near a bug) | 2 |
| `qwen2.5-coder:14b` | verify | [2026-11-07-verify-actions-cpu](results/2026-11-07-verify-actions-cpu/) | 39 | at least 0 of 39 | 0% to 17% | 2 of 39 | 7 on 39 | 0.18 | 13 of 180 | 434s | 10 (2 near a bug) | 1 |
| `qwen3:14b` | reasoning-off | [2026-11-08-cache-fit-actions-cpu](results/2026-11-08-cache-fit-actions-cpu/) | 19 (dev) | 4 of 19 | 8.5% to 43% | 6 of 19 | 27 on 19 | 1.42 | 6 of 42 | 476s | 29 | 1 |
| `deepseek-coder-v2:16b` | - | [2026-11-08-moe-actions-cpu](results/2026-11-08-moe-actions-cpu/) | 39 | 4 of 39 | 4.1% to 24% | 12 of 39 | 90 on 39 | 2.31 | 0 of 86 | 143s | 78 | 1 |
| `gpt-oss:20b` | - | [2026-11-08-moe-actions-cpu](results/2026-11-08-moe-actions-cpu/) | 39 | 9 of 39 | 13% to 38% | 0 of 39 | 4 on 39 | 0.10 | 64 of 86 | 508s | 2 | 1 |
| `gpt-oss:20b` | logic-context10-verify-strict | [2026-11-13-logic-pass-actions-cpu](results/2026-11-13-logic-pass-actions-cpu/) | 39 | 27 of 39 | 54% to 81% | 1 of 39 | 15 on 39 | 0.38 | 3 of 276 | 676s | 11 | 2 |
| `gpt-oss:20b` | logic-context10-verify-strict | [2026-11-21-gpt-oss-test-split-actions-cpu](results/2026-11-21-gpt-oss-test-split-actions-cpu/) | 20 (test) | 14 of 20 | 48% to 85% | 0 of 20 | 7 on 20 | 0.35 | 3 of 146 | 688s | 5 | 2 |
| `gpt-oss:20b` | logic-verify-strict | [2026-11-13-logic-pass-actions-cpu](results/2026-11-13-logic-pass-actions-cpu/) | 19 (dev) | 11 of 19 | 36% to 77% | 2 of 19 | 5 on 19 | 0.26 | 1 of 118 | 433s | 7 | 1 |
| `gpt-oss:20b` | pr-agent-0.50.0-reasoning-low | [2026-11-13-pr-agent-actions-cpu](results/2026-11-13-pr-agent-actions-cpu/) | 19 (dev) | 10 of 19 | 32% to 73% | 9 of 19 | 20 on 19 | 1.05 | 0 of 108 | 599s | 15 | 1 |
| `gpt-oss:20b` | reasoning-low | [2026-11-08-gpt-oss-actions-cpu](results/2026-11-08-gpt-oss-actions-cpu/) | 19 (dev) | 6 of 19 | 15% to 54% | - | - | - | 0 of 21 | 186s | 3 | 2 |
| `gpt-oss:20b` | reasoning-low | [2026-11-08-moe-actions-cpu](results/2026-11-08-moe-actions-cpu/) | 39 | 12 of 39 | 19% to 46% | 2 of 39 | 16 on 39 | 0.41 | 4 of 86 | 166s | 11 | 2 |
| `gpt-oss:20b` | reasoning-low-verify-lenient | [2026-11-08-gpt-oss-actions-cpu](results/2026-11-08-gpt-oss-actions-cpu/) | 39 | 16 of 39 | 27% to 57% | 2 of 39 | 10 on 39 | 0.26 | 4 of 131 | 222s | 11 | 1 |
| `gpt-oss:20b` | reasoning-low-verify-strict | [2026-11-08-gpt-oss-actions-cpu](results/2026-11-08-gpt-oss-actions-cpu/) | 39 | 16 of 39 | 27% to 57% | 0 of 39 | 7 on 39 | 0.18 | 3 of 122 | 210s | 8 | 4 |
| `gpt-oss:20b` | reasoning-low-verify-strict | [2026-11-13-logic-pass-actions-cpu](results/2026-11-13-logic-pass-actions-cpu/) | 39 | 18 of 39 | 32% to 61% | 2 of 39 | 8 on 39 | 0.21 | 3 of 129 | 223s | 4 | 4 |
| `gpt-oss:20b` | reasoning-low-verify-strict | [2026-11-13-pr-agent-actions-cpu](results/2026-11-13-pr-agent-actions-cpu/) | 19 (dev) | 6 of 19 | 15% to 54% | 2 of 19 | 8 on 19 | 0.42 | 1 of 62 | 219s | 4 | 4 |
| `gpt-oss:20b` | reasoning-low-verify-strict | [2026-11-21-gpt-oss-test-split-actions-cpu](results/2026-11-21-gpt-oss-test-split-actions-cpu/) | 20 (test) | 5 of 20 | 11% to 47% | 0 of 20 | 3 on 20 | 0.15 | 2 of 57 | 199s | 2 | 4 |
| `gpt-oss:20b` | reasoning-low-verify-strict-context10 | [2026-11-13-logic-pass-actions-cpu](results/2026-11-13-logic-pass-actions-cpu/) | 19 (dev) | 10 of 19 | 32% to 73% | 0 of 19 | 5 on 19 | 0.26 | 0 of 69 | 249s | 4 | 1 |
| `gpt-oss:20b` | samples2-logic-verify-strict | [2026-11-13-logic-pass-actions-cpu](results/2026-11-13-logic-pass-actions-cpu/) | 19 (dev) | 11 of 19 | 36% to 77% | 2 of 19 | 6 on 19 | 0.32 | 0 of 162 | 497s | 3 | 1 |
| `gpt-oss:20b` | samples2-verify-strict | [2026-11-13-logic-pass-actions-cpu](results/2026-11-13-logic-pass-actions-cpu/) | 19 (dev) | 9 of 19 | 27% to 68% | 1 of 19 | 6 on 19 | 0.32 | 1 of 109 | 252s | 2 | 1 |
| `hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q3_K_S` | - | [2026-11-14-qwen3-coder-moe-actions-cpu](results/2026-11-14-qwen3-coder-moe-actions-cpu/) | 19 (dev) | 9 of 19 | 27% to 68% | 10 of 19 | 42 on 19 | 2.21 | 0 of 42 | 148s | 28 | 1 |
| `hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q3_K_S` | verify | [2026-11-14-qwen3-coder-moe-actions-cpu](results/2026-11-14-qwen3-coder-moe-actions-cpu/) | 19 (dev) | 0 of 19 | 0% to 17% | 0 of 19 | 1 on 19 | 0.05 | 0 of 134 | 186s | 0 | 1 |
| `hf.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:UD-Q2_K_XL` | verify | [2026-11-14-qwen3-coder-moe-actions-cpu](results/2026-11-14-qwen3-coder-moe-actions-cpu/) | 19 (dev) | 1 of 19 | 0.9% to 25% | 2 of 19 | 3 on 19 | 0.16 | 2 of 150 | 232s | 1 | 1 |
| `qwen/qwen-2.5-coder-32b-instruct` | - | [2026-11-07-planted-hosted](results/2026-11-07-planted-hosted/) | 39 | 12 of 39 | 19% to 46% | - | - | - | 0 of 43 | 13s | 0 | 1 |
| `qwen/qwen3-coder` | - | [2026-11-07-planted-hosted](results/2026-11-07-planted-hosted/) | 39 | 15 of 39 | 25% to 54% | - | - | - | 0 of 43 | 3s | 0 | 1 |

### Run more than once

The same review can catch different bugs from one run to the next, so one run's count isn't the last word. These models and variants have more than one:

| Model | Variant | Runs | Found in each run |
| --- | --- | --- | --- |
| `qwen2.5-coder:7b` | - | 2 | 1 of 39 in [2026-11-07-planted-actions-cpu](results/2026-11-07-planted-actions-cpu/); at least 0 of 39 in [2026-11-07-verify-actions-cpu](results/2026-11-07-verify-actions-cpu/) |
| `qwen2.5-coder:14b` | - | 2 | 10 of 39 in [2026-11-07-planted-actions-cpu](results/2026-11-07-planted-actions-cpu/); at least 0 of 39 in [2026-11-07-verify-actions-cpu](results/2026-11-07-verify-actions-cpu/) |
| `gpt-oss:20b` | logic-context10-verify-strict | 2 | 27 of 39 in [2026-11-13-logic-pass-actions-cpu](results/2026-11-13-logic-pass-actions-cpu/); 14 of 20 in [2026-11-21-gpt-oss-test-split-actions-cpu](results/2026-11-21-gpt-oss-test-split-actions-cpu/) |
| `gpt-oss:20b` | reasoning-low | 2 | 6 of 19 in [2026-11-08-gpt-oss-actions-cpu](results/2026-11-08-gpt-oss-actions-cpu/); 12 of 39 in [2026-11-08-moe-actions-cpu](results/2026-11-08-moe-actions-cpu/) |
| `gpt-oss:20b` | reasoning-low-verify-strict | 4 | 16 of 39 in [2026-11-08-gpt-oss-actions-cpu](results/2026-11-08-gpt-oss-actions-cpu/); 18 of 39 in [2026-11-13-logic-pass-actions-cpu](results/2026-11-13-logic-pass-actions-cpu/); 6 of 19 in [2026-11-13-pr-agent-actions-cpu](results/2026-11-13-pr-agent-actions-cpu/); 5 of 20 in [2026-11-21-gpt-oss-test-split-actions-cpu](results/2026-11-21-gpt-oss-test-split-actions-cpu/) |

Left out of every run: encode/httpx#3670. Not a bug: the stream's file comes from the builtin open(), so close() is synchronous and dropping its await fixes the line rather than breaking it. The merged PR awaits that file's read() and close() (lines 199, 205, 217), which raises TypeError on httpx's v1 branch.

## The checks that need no model

- **Run:** a run split into groups, such as one per language, has a row for each group.
- **Planted risks caught:** each risk is a PR adding files with one problem in them. Caught means the rule written for it fired.
- **Controls left alone:** files a rule must not flag, where the run has them.
- **Merged PRs:** real merged PRs as they are, where every finding is a possible false alarm. A person marks each one right (what it says is true) or wrong (a false alarm).
- **PRs with a wrong finding:** the false-alarm rate. It reads *at least* while a finding is still not marked.

| Run | Planted risks caught | 95% interval | Missed | Controls left alone | Merged PRs | Findings on them | Marked right | Marked wrong | Unsure | Not marked | PRs with a wrong finding | 95% interval |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [2026-11-07-checks](results/2026-11-07-checks/) | 43 of 44 | 88% to 99.6% | `dep-jellyfish` | - | 184 | 3 on 2 PRs | 3 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-checks-beyond-python](results/2026-11-13-checks-beyond-python/), [JavaScript and TypeScript](results/2026-11-13-checks-beyond-python/js/) | 43 of 44 | 88% to 99.6% | `dep-jellyfish` | - | 60 | 5 on 4 PRs | 4 | 0 | 1 | 0 | 0 of 60 | 0% to 6.0% |
| [2026-11-13-checks-beyond-python](results/2026-11-13-checks-beyond-python/), [Go](results/2026-11-13-checks-beyond-python/go/) | 43 of 44 | 88% to 99.6% | `dep-jellyfish` | - | 60 | 2 on 1 PRs | 2 | 0 | 0 | 0 | 0 of 60 | 0% to 6.0% |
| [2026-11-13-checks-beyond-python](results/2026-11-13-checks-beyond-python/), [Rust](results/2026-11-13-checks-beyond-python/rust/) | 43 of 44 | 88% to 99.6% | `dep-jellyfish` | - | 60 | 7 on 6 PRs | 6 | 0 | 1 | 0 | 0 of 60 | 0% to 6.0% |
| [2026-11-13-checks-beyond-python](results/2026-11-13-checks-beyond-python/), [Workflows and manifests](results/2026-11-13-checks-beyond-python/infra/) | 43 of 44 | 88% to 99.6% | `dep-jellyfish` | - | 53 | 73 on 30 PRs | 73 | 0 | 0 | 0 | 0 of 53 | 0% to 6.8% |
| [2026-11-13-osv](results/2026-11-13-osv/) | 49 of 50 | 90% to 99.6% | `dep-jellyfish` | 3 of 3 | 184 | 6 on 3 PRs | 6 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-removed-names](results/2026-11-13-removed-names/) | 72 of 77 | 86% to 97.2% | `dep-jellyfish`, `name-third-party`, `name-stdlib-unlisted-module`, `removed-method-of-a-class`, `removed-used-through-a-star-import` | 38 of 38 | 184 | 6 on 3 PRs | 6 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-ruff](results/2026-11-13-ruff/) | 98 of 99 | 94.5% to 99.8% | `dep-jellyfish` | - | 184 | 6 on 3 PRs | 6 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-unknown-names](results/2026-11-13-unknown-names/) | 60 of 63 | 87% to 98.4% | `dep-jellyfish`, `name-third-party`, `name-stdlib-unlisted-module` | 19 of 19 | 184 | 6 on 3 PRs | 6 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-weakened-tests](results/2026-11-13-weakened-tests/) | 7 of 7 | 65% to 100% | - | 6 of 6 | 782 | 15 on 9 PRs | 15 | 0 | 0 | 0 | 0 of 782 | 0% to 0.5% |

### Rules that are off by default

A run that turns such a rule on measures it on cases of its own, and its summary counts them apart. They are in the run's row above too.

- **Setting:** what turns the rules on in `.stratum/policy.yml`.
- **Planted cases caught** and **Controls left alone:** that setting's cases alone.
- **Findings on them:** the findings of those rules on the run's merged PRs.
- A rule *taken out* was measured on the merged PRs and then removed from its check, so it has no planted cases.

| Run | Setting | Rules | Planted cases caught | 95% interval | Controls left alone | 95% interval | Merged PRs | Findings on them | Marked right | Marked wrong | Unsure | Not marked | PRs with a wrong finding | 95% interval |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [2026-11-13-osv](results/2026-11-13-osv/) | `dependencies.known_vulnerabilities` | `deps/vulnerable` | 6 of 6 | 61% to 100% | 3 of 3 | 44% to 100% | 184 | 0 | 0 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-removed-names](results/2026-11-13-removed-names/) | `checks.unknown_names` | `code/unknown-name` | 17 of 19 | 69% to 97.1% | 19 of 19 | 83% to 100% | 184 | 0 | 0 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-removed-names](results/2026-11-13-removed-names/) | `checks.removed_names` | `code/removed-name` | 12 of 14 | 60% to 96.0% | 19 of 19 | 83% to 100% | 184 | 0 | 0 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-ruff](results/2026-11-13-ruff/) | `checks.ruff` | 55 rules, `ruff/*` | 55 of 55 | 93.5% to 100% | - | - | 184 | 0 | 0 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-unknown-names](results/2026-11-13-unknown-names/) | `checks.unknown_names` | `code/unknown-name` | 17 of 19 | 69% to 97.1% | 19 of 19 | 83% to 100% | 184 | 0 | 0 | 0 | 0 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-weakened-tests](results/2026-11-13-weakened-tests/) | `checks.weakened_tests` | `tests/assertions-removed`, `tests/only`, `tests/skipped`, `tests/tolerance-loosened` | 7 of 7 | 65% to 100% | 6 of 6 | 61% to 100% | 782 | 15 on 9 PRs | 15 | 0 | 0 | 0 | 0 of 782 | 0% to 0.5% |
| [2026-11-13-weakened-tests](results/2026-11-13-weakened-tests/) | `checks.weakened_tests` | `tests/deleted`, taken out | - | - | - | - | 782 | 19 on 17 PRs | 16 | 3 | 0 | 0 | 3 of 782 | 0.1% to 1.1% |

### Runs split into groups, all groups together

Each group is a row above, with the same planted risks each time, so only the merged PRs are added up.

| Run | Groups | Merged PRs | Findings on them | Marked right | Marked wrong | Unsure | Not marked | PRs with a wrong finding | 95% interval |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [2026-11-13-checks-beyond-python](results/2026-11-13-checks-beyond-python/) | JavaScript and TypeScript, Go, Rust, Workflows and manifests | 233 | 87 on 41 PRs | 85 | 0 | 2 | 0 | 0 of 233 | 0% to 1.6% |

## The missing-await checks

- **Language:** Python's check is on by default. The one for JavaScript and TypeScript is off unless `checks.floating_promises` is on.
- **Dropped awaits caught:** every `await` the merged PRs add, dropped one at a time. Caught means the check flagged that line.
- **PRs with a finding:** the same PRs as merged, where any finding is a possible false alarm. Nobody marks these, so each one counts.

| Run | Language | Dropped awaits caught | 95% interval | Merged PRs | Findings on them | PRs with a finding | 95% interval |
| --- | --- | --- | --- | --- | --- | --- | --- |
| [2026-11-07-missing-await](results/2026-11-07-missing-await/) | Python | 42 of 56 | 62% to 84% | 184 | 0 | 0 of 184 | 0% to 2.0% |
| [2026-11-13-floating-promises](results/2026-11-13-floating-promises/) | JavaScript and TypeScript | 42 of 126 | 26% to 42% | 60 | 0 | 0 of 60 | 0% to 6.0% |

### Merged code read as if it were new

Merged PRs add few lines, so a run can also read merged code as if a PR had just added every line of it. A finding there is right when nothing waits for the promise by mistake, and wrong (a false alarm) when the project means to leave the call running.

- **Right, of those marked:** findings marked right, of those marked right or wrong.

| Run | Read | Files | Lines | Findings | Right | Wrong | Unsure | Not marked | Right, of those marked | 95% interval |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [2026-11-13-floating-promises](results/2026-11-13-floating-promises/) | The files the PRs touch | 193 | 98,917 | 1 | 0 | 1 | 0 | 0 | 0 of 1 | 0% to 79% |
| [2026-11-13-floating-promises](results/2026-11-13-floating-promises/) | 12 whole repositories, `dev` | 19,108 | 2,300,697 | 27 | 6 | 16 | 5 | 0 | 6 of 22 | 13% to 48% |
| [2026-11-13-floating-promises](results/2026-11-13-floating-promises/) | 6 whole repositories, `test` | 11,959 | 1,562,067 | 28 | 6 | 16 | 6 | 0 | 6 of 22 | 13% to 48% |

## ShellCheck and actionlint

Both are off unless their setting is on. Each ran on merged PRs that change a shell script or a workflow, where any finding is a possible false alarm, and on the whole of those files read as if every line were new. Each finding is marked right when what it says is true of the code, and wrong (a false alarm) when the code works as meant.

| Setting | Run | Merged PRs | Added lines | Findings on them | Files read whole | Lines | Findings on them | Right | Wrong | Unsure | Right, of those marked | 95% interval |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `checks.shellcheck` | [2026-11-14-shell-checks](results/2026-11-14-shell-checks/) | 65 | 2,146 | 0 | 109 | 23,997 | 1 | 0 | 1 | 0 | 0 of 1 | 0% to 79% |
| `checks.actionlint` | [2026-11-14-shell-checks](results/2026-11-14-shell-checks/) | 129 | 1,467 | 0 | 144 | 24,353 | 21 | 9 | 11 | 1 | 9 of 20 | 26% to 66% |

## The model's review: merged PRs as they are

Real merged PRs with nothing planted, so each finding needs a maintainer to mark it. Until one does, a run has times and counts but no share right.

- **Right, of those marked:** findings marked right, of those marked right or wrong.
- **Not marked:** findings with no verdict yet, or marked unsure.

| Model | Run | PRs | Findings | Per PR | Right, of those marked | 95% interval | Not marked | Failed calls | Median time per PR |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `qwen2.5-coder:7b` | [2026-11-02-actions-cpu](results/2026-11-02-actions-cpu/) | 10 | 1 | 0.1 | - | - | 1 | 0 of 10 | 224s |
| `qwen2.5-coder:14b` | [2026-11-02-actions-cpu](results/2026-11-02-actions-cpu/) | 10 | 9 | 0.9 | - | - | 9 | 2 of 10 | 667s |

## Not included

These folders under results/ are in none of the tables above:

| Folder | Why |
| --- | --- |
| [2026-11-13-quiet](results/2026-11-13-quiet/) | it has no results that a scorer counts |
