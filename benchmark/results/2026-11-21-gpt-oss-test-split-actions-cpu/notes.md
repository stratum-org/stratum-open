## The runs

All on GitHub's free runners (4 CPUs, 16 GB RAM, no GPU), 21 Nov 2026, from `main` at 1da45f1, with `gpt-oss:20b` and the Action's settings. Each reviewed the 20 test cases of [bugs.json](../../planted/bugs.json), the ones kept back while the variant was chosen on the dev cases, with the bug and as merged (the clean control).

| Run | Variant | Cases |
| --- | --- | --- |
| [44914547019](https://github.com/stratum-org/stratum-open/actions/runs/44914547019) | Today's default for `gpt-oss`: `review.reasoning: low` and `review.verify: strict`. These are the values `MODEL_SETTINGS` filled in, given as a policy file so that the variant keeps its meaning once the default changes. | test |
| [44914540299](https://github.com/stratum-org/stratum-open/actions/runs/44914540299) | `review.logic_pass: true` and `review.context_lines: 10` as well | test |

The same two were run on the test cases on 13 Nov ([notes](../2026-11-13-logic-pass-actions-cpu/notes.md)). They were run again because the review's code has changed since then (#106, #107, #121 and #208 among others), and today's code is what would ship.

One shard of the second run stopped after two hours, when the runner lost contact with GitHub, and left no results. It held the PRs with the bug of five cases: pallets/click#3858, psf/black#5637, Textualize/rich#3845, encode/httpx#3672 and pallets/werkzeug#3270. It was run again as the run's second attempt, on the same commit and inputs, and finished. Its results are in `result-gpt-oss-20b-planted-3`, so both runs count all 20 cases, with the bug and as merged.

## How the findings were marked

As on 13 Nov, every finding near a planted bug was read, and marked right when it describes the bug, wherever on the file's lines it sits within three of it.

This time the findings on the clean PRs were read too, since the decision turns on them, and so were the findings on the PRs with the bug that repeat one of them or sit in the code a clean finding was about. Seven other findings on the PRs with the bug weren't read.

## What it shows

On the test cases, side by side:

| | 13 Nov, default | 13 Nov, both settings | 21 Nov, default | 21 Nov, both settings |
| --- | --- | --- | --- | --- |
| Found, of the 20 | 9 | 13 | 5 | 14 |
| Findings on the 20 clean PRs | 4 | 7 | 3: 1 right, 1 unsure, 1 wrong | 7: 1 right, 6 wrong |
| Near the planted line on a clean PR | 1 | 0 | 0 | 0 |
| Median time per review | 223 s | 780 s | 199 s | 688 s |
| Slowest review | 775 s | 2,561 s | 630 s | 2,238 s |

Over both days, the two settings found 27 of the 40 reviews of a held-out case, and the default 14.

- **It holds on the held-out cases, twice:** 14 against 5 today, and 13 against 9 on 13 Nov.
- **It finds kinds of bug the default doesn't.** Today it caught an inverted `return` (pallets/click#3777), the `if t.TYPE_CHECKING:` (pallets/flask#5700), the root check turned the wrong way (psf/black#5637) and all four cases where `and` and `or` were swapped. The default found none of these.
- **What neither found today:** both off-by-ones, one of two dropped `await`s and one of two inverted `return`s.
- **The default found fewer than on 13 Nov:** 5 against 9, with the same model, the same settings and the same cases. The dev runs of the default found 7, 8 and 9 of 19, so part of this is the spread from one run to the next, and part may be the code that changed since. The two settings found about the same both times: 13 and 14.
- **The extra comments on clean code are mostly wrong.**
  - The right one is the same in both runs: pallets/jinja#2098 looked up the version of `werkzeug` instead of `jinja2`, and its maintainers fixed it two minutes after it merged.
  - Five of the six wrong ones claim something the code contradicts: a variable `rng` that isn't there, a `None` that the setter and `parse_cookie` each already handle, a call said to have no `try` around it that does, and a flag read as "is a coroutine" that means "warn". The sixth asks for a timeout on a pager, which runs until the user quits it.
  - None was near a planted line. That's 6 wrong comments on 20 clean PRs, against 1 for the default.
- **The cost is the wait.** A review's median is about 11.5 minutes on the free runner instead of about 3.3. The slowest took 37 minutes (43 on 13 Nov). The model's download adds a couple of minutes to each job.

## The decision

The two settings are now the default for `gpt-oss`, through `MODEL_SETTINGS` in `profiles.py`. The 13 Nov notes kept them off because of the wait. Issue #192 asked for them to be the default if the held-out cases confirmed them, with the time in the README so that people can choose. They did, twice.

The benchmark's model variants now set `logic_pass` and `context_lines` themselves, so a run of `reasoning-low-verify-strict` still measures the review without them.

A project that would rather have the review sooner turns both off in its policy file (`review.logic_pass: false` and `review.context_lines: 0`). The README's Models table lists both reviews. The example workflow's job limit went from 60 to 90 minutes, since a job stopped at the limit posts no model review.

## What it doesn't show

- **One model.** The `gpt-oss` pattern in `MODEL_SETTINGS` also matches `gpt-oss:120b`, which wasn't measured. On the `large` profile it already gets ten lines of context, so only the logic pass is new for it.
- **One kind of bug:** planted one-line logic bugs only. The second set (`bugs-2.json`) wasn't run with these settings.
- **Few cases.** There are 20 test cases, and the counts move by a few from run to run.
- **Not every finding was read:** seven findings away from the planted line, on the PRs with the bug.
