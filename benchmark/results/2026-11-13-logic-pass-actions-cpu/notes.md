## The runs

All on GitHub's free runners (4 CPUs, 16 GB RAM, no GPU), 13 Nov 2026, with `gpt-oss:20b`, the Action's settings, `review.reasoning: low` and `review.verify: strict`. Each reviewed the cases with the bug and as merged (the clean control).

| Run | Variant | Cases |
| --- | --- | --- |
| [43337430811](https://github.com/stratum-org/stratum-open/actions/runs/43337430811) | none (the baseline) | the 19 dev cases |
| [43337441855](https://github.com/stratum-org/stratum-open/actions/runs/43337441855) | `review.logic_pass: true` | dev |
| [43337477581](https://github.com/stratum-org/stratum-open/actions/runs/43337477581) | `logic_pass` and `review.context_lines: 10` | dev |
| [43366578242](https://github.com/stratum-org/stratum-open/actions/runs/43366578242) | `context_lines: 10` alone | dev |
| [43337455534](https://github.com/stratum-org/stratum-open/actions/runs/43337455534) | `review.samples: 2`: the same prompt asked twice and the answers merged | dev |
| [43337466413](https://github.com/stratum-org/stratum-open/actions/runs/43337466413) | `samples: 2` and `logic_pass` | dev |
| [43366589969](https://github.com/stratum-org/stratum-open/actions/runs/43366589969) | `logic_pass` and `context_lines: 10` | the 20 test cases, run once, after the variant was chosen on dev |
| [43366600863](https://github.com/stratum-org/stratum-open/actions/runs/43366600863) | none (the baseline) | test |

One shard of the test run (5 clean PRs) failed when Ollama's registry answered 503 at the end of the model download; it was rerun and is included. `scripts/start-ollama.sh` now retries the pull (#93).

`samples` was measured and then taken out of the code: it found nothing more. Its two runs are kept here as the record. Two more runs tried showing the whole enclosing function instead of ten lines; they found 7 of 19 alone and 12 with the logic pass, at 711 s a PR, so that change wasn't kept.

## How the findings were marked

Only the findings near a planted bug were marked, by the rule used for the earlier runs: right when the finding describes the planted bug, wherever on the file's lines it sits within three of it. The findings away from the planted line weren't read.

## What it shows

| Variant | Found, dev | Found, test | Near the line on a clean PR | Findings on the clean PRs | Median time per PR |
| --- | --- | --- | --- | --- | --- |
| none | 9 of 19 | 9 of 20 | 2 of 39 | 8 on 39 | 223 s |
| `context_lines: 10` | 10 of 19 | | 0 of 19 | 5 on 19 | 249 s |
| `logic_pass` | 11 of 19 | | 2 of 19 | 5 on 19 | 433 s |
| `logic_pass` and `context_lines: 10` | 14 of 19 | 13 of 20 | 1 of 39 | 15 on 39 | 676 s |

- **The two together find half as many again.** 27 of 39 against 18 for the same model and settings without them. It held on the test cases, which were run once, after the variant was picked on dev: 13 of 20 against 9 of 20. Earlier runs of the baseline on the dev cases found 7, 8 and 9, so one or two either way is sampling; nine more on 39 is not.
- **Neither half does it alone.** Ten lines of context found 10 of 19, the logic pass 11 of 19, both 14.
- **It finds kinds of bug the plain review never has.** Two of four inverted `return`s and two of four off-by-ones, against none of either in every earlier run of this model.
- **It isn't scoring by commenting on every line.** On the 39 clean PRs it was near the planted line once.
- **What it costs.**
  - **Time:** a review takes about three times as long, a median of 11 minutes a PR on the free runner, with the slowest at 43.
  - **Comments on clean code:** they nearly double, from 8 to 15 on 39 clean PRs. One of them is a real bug in a merged PR: jinja#2098 looked up the version of `werkzeug` instead of `jinja2`, and its maintainers fixed it two minutes after merging.

## Why it stays off by default

The time. A maintainer waiting on a review on the free runner waits three times as long. It's offered as an opt-in for projects that would rather have the catches: set `review.logic_pass: true` and `review.context_lines: 10`.

Since 21 Nov it is the default for `gpt-oss`, after the test cases were run again on newer code, with the time in the README's Models table ([notes](../2026-11-21-gpt-oss-test-split-actions-cpu/notes.md)).

## What it doesn't show

- One model. The logic pass's prompt was written for a model that can work through a list of decisions; it wasn't run on the 7B or 14B models.
- Planted one-line logic bugs only. Whether the logic pass helps on bugs of other kinds (the second planted set, `bugs-2.json`) isn't measured.
- Only findings near the planted line were read, so how many of the other comments are right isn't known.
