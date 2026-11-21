"""The pre-commit hook (`.pre-commit-hooks.yaml`) through pre-commit itself:
`pre-commit try-repo` on this checkout, from a new repo with changes staged.

Skipped unless STRATUM_TEST_PRE_COMMIT=1 and pre-commit is installed in this
Python (`pip install pre-commit`). Unlike the rest of the suite it needs the
network: pre-commit makes a virtualenv for the hook and pip installs this
checkout into it, with its dependencies from PyPI, which takes a minute or
so. CI runs it in one job (`e2e` on Python 3.12). tests/test_app_cli.py runs
the hook's `entry` without pre-commit.

    STRATUM_TEST_PRE_COMMIT=1 python -m pytest tests/e2e/test_m_pre_commit.py
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.slow, pytest.mark.skipif(
    os.environ.get("STRATUM_TEST_PRE_COMMIT") != "1" or importlib.util.find_spec("pre_commit") is None,
    reason="set STRATUM_TEST_PRE_COMMIT=1 and install pre-commit: the hook is pip installed from PyPI",
)]


def test_try_repo_stops_a_commit_on_a_staged_secret_only(tmp_path):
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1", "PRE_COMMIT_HOME": str(tmp_path / "cache"),
           "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@example.com"}
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)

    def hook():
        # try-repo takes this checkout as it is, uncommitted changes included.
        done = subprocess.run([sys.executable, "-m", "pre_commit", "try-repo", str(ROOT), "stratum-open-checks", "--verbose"],
                              cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        return done.returncode, done.stdout + done.stderr

    git("init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")
    (repo / "app.py").write_text('x = 1\nAPI_KEY = "sk-live-0123456789abcdef0123"\n')
    (repo / "double.py").write_text("def double(n):\n    return n * 2\n")
    git("add", "double.py")
    code, out = hook()
    assert code == 0, out  # the secret isn't staged
    assert "code/secret" not in out
    # The hook's own virtualenv has no Bandit, and says so.
    assert "Bandit didn't run on the staged Python files: not installed here." in out
    git("add", "app.py")
    code, out = hook()
    assert code == 1, out
    assert "app.py:2: critical code/secret Possible secret in the code" in out
