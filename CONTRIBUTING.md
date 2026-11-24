# Contributing

Thanks for helping. Stratum Open is for maintainers who are short on review
time, so it holds itself to what it asks of contributors:

- **Open an issue first** for anything bigger than a small fix, so we agree on
  the change before you spend time on it.
- **Explain the change in the PR:** what it does, why, and how you checked it.
- **Add or update tests.** Run them with `pytest`; they need no network and no
  model. `tests/e2e/` runs the whole pipeline against a fake model server and
  a fake GitHub API (`make e2e`); `make e2e-live` runs it against a real model
  when you have one. A fix for a crash comes with a test that failed before it.
- **Keep the data page true.** If your change stores something new, or sends
  something new off the maintainers' machines, update [DATA.md](DATA.md) in
  the same PR. `tests/test_data_page.py` fails when a stored field isn't listed.
- **Title commits and PRs as `type(scope): message`** ([below](#commits-and-pr-titles)).
- **Sign the CLA** ([CLA.md](CLA.md)) on your first PR ([how](#signing-the-cla)).

AI-assisted contributions are welcome when you've read, run and understood
the code you submit, and you can answer questions about it. Coding agents
follow [AGENTS.md](AGENTS.md); Claude Code reads it through
[CLAUDE.md](CLAUDE.md).

## Setup

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test,app]"
pytest
```

To try a review against a real model, install [Ollama](https://ollama.com), run
`ollama pull qwen2.5-coder:7b`, then `stratum-open review --local main` in any repo.

## Commits and PR titles

Write each commit message and each PR title as

```
type(scope): what the change does, in the imperative
```

- `type` is one of:
  - `feat`: something new a user can do or see
  - `fix`: a bug fix
  - `perf`: the same behavior, faster or with less memory
  - `refactor`: a code change that neither fixes a bug nor adds a feature
  - `test`: tests only
  - `docs`: documentation only
  - `ci`: the workflows in `.github/`
  - `build`: packaging, dependencies, the Dockerfile
  - `chore`: anything else that doesn't change what ships
- `scope` is the part of Stratum the change is in: `review`, `triage`,
  `checks`, `github`, `gitlab`, `app`, `action`, `cli`, `mcp`, `policy`,
  `model`, `storage`, `repo`, `benchmark`, or a document such as `readme`.
  Leave it out, with its parentheses, when a change spans several.
- The message says what the change does, as a command: "add", not "added" or
  "adds". It starts in lowercase, has no full stop, and the whole line stays
  under about 72 characters.
- A change that breaks a policy key, a CLI flag or the public API puts `!`
  before the colon, as in `feat(policy)!: ...`, and the body says what users
  have to change.

For example:

```
fix(triage): forget a 👍 or 👎 that's taken back
feat(checks): add go vet and Clippy checks that build the PR, off by default
refactor(review): send every model call through one wrapper that never raises
ci(stratum): hold the review of outside PRs until a maintainer approves it
docs(readme): add badges now that 0.3.0 is on PyPI
```

PRs are squash-merged, so the PR title becomes the commit on `main`: keep it
in this form if the PR changes along the way. Name your branch
`type/<issue>-<short-description>`, such as `fix/160-taken-back-votes`.

## Signing the CLA

Your first PR can't be merged until you sign the [CLA](CLA.md). The CLA bot
comments on the PR and its check fails until you do. Then:

1. Read [CLA.md](CLA.md).
2. Comment on the PR with exactly this, as the whole comment:
   `I have read the CLA Document and I hereby sign the CLA`
3. Comment `recheck`, so the bot reruns its check and it passes.

One signature covers all your later PRs. The bot checks the author of every
commit, so commit with the name and email linked to your GitHub account.

If a coding agent opened the PR for you, you're still the one who signs: the
comments come from your account, and the agent should post them only after
you've read the CLA and told it to.

## Reporting a security problem

Please don't open a public issue. See [SECURITY.md](SECURITY.md).
