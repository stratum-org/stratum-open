"""PRs written by coding agents (`triage.agent_prs`): the signals, the data
file they come from, what triage does with them, and that with every key
off a review is exactly what it was before."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from fakes import FakeGitHub, FakeModel

from stratum_open import agent_prs
from stratum_open.agent_prs import LIKELY, NO, POSSIBLE, detect, known
from stratum_open.github.api import GitHubError
from stratum_open.platform import CI
from stratum_open.policy import Policy, parse_policy
from stratum_open.runner import Context, review_pull
from stratum_open.triage import AGENT_HUMAN, AGENT_TESTS, PRFacts, triage

ROOT = Path(__file__).resolve().parent.parent
BOT = "stratum[bot]"
DIFF = """diff --git a/app/users.py b/app/users.py
--- a/app/users.py
+++ b/app/users.py
@@ -1,4 +1,5 @@
 import sqlite3

 def get_user(conn, user_id):
-    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
+    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
+    return row
"""


def pull(login="dana", body="Fixes the date parser when the input has no timezone.", branch="fix-dates", **extra):
    return {"number": 7, "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"}, "body": body,
            "head": {"sha": "a" * 40, "ref": branch}, **extra}


def commit(email="dana@example.com", message="Fix the parser", name="Dana"):
    return {"sha": "c" * 40, "commit": {"message": message, "author": {"name": name, "email": email}}}


# ── the signals ──────────────────────────────────────────────────────────


def test_an_agents_account_as_the_author_is_strong():
    result = detect(pull(login="Copilot"), ["app/users.py"], [])
    assert result.verdict == LIKELY and [s.kind for s in result.signals] == ["author"]
    assert result.line() == "**Likely written by a coding agent:** the author is `Copilot` (GitHub Copilot)."
    assert detect(pull(login="DEVIN-AI-INTEGRATION[bot]"), [], []).likely  # logins match without regard to case


def test_a_commit_by_an_agent_or_co_authored_by_one_is_strong():
    by_cursor = commit(email="cursoragent@cursor.com", name="Cursor Agent")
    co_devin = commit(message="Fix the parser\n\nCo-Authored-By: Devin <158243242+devin-ai-integration[bot]@users.noreply.github.com>\n")
    result = detect(pull(), [], [by_cursor, commit(), co_devin])
    assert result.verdict == LIKELY and [s.kind for s in result.signals] == ["commits"]
    assert result.signals[0].text == "2 of 3 commits are by or co-authored by Devin and Cursor"
    assert detect(pull(), [], [by_cursor]).signals[0].text == "its commit is by or co-authored by Cursor"
    assert detect(pull(), [], [by_cursor, by_cursor]).signals[0].text == "all 2 commits are by or co-authored by Cursor"
    # A PR with more commits than the one page read says so.
    assert detect(pull(commits=150), [], [by_cursor] + [commit()] * 99).signals[0].text == (
        "1 of the first 100 commits is by or co-authored by Cursor")


def test_an_agents_footer_in_the_description_is_medium():
    body = "Adds the handoff history.\n\n------\nhttps://chatgpt.com/codex/tasks/task_i_68ff73bda0f4832496f3d1fa9103905f"
    result = detect(pull(body=body), [], [])
    assert result.verdict == LIKELY and [(s.kind, s.strength) for s in result.signals] == [("description", "medium")]
    assert result.signals[0].text == "the description carries OpenAI Codex's footer (`chatgpt.com/codex/tasks/`)"


def test_weak_signals_alone_are_only_possible():
    branch = detect(pull(branch="copilot/fix-the-parser"), ["app/users.py"], [])
    assert branch.verdict == POSSIBLE and [(s.kind, s.strength) for s in branch.signals] == [("branch", "weak")]
    assert branch.line() == ("**Possibly written by a coding agent:** its branch `copilot/fix-the-parser` is named the way GitHub Copilot "
                             "names its own. These are weak signals, so they don't change the triage.")
    files = detect(pull(), ["src/a.py", "AGENTS.md"], [])
    assert files.verdict == POSSIBLE and files.signals[0].text == "it changes a file that steers coding agents (`AGENTS.md`)"
    both = detect(pull(branch="cursor/x"), [".cursor/rules/style.mdc"], [])
    assert both.verdict == POSSIBLE and [s.kind for s in both.signals] == ["branch", "instructions"]


def test_a_persons_pr_stays_no():
    """No signal fires on a PR a person wrote, even one that used an editor's
    completions, names an agent in prose, or has a review bot's summary."""
    body = ("I used Copilot's completions and asked Cursor about the regex. Devin and Codex weren't involved.\n\n"
            "<!-- This is an auto-generated comment: release notes by coderabbit.ai -->\n## Summary by CodeRabbit\n- Fixes the parser")
    commits = [
        commit(message="Fix the parser\n\nCo-authored-by: Copilot <copilot@github.com>"),  # VS Code's git.addAICoAuthor, which counts completions
        commit(message="Tidy\n\nMade-with: Cursor"),
        commit(message="Pair\n\nCo-authored-by: Jane Doe <jane@example.com>\nSigned-off-by: Dana <dana@example.com>"),
        commit(email="49699333+dependabot[bot]@users.noreply.github.com"),
    ]
    result = detect(pull(body=body, branch="fix/copilot-and-cursor-docs"), ["src/dates.py", "tests/test_dates.py", "docs/guide.md"], commits)
    assert (result.verdict, result.signals, result.line()) == (NO, [], "")
    assert detect(pull(login="dependabot[bot]"), [], []).verdict == NO  # a bot, but not a coding agent


def test_commits_that_couldnt_be_read_are_unknown_not_none():
    result = detect(pull(login="Copilot"), [], None)
    assert result.likely and not result.commits_read
    assert detect(pull(), [], None).verdict == NO and detect(pull(), [], []).commits_read


def test_text_a_contributor_chose_stays_inside_its_code_span():
    branch = "copilot/`@mona` hi " + "x" * 100
    [signal] = detect(pull(branch=branch), [], []).signals
    span = signal.text.split("`")[1]
    assert span.startswith("copilot/@mona hi") and len(span) == 80 and span.endswith("…")  # in code, so nobody is pinged
    assert signal.text.count("`") == 2


# ── the data file ────────────────────────────────────────────────────────


def test_every_agent_in_the_data_file_names_a_source_for_what_it_lists():
    agents = known()
    assert len(agents) >= 5
    seen: dict[str, str] = {}
    for agent in agents:
        assert agent.name and agent.sources, agent
        assert agent.accounts or agent.emails or agent.markers or agent.branches, agent.name
        assert all(re.fullmatch(r"https://\S+", source) for source in agent.sources), agent.name
        assert all(re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[a-z]+", email) for email in agent.emails), agent.name
        assert all(marker.strip() and len(marker) >= 10 for marker in agent.markers), agent.name  # specific enough not to turn up by chance
        assert all("/" in glob or glob.endswith("*") for glob in agent.branches), agent.name
        for value in agent.accounts + agent.emails + agent.markers + agent.branches:
            assert seen.setdefault(value.lower(), agent.name) == agent.name, f"{value} is listed for two agents"


def test_the_data_file_ships_in_the_package():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert agent_prs.DATA in config["tool"]["setuptools"]["package-data"]["stratum_open"]
    assert (ROOT / "stratum_open" / agent_prs.DATA).is_file()


def test_another_list_can_be_passed_in():
    ours = agent_prs.parse("agents:\n  - name: Ours\n    accounts: [ours-bot]\n    sources: [https://example.com]\n")
    assert detect(pull(login="ours-bot"), [], [], ours).likely and not detect(pull(login="Copilot"), [], [], ours).signals


# ── the policy and triage ────────────────────────────────────────────────


def test_every_key_is_off_by_default_and_any_turns_the_check_on():
    rules = Policy().triage.agent_prs
    assert (rules.label, rules.needs_maintainer, rules.require_tests, Policy().review.agent_prs_logic_pass) == (False, False, False, False)
    assert Policy().triage.labels.agent_written == "triage: agent-written"
    assert not agent_prs.wanted(Policy())
    for text in ("triage:\n  agent_prs:\n    label: true\n", "triage:\n  agent_prs:\n    needs_maintainer: true\n",
                 "triage:\n  agent_prs:\n    require_tests: true\n", "review:\n  agent_prs_logic_pass: true\n"):
        policy = parse_policy(text)
        assert policy.errors == () and agent_prs.wanted(policy), text
    assert parse_policy("triage:\n  agent_prs:\n    label: yes please\n").errors == ("triage.agent_prs.label must be true or false.",)


def facts(paths=("src/dates.py",)):
    return PRFacts(number=1, title="Fix parsing", body="Fixes the date parser when the input has no timezone; adds tests.",
                   author="Copilot", draft=False, additions=20, deletions=4, paths=list(paths), ci=CI.PASSED, linked_issues=[9])


ROUTE = parse_policy("triage:\n  agent_prs:\n    needs_maintainer: true\n    require_tests: true\n")


def test_a_likely_agent_pr_is_sorted_as_the_policy_asks():
    likely = detect(pull(login="Copilot"), [], [])
    sorted_pr = triage(facts(), [], ROUTE, agent=likely)
    assert sorted_pr.key == "needs_human" and sorted_pr.reasons == [AGENT_HUMAN]
    tests_only = parse_policy("triage:\n  agent_prs:\n    require_tests: true\n")
    assert triage(facts(), [], tests_only, agent=likely).reasons == [AGENT_TESTS]
    assert triage(facts(["src/dates.py", "tests/test_dates.py"]), [], tests_only, agent=likely).key == "ready"
    assert triage(facts(["README.md"]), [], tests_only, agent=likely).key == "ready"  # no code changed


def test_possible_never_changes_triage_and_nor_does_likely_with_the_keys_off():
    possible = detect(pull(branch="copilot/x"), ["AGENTS.md"], [])
    assert possible.verdict == POSSIBLE
    plain = triage(facts(), [], ROUTE)
    assert triage(facts(), [], ROUTE, agent=possible) == plain
    assert triage(facts(), [], Policy(), agent=detect(pull(login="Copilot"), [], [])) == triage(facts(), [], Policy())


# ── a review ─────────────────────────────────────────────────────────────


def model_answer():
    return json.dumps({"summary": "Keeps the row before returning it.", "matches_description": "yes", "mismatch": "", "findings": []})


def agent_pr(**kwargs) -> FakeGitHub:
    """PR 7 as GitHub Copilot opens one."""
    gh = FakeGitHub(diff=DIFF, author="Copilot", body="Keeps the row.\n\n<!-- START COPILOT CODING AGENT SUFFIX -->\n\n- Fixes #3", **kwargs)
    gh.pulls[7]["head"]["ref"] = "copilot/keep-the-row"
    gh.commits[7] = [commit(email="198982749+Copilot@users.noreply.github.com", name="copilot-swe-agent[bot]")]
    return gh


def review(gh, policy_text, model=None):
    ctx = Context(gh=gh, policy=parse_policy(policy_text), model=model or FakeModel(model_answer()), bot=BOT, log=lambda line: None)
    return review_pull(ctx, 7)


LABEL = "triage:\n  enabled: true\n  agent_prs:\n    label: true\n"


def test_the_label_and_the_summary_line_on_a_likely_agent_pr():
    gh = agent_pr()
    outcome = review(gh, LABEL)
    assert gh.commits_asked == [7]
    assert gh.labels[7] == ["triage: agent-written", "triage: ready"] and "triage: agent-written" in gh.created_labels
    body = gh.issue_comments_[7][0]["body"]
    line = ("**Likely written by a coding agent:** the author is `Copilot` (GitHub Copilot); its commit is by or co-authored by "
            "GitHub Copilot; the description carries GitHub Copilot's footer (`START COPILOT CODING AGENT SUFFIX`); its branch "
            "`copilot/keep-the-row` is named the way GitHub Copilot names its own.")
    assert f"\n{line}\n\n**Triage:** `triage: ready`" in body
    assert outcome.triage.key == "ready"  # the label alone doesn't change the triage
    # A renamed label; and the next review doesn't add it twice.
    gh = agent_pr()
    review(gh, LABEL + "  labels:\n    agent_written: written-by-agent\n")
    review(gh, LABEL + "  labels:\n    agent_written: written-by-agent\n")
    assert gh.labels[7].count("written-by-agent") == 1


def test_a_possible_agent_pr_gets_the_line_and_no_label():
    gh = FakeGitHub(diff=DIFF)
    gh.pulls[7]["head"]["ref"] = "codex/keep-the-row"
    review(gh, LABEL)
    assert gh.labels[7] == ["triage: ready"]
    assert "**Possibly written by a coding agent:** its branch `codex/keep-the-row`" in gh.issue_comments_[7][0]["body"]


def test_labels_off_means_the_line_but_no_label():
    gh = agent_pr()
    review(gh, "triage:\n  agent_prs:\n    label: true\n")
    assert gh.labels[7] == [] and gh.created_labels == []
    body = gh.issue_comments_[7][0]["body"]
    assert "**Likely written by a coding agent:**" in body and "labels are off in this project's policy" in body


def test_the_label_comes_off_when_the_pr_no_longer_looks_it_but_not_when_its_commits_are_unknown(monkeypatch):
    gh = FakeGitHub(diff=DIFF, body="Keeps the row; written by hand, as the description says, and tested.")
    gh.add_labels(7, ["triage: agent-written"])

    def refuse(number):
        raise GitHubError(502, "bad gateway")

    with monkeypatch.context() as patch:
        patch.setattr(gh, "pull_commits", refuse)
        review(gh, LABEL)
    assert "triage: agent-written" in gh.labels[7]
    assert "Couldn't read the PR's commits (502), so the coding-agent check went without them." in gh.issue_comments_[7][0]["body"]
    review(gh, LABEL)
    assert "triage: agent-written" not in gh.labels[7]


def test_needs_maintainer_routes_a_likely_agent_pr():
    gh = agent_pr()
    outcome = review(gh, "triage:\n  enabled: true\n  agent_prs:\n    needs_maintainer: true\n")
    assert outcome.triage.key == "needs_human" and AGENT_HUMAN in outcome.triage.reasons
    assert gh.labels[7] == ["triage: needs a maintainer"]  # label stays off: only needs_maintainer is on


def test_the_logic_pass_runs_only_on_agent_prs():
    logic = json.dumps({"decisions": [], "findings": []})
    policy = "review:\n  agent_prs_logic_pass: true\n"
    model = FakeModel(model_answer(), logic)
    review(agent_pr(), policy, model)
    assert ["one kind of defect only" in system for system, _ in model.prompts] == [False, True]
    person = FakeModel(model_answer(), logic)
    gh = FakeGitHub(diff=DIFF)
    review(gh, policy, person)
    assert len(person.prompts) == 1 and gh.commits_asked == [7]
    assert "The logic pass is on for this PR" not in gh.issue_comments_[7][0]["body"]
    gh = agent_pr()
    review(gh, policy, FakeModel(model_answer(), logic))
    assert "The logic pass is on for this PR, since a coding agent likely wrote it (`review.agent_prs_logic_pass`)." in gh.issue_comments_[7][0]["body"]
