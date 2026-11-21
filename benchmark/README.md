# Small-model benchmark

Can a 7–14B open model running on a maintainer's own hardware review PRs well
enough to help? This folder holds the script that measures it and the results.

## Method

1. Take real merged PRs ([prs.json](prs.json)). Merged PRs passed their
   maintainers' review, so most findings on them should be wrong, and a model
   that flags little on them is doing its job.
2. Review each one with every model under test, with the same prompt, policy
   defaults and context window. Only the model's findings are counted: the
   checks that need no model are the same whatever the model.
3. Maintainers mark each finding in `findings.csv` as `right`, `wrong` or
   `unsure`.
4. `bench.py score` then reports, per model: findings marked right and wrong,
   failed calls, and the median time per PR.

```sh
python benchmark/bench.py run --prs benchmark/prs.json --out benchmark/results/<name> \
    --model qwen2.5-coder:7b@http://localhost:11434/v1 \
    --model qwen2.5-coder:14b@http://localhost:11434/v1
python benchmark/bench.py score benchmark/results/<name>
```

To measure on GitHub's free runners (4 CPUs, no GPU), the machine the Action
reviews on, start the **Benchmark** workflow from the Actions tab. Its results
come back as an artifact.

A hosted model can be compared the same way: pass its OpenAI-compatible
endpoint, with the key in `STRATUM_MODEL_API_KEY`.

## Planted bugs

Marking findings on merged PRs needs a maintainer for every row, so
[planted.py](planted.py) also measures against an answer known in advance.

- **The model's review.** [bugs.json](planted/bugs.json) is 40 real merged PRs (one since found not to be a bug, and left out: see its `invalid`)
  from 12 Python projects, each with one bug planted on a line it adds: a
  flipped comparison, a negated condition, `and` for `or`, an inverted return,
  a dropped `await`, an off-by-one. Each case says what the bug breaks. Each
  model reviews them as the Action does (the `small` profile, an 8,192-token
  window). A finding within 3 lines of the bug is near it; each finding is
  then read and marked: whether it describes the bug (`found`), and whether
  each other finding is a real problem, with the reason in its `note`. Start the **Planted-bug benchmark** workflow to run it on
  GitHub's free runners, one job per model and shard.
- **The checks that need no model.** [risks.yml](planted/risks.yml) is 114
  planted risks (secrets, code that runs input, a dropped promise, a weakened test, workflow holes, typosquatted
  and made-up packages, versions with known advisories, coding-agent files,
  hidden Unicode), after real incidents where there was one, an error for
  each rule of the Ruff check, and 10 controls a rule must leave alone.
  [clean.json](planted/clean.json) is 184 merged PRs as they are, where every
  finding is a possible false alarm. `--policy` takes a policy file, for a
  rule that is off by default: the six known-advisory cases are missed
  without [known-vulnerabilities.yml](planted/variants/known-vulnerabilities.yml).
  [whole_files.py](whole_files.py) runs the Ruff check's rules, and the ones
  left out of it, on every line of those PRs' Python files, since on the
  added lines alone they find next to nothing to judge.
  Those PRs are from Python projects. [corpus.py](corpus.py) picks 233 more from
  JavaScript and TypeScript, Go and Rust projects (`clean-js.json`,
  `clean-go.json`, `clean-rust.json`) and from PRs that change workflows or
  dependency manifests (`clean-infra.json`), and sums up a run on them.
  `--policy` takes a policy file, for a rule that is off by default: the two
  dropped-promise cases are missed without
  [floating-promises.yml](planted/variants/floating-promises.yml) and the
  `js` extra.
- **The floating-promise check.** [floating_promises.py](floating_promises.py)
  drops every `await` the PRs in `clean-js.json` add, one at a time, and asks
  whether the check flags the line. It also runs the check on those PRs as
  merged, and on merged code as if every line were new: the files the PRs
  touch, and the 18 repositories pinned in
  [repos-js.json](planted/repos-js.json), six of them kept back until the
  rule was settled. Each finding is then read and marked.

```sh
python benchmark/planted.py checks --risks benchmark/planted/risks.yml --prs benchmark/planted/clean.json \
    --out benchmark/results/<name> [--policy benchmark/planted/variants/known-vulnerabilities.yml]
python benchmark/planted.py run --bugs benchmark/planted/bugs.json --out benchmark/results/<name>/raw/7b \
    --model qwen2.5-coder:7b@http://localhost:11434/v1
python benchmark/planted.py score benchmark/results/<name>   # after marking the verdict columns
```

**Weakened tests.** The check for tests a PR weakens is off unless `checks.weakened_tests` is on, and
[weakened_tests.py](weakened_tests.py) measures it alone:
- **Planted.** risks.yml also holds its cases and its controls (a rewritten assertion, a skip a platform decides, a renamed test), which the check must leave alone. Each is the PR's diff, not a set of new files, because the rules read what a PR removes. `planted.py checks` counts them as missed without `--policy benchmark/planted/variants/weakened-tests.yml`.
- **Merged PRs.** Every list above, and two more that [corpus.py](corpus.py) picks from the same JavaScript, Go and Rust projects: `clean-tests.json`, PRs that remove a line from a test file, and `clean-flaky.json`, PRs whose title is about a flaky test or about skipping one.
- **Marking.** Each finding is right or wrong, and a right one is either plainly fine (the PR says it, or it follows from what the PR says it does) or worth pointing out.

```sh
python benchmark/weakened_tests.py run --out benchmark/results/<name>
python benchmark/weakened_tests.py score benchmark/results/<name>   # after marking checks.csv and markers.csv
```

**A check that reads the repository.** The unknown-names check (off by default) needs each project's files, not only
a PR's diff. [unknown_names.py](unknown_names.py) clones the projects of clean.json, and `checks --repos` then reads each
merged PR against its project as it was just before the PR merged. Merged PRs add few imports, so `source` reads every
line of each project as if a PR had just added it, and counts how many names the check could decide. Its 19 planted
cases and 19 controls are in risks.yml, each with the `repo` files its PR imports from.

```sh
python benchmark/unknown_names.py clone --repos ../repos
python benchmark/planted.py checks --risks benchmark/planted/risks.yml --prs benchmark/planted/clean.json \
    --out benchmark/results/<name> --policy benchmark/planted/variants/unknown-names.yml --repos ../repos
python benchmark/unknown_names.py source --repos ../repos --out benchmark/results/<name>
```

**A check that reads what a PR removes.** The removed-names check (off by default) is the unknown-names check the other
way round: a name a PR takes out of a module while another file still imports or uses it. Its 14 planted cases and 19
controls are in risks.yml too, each a change to its `repo` files instead of new ones, and `checks --repos` reads the
merged PRs with it as above. Merged PRs remove few names that anything used, so [removed_names.py](removed_names.py)
`commits` also reads the last 300 commits of each project, each as a PR against its parent: once as merged, where a
finding is a false alarm or a break that got merged, and once for each file that loses a definition with the rest of the
commit left out, where the lines the commit went on to change are the answer.

```sh
python benchmark/planted.py checks --risks benchmark/planted/risks.yml --prs benchmark/planted/clean.json \
    --out benchmark/results/<name> --policy benchmark/planted/variants/removed-names.yml --repos ../repos
python benchmark/removed_names.py commits --repos ../repos --out benchmark/results/<name>
```

**Checks that run another program.** `checks.shellcheck` and `checks.actionlint` (both off by default) keep a short list of
what ShellCheck and actionlint report. [shell_checks.py](shell_checks.py) picks merged PRs that change a shell script or a
workflow (`clean-shell.json`), and writes down every issue the two programs report on a line those PRs add, kept or not.
`whole` then reads the same files as if every line were new. Each row is marked, and what the checks keep was chosen from
the marks. Run it under Linux with both programs on PATH.

```sh
python benchmark/shell_checks.py pick
python benchmark/shell_checks.py run --out benchmark/results/<name>
python benchmark/shell_checks.py whole --out benchmark/results/<name>
python benchmark/shell_checks.py score benchmark/results/<name>   # after marking issues.csv and whole.csv
```

**Checks that build the code.** `checks.go_vet` and `checks.clippy` (both off by default) compile the PR's code, so they
need each PR checked out at its head, with its dependencies, and not only its diff and files, which is all `planted.py
checks` and the corpus fetch. [build_checks.py](build_checks.py) clones each of the 12 Go and Rust projects of
`clean-go.json` and `clean-rust.json` once, checks out each of their 120 PRs at its head (`pull/N/head`), runs both checks
on it as a review would, and writes down every issue go vet, staticcheck and Clippy report on a line the PR adds, kept or
not, with the seconds each build took, what it built (a build that built nothing counts as one that didn't build) and the
disk space it used. staticcheck is asked for every SA check and Clippy for its `suspicious` group too,
so what the checks leave out is measured from the same build. The first PR of each project is built with nothing
cached, as on a fresh runner; the others reuse what the PRs before them downloaded and built. Building a project runs its
code, so start the **Build-check benchmark** workflow: it runs on GitHub's runners, one job per project, and builds inside
a container made from [Dockerfile](Dockerfile), away from the runner and its credentials. It hasn't been run yet. Until it has, their README entries say they're unmeasured, and what they keep is the tools' own
correctness rules: every go vet analyzer but `composites` and `tests`, staticcheck's SA checks but SA1019 and SA5009, and
Clippy's `correctness` group.

```sh
python benchmark/build_checks.py fetch   # the PRs' diffs, the one step that needs a GitHub token
python benchmark/build_checks.py run --repos ../repos --out benchmark/results/<name>/raw/<project> --repo <owner/name>
python benchmark/build_checks.py score benchmark/results/<name>   # after marking issues.csv
```

**Trying a change.** Each case is in the `dev` or the `test` set (in bugs.json, stratified by kind):
- **Tune on dev only.** Use `--set dev`, and run `test` once at the end, so the result isn't tuned to its cases.
- **Add the clean control.** `--clean` reviews the PRs as merged, without the bug. A method that comments on every changed condition flags the planted line in both, so the summary counts **near only with the bug**: the cases whose clean version got no finding on that line.
- **Name what you change.** `--policy change.yml --variant <name>` merges a policy file over the Action's settings and keeps the name with the results.

```sh
python benchmark/planted.py run --bugs benchmark/planted/bugs.json --set dev --out benchmark/results/<name>/raw/7b-planted \
    --model qwen2.5-coder:7b@http://localhost:11434/v1 --policy change.yml --variant change
python benchmark/planted.py run --bugs benchmark/planted/bugs.json --set dev --clean --out benchmark/results/<name>/raw/7b-clean \
    --model qwen2.5-coder:7b@http://localhost:11434/v1 --policy change.yml --variant change
```

### A second set, with other kinds of bug

The first set's bugs are all one-line logic flips, and a review tuned on seven kinds of flip can look better than it is. [bugs-2.json](planted/bugs-2.json) is 32 more cases in the same format: eight of each of four kinds, half `dev` and half `test`.

- **`swapped-arguments`:** two arguments of a call exchanged, where both are of the same type, like `urljoin(target, url)`.
- **`wrong-variable`:** a neighbouring variable or attribute of a fitting type where the right one should be.
- **`wrong-constant`:** an index, a key, a limit or a mask changed to another plausible one.
- **`dropped-statement`:** a statement the PR wrote, left out. Its `replace` is `null`. The diff is then numbered as if the line was never written, and a finding is near within 3 lines of the gap.

How the cases were chosen:
- **The PRs:** merged PRs from 11 of the first set's 12 projects (requests had too few that fit). Each changes Python outside the tests, adds at most 300 lines and touches at most 10 files. None is in the first set or in clean.json.
- **The bugs:** each is on a line the PR adds and changes what the code does, while the file still parses and every name on the line is defined and of a fitting type. Where there was a choice, the bug goes against a test, a docstring or a comment of the same PR, which is how a careful reviewer would catch it.
- **What a diff can show:** the planted line is never one the PR's diff already holds. A dropped line has the code that needs it right beside it, and another added line in its hunk: a hunk that only held the dropped line wouldn't be in the diff at all.

`validate` checks every case against its PR: the line is one the PR adds and reads `find`, and the file parses with the bug in it. With `--strict` it also refuses what it otherwise only notes, such as a `find` that several added lines match (three cases of the first set).

```sh
python benchmark/planted.py fetch --bugs benchmark/planted/bugs-2.json     # cache the PRs
python benchmark/planted.py validate --bugs benchmark/planted/bugs-2.json --strict
python benchmark/planted.py run --bugs benchmark/planted/bugs-2.json --set dev --out benchmark/results/<name>/raw/7b \
    --model qwen2.5-coder:7b@http://localhost:11434/v1
```

On GitHub's runners, give the **Planted-bug benchmark** workflow the file as its `bugs` input. Results on the second set are reported separately from the first set's, in a run folder of their own, so every "of 39" stays comparable.

## Other reviewers

[others/](others/README.md) runs another open-source reviewer, PR-Agent,
through the same planted bugs with the same local model on the same runner,
and writes the same files, so `planted.py score` puts both in one table. Its
README says how PR-Agent is set up and what the comparison doesn't show.

## Results

Each run is a folder in [results/](results/), holding its `summary.md`, every
finding and every call. The machine each run used is noted in its summary.

[RESULTS.md](RESULTS.md) puts every run's numbers on one page, each with its
sample size, a 95% interval and how much of it has been marked. It's generated:
after adding or marking a run, run `python benchmark/results_page.py`.

| Run | Machine | Models |
| --- | --- | --- |
| [2026-11-02-actions-cpu](results/2026-11-02-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | `qwen2.5-coder:7b`, `qwen2.5-coder:14b` |
| [2026-11-07-checks](results/2026-11-07-checks/summary.md) | Any (no model) | The checks: 43 of 44 planted risks caught, no false alarms on 184 merged PRs |
| [2026-11-13-unknown-names](results/2026-11-13-unknown-names/summary.md) | Any (no model) | `checks.unknown_names` on: 17 of 19 planted names caught (the 2 misses are out of its reach), 19 of 19 controls left alone, nothing on the lines 184 merged PRs add; on every line of the 17 projects, 4 findings, all 4 right |
| [2026-11-13-removed-names](results/2026-11-13-removed-names/summary.md) | Any (no model) | `checks.removed_names` on: 12 of 14 planted removals caught (the 2 misses are out of its reach), 19 of 19 controls left alone, nothing on 184 merged PRs or on 5,100 commits as merged; one file of a commit at a time, 144 of the 320 removed definitions the commit also took out of another file, and every one of the 307 lines it named changed by that commit |
| [2026-11-07-planted-actions-cpu](results/2026-11-07-planted-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | Planted bugs found: `qwen2.5-coder` 1.5B 0, 3B 0, 7B 1, 14B 10, of 39 |
| [2026-11-07-planted-hosted](results/2026-11-07-planted-hosted/summary.md) | OpenRouter (reference) | Planted bugs found: `qwen-2.5-coder-32b` 12, `qwen3-coder` 15, of 39, with the same prompt |
| [2026-11-07-verify-actions-cpu](results/2026-11-07-verify-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | `review.verify` against the current method, 7B and 14B, with the clean control |
| [2026-11-08-moe-actions-cpu](results/2026-11-08-moe-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | Mixtures of experts: `gpt-oss:20b` finds 12 of 39 with `review.reasoning: low`, `deepseek-coder-v2:16b` 4 |
| [2026-11-08-gpt-oss-actions-cpu](results/2026-11-08-gpt-oss-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | `gpt-oss:20b` with `reasoning: low` and each `verify`: 16 of 39 found with strict verify, 7 comments on 39 clean PRs |
| [2026-11-08-cache-fit-actions-cpu](results/2026-11-08-cache-fit-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | Models that fit the Actions cache, on the 19 dev cases: `qwen3:14b` 4, `phi4:14b` 3, `gemma3:12b` 1, against 7 for `qwen2.5-coder:14b` |
| [2026-11-13-osv](results/2026-11-13-osv/summary.md) | Any (no model) | `dependencies.known_vulnerabilities` on: 6 of 6 pins with known advisories caught, 3 of 3 controls left alone. No pinned version in the 184 merged PRs; in 47 more that change a manifest, 3 of 18 pins flagged, all 3 right |
| [2026-11-13-ruff](results/2026-11-13-ruff/summary.md) | Any (no model) | The Ruff check (`checks.ruff`, off by default): 55 of 55 planted errors caught and no finding on the lines 184 merged PRs add; 53 of its 55 rules fire nowhere in those files, so their false alarms aren't measured |
| [2026-11-13-checks-beyond-python](results/2026-11-13-checks-beyond-python/summary.md) | Any (no model) | The checks on 233 merged PRs from JavaScript and TypeScript, Go and Rust projects: 87 findings, none plainly wrong, 2 unsure. The rules for code had almost nothing to read there |
| [2026-11-13-floating-promises](results/2026-11-13-floating-promises/summary.md) | Any (no model) | `checks.floating_promises` on JavaScript and TypeScript: 42 of 126 dropped awaits caught, nothing on 60 merged PRs. On 1.56 million lines of six other projects read as new, 28 findings, 6 of them real mistakes and 16 false alarms |
| [2026-11-13-pr-agent-actions-cpu](results/2026-11-13-pr-agent-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | PR-Agent 0.50.0 on the 19 dev cases with the same `gpt-oss:20b`: 10 found against 6 for stratum-open in the same run, and 20 findings on the 19 clean PRs against 8 ([notes](results/2026-11-13-pr-agent-actions-cpu/notes.md)) |
| [2026-11-13-logic-pass-actions-cpu](results/2026-11-13-logic-pass-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | `gpt-oss:20b` with `review.logic_pass` and `review.context_lines: 10`: 27 of 39 found against 18 without them, 15 comments on 39 clean PRs against 8, about three times as long per PR ([notes](results/2026-11-13-logic-pass-actions-cpu/notes.md)) |
| [2026-11-14-qwen3-coder-moe-actions-cpu](results/2026-11-14-qwen3-coder-moe-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | `Qwen3-Coder-30B-A3B` at 2 and 3 bits on the 19 dev cases: 1 and 0 found with `review.verify: strict` against 8 and 9 for `gpt-oss:20b`; 9 without verify, but with 42 comments on 19 clean PRs ([notes](results/2026-11-14-qwen3-coder-moe-actions-cpu/notes.md)) |
| [2026-11-14-shell-checks](results/2026-11-14-shell-checks/summary.md) | Any (no model) | `checks.shellcheck` and `checks.actionlint` (off by default): nothing on the lines 65 and 129 merged PRs add to scripts and workflows. On those files read as new, 1 false alarm from ShellCheck in 23,997 lines; 21 findings from actionlint in 24,353, 9 right but harmless and 11 false alarms, each something GitHub added after actionlint 1.7.12 |
| [2026-11-21-gpt-oss-test-split-actions-cpu](results/2026-11-21-gpt-oss-test-split-actions-cpu/summary.md) | GitHub runner, 4 CPUs, no GPU | `gpt-oss:20b` with `review.logic_pass` and `review.context_lines: 10` on the 20 cases kept back from tuning, run again on the code of 21 Nov: 14 of 20 found, against 5 of 20 for the default; 7 comments on 20 clean PRs, 6 of them wrong, against 3. They're now the default for `gpt-oss` ([notes](results/2026-11-21-gpt-oss-test-split-actions-cpu/notes.md)) |

One folder holds no run of its own: [2026-11-13-quiet](results/2026-11-13-quiet/summary.md) tries the patterns `review.quiet` uses
on the findings the runs above left and people marked. They leave out 41 of the 215 marked wrong and none of the 220 marked right
([notes](results/2026-11-13-quiet/notes.md)). With nothing in it a scorer counts, RESULTS.md lists it at the end.
