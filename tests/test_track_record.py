import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from stratum_open.track_record import author_identity, build_records

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
START = NOW - timedelta(days=100)
MAINTAINER = "maint@example.com"
ALICE = "123+Alice@users.noreply.github.com"
BOB = "bob@example.com"


class Repo:
    def __init__(self, path):
        self.path = str(path)
        empty = path / "empty-gitconfig"
        empty.write_text("")
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", "-b", "main")
        self.commit("initial", day=0)

    def git(self, *args, email=MAINTAINER, day=0):
        stamp = (START + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        env = {
            **self.env,
            "GIT_AUTHOR_NAME": email.split("@")[0], "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": "maint", "GIT_COMMITTER_EMAIL": MAINTAINER, "GIT_COMMITTER_DATE": stamp,
        }
        return subprocess.run(["git", "-C", self.path, *args], env=env, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message, *, email=MAINTAINER, day):
        self.git("commit", "-q", "--allow-empty", "-m", message, email=email, day=day)
        return self.git("rev-parse", "HEAD")

    def squash(self, number, title, *, email, day, body=""):
        return self.commit(f"{title} (#{number})" + (f"\n\n{body}" if body else ""), email=email, day=day)

    def merge(self, number, title, branch, commits, *, day, base="main", body=""):
        """`commits` is a list of (email, message) on a new branch from `base`."""
        self.git("checkout", "-q", "-b", branch, base)
        for offset, (email, message) in enumerate(commits):
            self.commit(message, email=email, day=day - 1 + offset * 0.01)
        self.git("checkout", "-q", "main")
        message = f"Merge pull request #{number} from someone/{branch}\n\n{title}" + (f"\n\n{body}" if body else "")
        self.git("merge", "-q", "--no-ff", branch, "-m", message, day=day)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def records(repo, **kwargs):
    return build_records(repo.path, now=NOW, **kwargs)


def test_author_identity_prefers_the_github_login():
    assert author_identity("123+Alice@users.noreply.github.com") == "alice"
    assert author_identity("alice@users.noreply.github.com") == "alice"
    assert author_identity("49699333+dependabot[bot]@users.noreply.github.com") == "dependabot[bot]"
    assert author_identity(" Bob@Example.com ") == "bob@example.com"
    assert author_identity("Alice") == "alice"


def test_reverted_squash_merge_counts_against_its_author(repo):
    sha = repo.squash(12, "Add login", email=ALICE, day=10)
    repo.squash(13, 'Revert "Add login"', email=BOB, day=12, body=f"This reverts commit {sha}.")

    alice = records(repo)["alice"]
    assert [pr.number for pr in alice.merged] == [12]
    assert [(s.pr.number, s.kind) for s in alice.strikes] == [(12, "revert")]
    assert alice.summary() == "1 merged PR(s), 1 later reverted or fixed"
    assert records(repo)["bob@example.com"].strikes == []


def test_merge_commit_pr_belongs_to_whoever_wrote_it_not_the_merger(repo):
    repo.merge(20, "Add cache", "cache", [(BOB, "cache"), (BOB, "tests"), (MAINTAINER, "nit")], day=10)

    result = records(repo)
    assert [pr.title for pr in result["bob@example.com"].merged] == ["Add cache"]
    assert MAINTAINER not in result


def test_github_revert_pr_is_counted_once(repo):
    merge_sha = repo.merge(20, "Add cache", "cache", [(BOB, "cache")], day=10)
    repo.merge(
        21, 'Revert "Add cache"', "revert-20-cache",
        [(ALICE, f'Revert "Merge pull request #20"\n\nThis reverts commit {merge_sha}, reversing\nchanges made to abc1234.')],
        day=11, body="Reverts someone/repo#20",
    )

    bob = records(repo)["bob@example.com"]
    assert [(s.pr.number, s.kind) for s in bob.strikes] == [(20, "revert")]


def test_follow_up_fix_within_the_window_counts(repo):
    repo.squash(30, "Parse dates", email=ALICE, day=10)
    repo.squash(31, "Fix timezone bug from #30", email=BOB, day=20)

    assert [(s.pr.number, s.kind) for s in records(repo)["alice"].strikes] == [(30, "follow-up")]


@pytest.mark.parametrize(
    "subject, day",
    [
        ("Fix timezone bug from #30", 25),  # 15 days later: outside the 14-day window
        ("Improve docs for #30", 12),  # no fix word
        ("Fix the prefix#30 thing", 12),  # not a reference
        ("Hotfix for #30", 12),  # "hotfix" isn't one of the words
    ],
)
def test_what_isnt_a_follow_up(repo, subject, day):
    repo.squash(30, "Parse dates", email=ALICE, day=10)
    repo.squash(31, subject, email=BOB, day=day)

    assert records(repo)["alice"].strikes == []


def test_a_pr_never_counts_against_itself(repo):
    repo.squash(40, "Fix crash in #40 follow-up", email=ALICE, day=10)

    assert records(repo)["alice"].strikes == []


def test_follow_up_window_is_configurable(repo):
    repo.squash(30, "Parse dates", email=ALICE, day=10)
    repo.squash(31, "Fix timezone bug from #30", email=BOB, day=25)

    assert len(records(repo, followup_days=30)["alice"].strikes) == 1


def test_ignore_trailer_on_the_landing_commit_covers_the_whole_pr(repo):
    sha = repo.squash(12, "Add login", email=ALICE, day=10)
    repo.merge(
        13, 'Revert "Add login"', "revert-12-login", [(BOB, f"Revert\n\nThis reverts commit {sha}.")],
        day=12, body="Unrelated outage, not a bug in #12.\n\nStratum-Record: ignore",
    )

    assert records(repo)["alice"].strikes == []


def test_ignore_trailer_on_one_commit(repo):
    sha = repo.squash(12, "Add login", email=ALICE, day=10)
    repo.squash(13, 'Revert "Add login"', email=BOB, day=12, body=f"This reverts commit {sha}.\n\nStratum-Record: ignore")

    assert records(repo)["alice"].strikes == []


def test_excluded_authors_have_no_record(repo):
    repo.squash(12, "Add login", email=ALICE, day=10)
    repo.squash(13, "Add cache", email=BOB, day=11)

    assert set(records(repo, exclude=["Alice", "bob@example.com"])) == set()
    assert set(records(repo, exclude=["123+alice@users.noreply.github.com"])) == {"bob@example.com"}


def test_prs_outside_the_window_are_left_out(repo):
    sha = repo.squash(12, "Add login", email=ALICE, day=10)
    repo.squash(13, "Add cache", email=BOB, day=90)
    repo.squash(14, 'Revert "Add login"', email=BOB, day=95, body=f"This reverts commit {sha}.")

    result = records(repo, window_days=30)
    assert "alice" not in result
    assert [pr.number for pr in result["bob@example.com"].merged] == [13, 14]


@pytest.mark.parametrize("window_days", [20000, 36500])
def test_a_window_back_past_1973_still_reads_the_history(repo, window_days):
    repo.squash(12, "Add login", email=ALICE, day=10)
    assert [pr.number for pr in records(repo, window_days=window_days)["alice"].merged] == [12]


def test_stacked_pr_is_credited_only_with_its_own_commits(repo):
    repo.merge(50, "Base", "base", [(ALICE, "a1"), (ALICE, "a2")], day=10)
    repo.git("checkout", "-q", "-b", "stacked", "base")
    repo.commit("b1", email=BOB, day=10.5)
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "stacked", "-m", "Merge pull request #51 from someone/stacked\n\nStacked", day=11)

    result = records(repo)
    assert [pr.number for pr in result["alice"].merged] == [50]
    assert [pr.number for pr in result["bob@example.com"].merged] == [51]


def test_rebase_merged_commits_are_not_counted_as_prs(repo):
    repo.commit("Add login", email=ALICE, day=10)

    assert records(repo) == {}


def test_the_cli_reads_the_track_record_settings_from_the_policy_at_ref(repo, monkeypatch, capsys):
    from functools import partial

    from stratum_open import cli

    monkeypatch.setattr(cli, "build_records", partial(build_records, now=NOW))
    policy = os.path.join(repo.path, ".stratum", "policy.yml")
    os.makedirs(os.path.dirname(policy))

    def set_policy(text, day):
        with open(policy, "w", encoding="utf-8") as f:
            f.write(text)
        repo.git("add", ".stratum")
        repo.commit("Set the policy", day=day)

    def record(login):
        assert cli.main(["record", login, "--repo", repo.path]) == 0
        return capsys.readouterr().out

    set_policy("track_record:\n  window_days: 30\n  exclude: [Alice]\n", day=1)
    sha = repo.squash(12, "Add login", email=ALICE, day=50)  # 50 days before NOW: outside the 30-day window
    repo.squash(13, 'Revert "Add login"', email=BOB, day=55, body=f"This reverts commit {sha}.")
    repo.squash(14, "Add cache", email=ALICE, day=90)

    assert record("Alice") == "No record is built for Alice: the project's policy excludes that login.\n"
    assert record(BOB).startswith(f"No merged PRs by {BOB} in the last 30 days of HEAD.")

    set_policy("track_record:\n  window_days: 60\n", day=91)
    assert record(BOB).startswith(f"{BOB}: 1 merged PR(s)")
    assert "#12 Add login: reverted" in record(ALICE)

    set_policy("track_record:\n  enabled: false\n", day=92)
    assert record(BOB) == "Track records are turned off in this project's policy.\n"


def test_record_reports_a_missing_checkout_or_ref(tmp_path):
    import pytest
    from stratum_open.cli import main

    for repo in (tmp_path, tmp_path / "missing"):
        with pytest.raises(SystemExit, match="Couldn't find missing-ref") as error:
            main(["record", "dana", "--repo", str(repo), "--ref", "missing-ref"])
        assert str(repo) in str(error.value)
