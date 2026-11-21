# Data

What this project stores, for how long, and what leaves the maintainers'
infrastructure.

This page changes in the same PR as any code that stores something, and a
test fails when a stored field isn't listed here.

## Install modes

| Mode | Where it runs | What it keeps |
| --- | --- | --- |
| GitHub Action | The project's own CI runners | Nothing of its own between runs (see below) |
| GitHub App | A server the maintainers run | The rows in the table below, and a clone of each repo's default branch |
| CLI | A maintainer's machine | Nothing; results go to the terminal or a JSON file |

**The Action keeps its memory on GitHub.** Stratum's summary comment on each PR
carries a hidden copy of the last review's findings (title, detail, file,
lines, the code they're on), of what a maintainer ignored, and of the paths
of any files the model hasn't read yet, each with how many reviews of the
commit got no usable answer for it. That's how the next push reviews only
what changed, plus those files, and how a file that never gets one stops
being sent again on the same commit. With `review.early_checks`, while the
model runs it also holds the commit, the check run's ID and the fingerprints
of the comments the checks posted first, so the full review can complete
them; the full review removes it. It's the same text the PR's comments
already show, it's deleted with the comment, and Stratum only reads it from a
comment it wrote itself. With `review.learn_from_history` on, each review also
reads the repo's latest comments, and the 👍/👎 and access of the people who
reacted to Stratum's, from GitHub; it keeps none of it. With `ollama-model` set, GitHub's Actions cache also
keeps Ollama and the model's weights (no project data); GitHub drops it after
7 days unused.

## What is stored and for how long

This applies to the GitHub App. The Action and the CLI store none of it.

| Data | Default retention | How it's deleted |
| --- | --- | --- |
| Review findings and summaries | 180 days; the PR comments stay on GitHub | Automatically, or `stratum-open data delete --repo owner/name --pr <number>` |
| 👍/👎 feedback and muted rules | Until a maintainer deletes them | `stratum-open data delete --feedback [--repo owner/name]`; unmuting a rule also deletes its votes, and a vote taken back on GitHub is deleted at the PR's next review |
| Track records | Not stored: rebuilt from git history on every run | Nothing to delete; `track_record.exclude` stops one being built |
| Job log: PR, event, time, outcome, errors | 14 days | Automatically, or with the PR's findings by `stratum-open data delete --repo owner/name --pr <number>` |
| A clone of each repo's default branch, with the history in `track_record.window_days` | On the App's disk while the App is installed; updated on each run | Deleted at once when the App is uninstalled from the repo |
| PR diffs, model prompts and replies | Never stored | Nothing to delete |
| App private key, webhook secret, model API key | Environment variables on the server, never the database | Rotate them at GitHub or the model provider |

`data.findings_retention_days` changes the 180 days (when the App can't read a
repo's policy, the 180 days apply). The App purges every repo it's installed on
once a day, so this holds for repos with no PRs lately. `review.log_prompts: true`
keeps prompts and replies in the job log for its 14 days. Uninstalling the App
deletes its rows at once, and drops any job still waiting for the repo; what a
job running at that moment writes is deleted when it ends.

## Stored fields

Every column the GitHub App's database has, so this list can be checked against
the code: a test fails when a column is missing here, or listed here but gone.
Repos (`owner/name`) and logins are kept in lower case, and times in UTC.
Anything shaped like a credential (a GitHub token, an API key, a private key)
is replaced with `[redacted]` before it's written.

**reviews:** one row per review the App posts. Deleted after
`data.findings_retention_days`.

- `reviews.id`: a number that links the review's findings to it.
- `reviews.repo`: the repo.
- `reviews.pr`: the PR number.
- `reviews.head_sha`: the commit that was reviewed.
- `reviews.risk`: the 0–100 risk score.
- `reviews.summary`: the model's one- or two-sentence summary of the PR, as posted on it.
- `reviews.created_at`: when it was recorded.

**findings:** one row per finding in a review. Deleted with the review. A
finding's suggested fix and replacement code are never kept.

- `findings.review_id`: the review it belongs to.
- `findings.fingerprint`: its id, which stays the same when lines move.
- `findings.rule`: the check or model rule that found it.
- `findings.severity`: low, medium, high or critical.
- `findings.category`: bug, security, tests and so on.
- `findings.path`: the file, or empty for the PR as a whole.
- `findings.line_start`: its first line, or empty for the whole file.
- `findings.line_end`: its last line.
- `findings.title`: its one-line title.
- `findings.detail`: what the model or check said about the code, as posted on
  the PR.

**jobs:** the job log, one row per job the App runs. Deleted after 14 days.

- `jobs.repo`: the repo.
- `jobs.pr`: the PR number, or empty when the job isn't about a PR.
- `jobs.event`: the GitHub event that started it.
- `jobs.outcome`: how it ended, such as reviewed, skipped or failed.
- `jobs.error`: the error message, when there was one.
- `jobs.prompt_log`: the prompts sent to the model and its replies, only when
  the policy sets `review.log_prompts: true`. Empty otherwise.
- `jobs.created_at`: when it ran.

**votes:** 👍/👎 reactions on the App's comments. Kept until a maintainer
deletes them, or until the vote is taken back: each time the App reviews a
PR, it reads the reactions on that PR's comments again, and deletes a stored
vote that's no longer there or whose voter no longer has write access. A vote
stays when GitHub won't say whether its voter has write access.

- `votes.repo`: the repo.
- `votes.comment_id`: the comment reacted to.
- `votes.voter`: the voter's GitHub login, kept only so each person has one
  vote per comment.
- `votes.rule`: the rule of the finding voted on.
- `votes.fingerprint`: the finding voted on.
- `votes.vote`: 1 for 👍, -1 for 👎.
- `votes.voted_at`: when the vote was last changed.

**mutes:** rules a maintainer muted by hand. Kept until unmuted or deleted.
Rules muted by votes have no row here: they're worked out from `votes` each
time.

- `mutes.repo`: the repo.
- `mutes.rule`: the rule.
- `mutes.muted_by`: the maintainer's GitHub login.
- `mutes.muted_at`: when it was muted.

## What leaves the maintainers' infrastructure

- **GitHub API calls:** reading the PR, its diff and the repo history; posting
  comments, labels, the check run and the triage queue issue.
- **The model endpoint in the policy file.** With `model: local`, the default,
  that's a server the maintainers run, so code stays with them.
- **Package registries** (PyPI, npm, crates.io): only the names of
  dependencies a PR adds, to check how new they are.
  `dependencies.registry_lookup: false` turns this off.
- **The OSV database** (api.osv.dev), only when the policy sets
  `dependencies.known_vulnerabilities: true`: the name, ecosystem and version
  of each dependency a PR pins to an exact version, to ask for that version's
  published advisories. `dependencies.registry_lookup: false` turns this off
  too.
- **Ollama's downloads**, only when the Action's `ollama-model` is set: Ollama
  itself from GitHub, and the model's weights from Ollama's registry, which
  learns the model's name and nothing about the project.
- **Nothing goes to Stratum:** no telemetry, no account, no license check.

## Contributors

A track record is about a person, so whenever records are on:

- **Everyone can see their own record.** Commenting `/stratum record` on any PR
  in the project replies with their merged PRs in the window, the ones later
  reverted or fixed, and the commits behind each. `stratum-open record <login>` in
  the CLI shows the same.
- **A record stays in one project.** It's built from one repo's history and is
  never sent anywhere else.
- **A record changes nothing by default.** It moves triage labels only when the
  policy sets `track_record.affects_triage: true`.

A merged PR counts against its author when its merge commit is reverted, or
when a commit landing within 14 days references it (`#123`) with "fix", "fixes"
or "follow-up". Maintainers can keep a commit out of every record with a
`Stratum-Record: ignore` trailer, for example on a revert made for an unrelated
reason. `track_record.exclude` stops a record being built for a login or email.
