# AGENTS.md

Instructions for coding agents (Claude Code, Codex, Copilot, Cursor and
others) working on Stratum Open, in this repository or a fork of it. People
should read [CONTRIBUTING.md](CONTRIBUTING.md); this file follows it and
adds what an agent needs to get right on its own.

Stratum Open reviews and triages pull requests for open-source maintainers,
on a model they run themselves. It's Python 3.11+, with a CLI
(`stratum-open`), a GitHub Action, a GitHub App and an MCP server.

## Setup and tests

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test,app,bandit,ruff,js,mcp]"
make test   # unit tests: no network, no model
make e2e    # the whole pipeline against a fake model server and a fake GitHub API
```

- Run both before you commit. CI runs them on Python 3.11, 3.12 and 3.13.
- Tests that need a tool you haven't installed (Bandit, Ruff, ShellCheck,
  actionlint, Go, Clippy) skip. Say so in the PR if a test you touched skipped.
- `make e2e-live` needs a real model. Don't run it unless you're asked to.
- A prompt or public-API change fails a snapshot test in `tests/snapshots/`.
  If you meant the change, rerun with `STRATUM_UPDATE_SNAPSHOTS=1` and commit
  the new snapshot, so the diff shows in review. Never update a snapshot to
  make a failure you don't understand go away.

## Where things are

- `stratum_open/review.py`, `review_large.py`: the model's review passes.
- `stratum_open/checks/`: the checks that need no model (secrets, workflows,
  dependencies, Bandit, Ruff, go vet, Clippy and others).
- `stratum_open/triage.py`, `triage_queue.py`: labels and the triage queue.
- `stratum_open/github/`, `gitlab/`, `platform.py`: the forges' APIs.
- `stratum_open/app.py`, `storage.py`: the GitHub App and what it stores.
- `stratum_open/policy.py`: `.stratum/policy.yml`. Every key, with its
  default, is in `policy.example.yml`; a new key goes in both.
- `tests/`: one `test_<module>.py` per module, `tests/e2e/` for the pipeline,
  `tests/fakes.py` for the fake model and GitHub.

## Rules the project holds to

These are in `.stratum/policy.yml`, and Stratum reviews every PR against them:

- Every field the App stores is listed in [DATA.md](DATA.md)'s Stored fields
  section, and nothing leaves the maintainers' infrastructure except what
  DATA.md lists. Storing or sending something new means updating DATA.md in
  the same PR; `tests/test_data_page.py` checks the stored fields.
- No code from a pull request is ever run. Stratum reads PRs through the
  GitHub API only. (go vet and Clippy, which build the PR, are off by default
  and say so.)
- A model's reply is checked before it's used: findings outside the diff are
  dropped. Don't trust a model's output anywhere in new code either.

And when you change code:

- A bug fix comes with a test that fails without it.
- Don't delete, skip or loosen a test to make it pass. Stratum's
  `tests_weakened` check flags it, and a maintainer will ask why.
- Don't add a dependency without an issue that agrees to it. Optional tools
  go in an extra in `pyproject.toml`, not in `dependencies`.
- Match the code around you: `from __future__ import annotations`, type
  hints, and comments that say why, in plain sentences.
- Leave `.github/workflows/`, `action.yml` and agent files (`AGENTS.md`,
  `CLAUDE.md`, `.claude/`, `.cursor/` and the like) alone unless the task is
  about them. Workflows here run with write tokens on other people's PRs.
- Never commit secrets, tokens, `stratum.db` or a model's key.

You can check your own change the way Stratum will before you push:
`stratum-open review --staged --no-model`, or through `stratum-open mcp`.

## Commits, branches and PRs

Commit messages and PR titles take the form in
[CONTRIBUTING.md](CONTRIBUTING.md#commits-and-pr-titles):

```
type(scope): what the change does, in the imperative
```

for example `fix(triage): forget a 👍 or 👎 that's taken back`. PRs are
squash-merged, so the PR title becomes the commit on `main`.

- Branch from `main` as `type/<issue>-<short-description>`, for example
  `fix/160-taken-back-votes`.
- Anything bigger than a small fix needs an issue the maintainers agreed to
  first. Link it in the PR (`Fixes #123`).
- The PR description says what the change does, why, and how you checked it:
  the commands you ran and what they printed. Say that a coding agent wrote
  it, and which one.
- Commit as the person you work for, with the name and email linked to their
  GitHub account. Don't add a bot as the author or a co-author: the CLA bot
  checks every commit's author, and a bot can't sign.

## The CLA, on a first PR

A contributor's first PR can't be merged until they sign the
[CLA](CLA.md). The CLA bot comments on the PR and its check fails. Signing
takes two comments on the PR, each the whole comment and nothing else:

1. `I have read the CLA Document and I hereby sign the CLA`
2. `recheck`, once the first is posted, so the bot reruns its check.

The CLA is a legal agreement between the person who owns the GitHub account
and the project's maintainer. You may post these comments for that person
only when they've read CLA.md and told you to sign it. Otherwise, stop and
ask them to, with a link to CLA.md and the two comments to post.

## Security problems

Don't open a public issue or PR for a vulnerability, and don't describe one
in a commit message. Tell the person you work for, and follow
[SECURITY.md](SECURITY.md).
