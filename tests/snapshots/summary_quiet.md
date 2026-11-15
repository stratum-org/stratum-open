<!-- stratum:summary -->
### Stratum review

**What this PR does:** This PR changes the `review_pull` function in `stratum_open/runner.py` to include a `retry` parameter and adjust how files are read based on previous failures.

**Triage:** `triage: ready`
- In its favour: CI passes. Linked to #51. Changes tests. The change matches its description.

**Found by the checks:** nothing.

**Suggested by the model:** 2 medium. 2 on the changed lines below. The model can be wrong, so check each one before acting on it.

**From the project's history:**
- `stratum_open/diff.py` is imported by 38 files (e.g. `benchmark/bench.py`, `benchmark/dropped_awaits.py`, `benchmark/planted.py`).
- `stratum_open/runner.py` is imported by 22 files (e.g. `benchmark/planted.py`, `stratum_open/action.py`, `stratum_open/app.py`).
- `stratum_open/cli.py` is a hotspot: changed 8 times in the last 180 days, and among the more complex files (83 branches and conditions). Reviewed strictly.
- 12 more under "What was reviewed".

<details><summary>What was reviewed</summary>

- Reviewed only the 2 files changed since aaaaaaa; findings elsewhere were kept.
- The model read 2 files in 1 call.
- The checks ran on all 6 files changed.
- Profile: `small` (runs locally).
- Model: `qwen2.5-coder:7b`, chosen by this project's maintainers.
- Rules: `.stratum/policy.yml` on the default branch.
- Quiet review: comments from the model that name no defect are left out, and one it repeats on 3 or more files is kept once.
- More from the project's history:
  - `stratum_open/github/publish.py` is imported by 13 files (e.g. `stratum_open/action.py`, `stratum_open/github/commands.py`, `stratum_open/github/feedback.py`).
  - `stratum_open/review.py` is imported by 11 files (e.g. `benchmark/bench.py`, `benchmark/verify_replay.py`, `stratum_open/engine.py`).
  - `stratum_open/engine.py` is imported by 8 files (e.g. `benchmark/planted.py`, `stratum_open/__init__.py`, `stratum_open/cli.py`).
  - `stratum_open/github/commands.py` is imported by 5 files (e.g. `stratum_open/action.py`, `stratum_open/app.py`, `tests/e2e/test_b_model_output.py`).
  - `stratum_open/review_large.py` is imported by 5 files (e.g. `stratum_open/engine.py`, `stratum_open/runner.py`, `tests/test_hunk_context.py`).
  - `stratum_open/cli.py` is imported by 3 files (e.g. `stratum_open/__main__.py`, `tests/e2e/test_j_robustness.py`, `tests/test_app_cli.py`).
  - `stratum_open/diff.py` is a hotspot: changed 5 times in the last 180 days, and among the more complex files (143 branches and conditions). Reviewed strictly.
  - `stratum_open/engine.py` is a hotspot: changed 8 times in the last 180 days, and among the more complex files (81 branches and conditions). Reviewed strictly.
  - `stratum_open/github/commands.py` is a hotspot: changed 6 times in the last 180 days, and among the more complex files (106 branches and conditions). Reviewed strictly.
  - `stratum_open/github/publish.py` is a hotspot: changed 7 times in the last 180 days, and among the more complex files (104 branches and conditions). Reviewed strictly.
  - `stratum_open/review.py` is a hotspot: changed 12 times in the last 180 days, and among the more complex files (159 branches and conditions). Reviewed strictly.
  - `stratum_open/runner.py` is a hotspot: changed 14 times in the last 180 days, and among the more complex files (195 branches and conditions). Reviewed strictly.

</details>

<sub>[Stratum Open](https://github.com/stratum-org/stratum-open): review and triage on a model the maintainers run. Comment `/stratum help` for commands.</sub>
