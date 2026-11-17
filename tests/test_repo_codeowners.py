from stratum_open.repo import codeowners
from stratum_open.repo.graph import Importers, importers, related_note

# The examples from GitHub's documentation of CODEOWNERS.
GITHUB_EXAMPLE = """
# These owners will be the default owners for everything in the repo.
*       @global-owner1 @global-owner2
*.js    @js-owner #This is an inline comment.
*.go docs@example.com
/build/logs/ @doctocat
docs/*  docs@example.com
apps/ @octocat
/docs/ @doctocat
/scripts/ @doctocat @octocat
**/logs @octocat
/apps/ @octocat
/apps/github
"""


def owners(path, text=GITHUB_EXAMPLE):
    return codeowners.owners_for(codeowners.parse(text), path)


def test_the_last_matching_line_wins():
    assert owners("README.md") == ("@global-owner1", "@global-owner2")
    assert owners("src/app.js") == ("@js-owner",)  # the inline comment isn't an owner
    assert owners("cmd/main.go") == ("docs@example.com",)


def test_folders_anchors_and_globs_follow_github():
    assert owners("build/logs/today.txt") == ("@octocat",)  # **/logs comes later, so it wins
    assert owners("build/logs/old/today.txt", "/build/logs/ @doctocat\n") == ("@doctocat",)  # and its subfolders
    assert owners("docs/getting-started.md") == ("@doctocat",)  # /docs/ comes after docs/*
    only = "*       @default\ndocs/* @docs\n"
    assert owners("docs/getting-started.md", only) == ("@docs",)
    assert owners("docs/build-app/troubleshooting.md", only) == ("@default",)  # docs/* is only docs' own files
    assert owners("deeply/nested/logs/a.txt") == ("@octocat",)  # **/logs
    assert owners("web/apps/a.ts", "apps/ @octocat\n") == ("@octocat",)  # apps/ anywhere
    assert owners("web/build/logs/a.txt", "/build/logs/ @doctocat\n") == ()  # anchored to the root
    assert owners("tools/apps", "apps/ @octocat\n") == ()  # a trailing slash is a folder, not a file
    assert owners("src/pkg/file.py", "* @default\nsrc/*/ @src\n") == ("@src",)  # every folder in src, and what's in it
    assert owners("src/file.py", "* @default\nsrc/*/ @src\n") == ("@default",)  # but not src's own files


def test_a_pattern_without_owners_takes_ownership_away():
    assert owners("apps/github/index.js") == ()
    assert owners("apps/web/index.js") == ("@octocat",)


def test_a_bare_name_covers_the_folder_of_that_name():
    assert owners("apps/x/y.py", "apps @team\n") == ("@team",)  # legacy matched nothing here


def test_github_reads_the_github_folder_first(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text("* @from-github-folder\n")
    (tmp_path / "CODEOWNERS").write_text("* @from-root\n")
    assert codeowners.read(str(tmp_path)) == "* @from-github-folder\n"
    assert codeowners.read(str(tmp_path / "nowhere")) is None


def test_the_note_names_owners_in_code_style():
    rules = codeowners.parse("* @acme/core\napi/ @acme/api\n/docs/\n")
    [note] = codeowners.owners_note(rules, ["api/users.py", "api/teams.py", "lib/x.py", "docs/a.md"])
    assert note == ("CODEOWNERS: `@acme/api` for `api/teams.py` and 1 more; `@acme/core` for `lib/x.py`. "
                    "No owner is listed for 1 of the changed files.")
    assert codeowners.owners_note(codeowners.parse("/docs/ @d\n"), ["src/x.py"]) == []


def test_each_changed_file_gets_who_imports_it_and_what_it_imports(tmp_path):
    for path, text in {
        "app/__init__.py": "",
        "app/db.py": "",
        "app/auth.py": "from app.db import connect\n",
        "app/routes.py": "from app.auth import login\n",
        "app/admin.py": "from app.auth import login\n",
    }.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text)
    found = importers(str(tmp_path), ["app/auth.py"])["app/auth.py"]
    assert found.imports == ["app/db.py"]
    assert related_note(found) == "Imported by 2 files: `app/admin.py`, `app/routes.py`. It imports `app/db.py`."
    assert related_note(Importers(9, ["a.py"], [])) == "Imported by 9 files: `a.py` and others."
    assert related_note(Importers(0)) == ""


def test_the_runner_passes_owners_and_per_file_notes_and_keeps_a_renamed_hotspot(tmp_path):
    import json

    from fakes import FakeGitHub, FakeModel
    from test_repo_history import Repo, branches

    from stratum_open.policy import parse_policy
    from stratum_open.runner import Context, review_pull

    repo = Repo(tmp_path)
    repo.commit({"CODEOWNERS": "app/ @acme/app\n", "app/__init__.py": "", "app/util.py": "x = 1\n",
                 "app/auth.py": "from app.util import x\n" + branches(3), "app/routes.py": "from app.auth import f\n"}, day=1)
    repo.commit({"app/auth.py": "from app.util import x\n" + branches(6)}, day=2)
    repo.commit({"app/auth.py": "from app.util import x\n" + branches(9)}, day=3)
    diff = ("diff --git a/app/auth.py b/app/session.py\nsimilarity index 90%\nrename from app/auth.py\nrename to app/session.py\n"
            "--- a/app/auth.py\n+++ b/app/session.py\n@@ -1,2 +1,3 @@\n from app.util import x\n+y = x + 1\n def f(x):\n")
    gh = FakeGitHub(diff=diff)
    model = FakeModel(json.dumps({"risk_score": 30, "change_summary": "Adds y.", "findings": []}),
                      json.dumps({"summary": "Renames auth.", "matches_description": "yes", "findings": []}), "{}")
    review_pull(Context(gh=gh, policy=parse_policy("review:\n  profile: large\n  parallel_calls: 1\n  lens_files: 0\n"), model=model,
                        bot="stratum[bot]", log=lambda line: None, checkout=repo.path, history_ref="HEAD"), 7)
    file_prompt, chunk_prompt = model.prompts[0][1], model.prompts[1][0]
    assert "File to review: app/session.py." in file_prompt
    assert "- Imported by 1 file: `app/routes.py`. It imports `app/util.py`." in file_prompt  # under its new name
    assert "review them strictly: app/session.py" in chunk_prompt  # still a hotspot after the rename
    assert "CODEOWNERS: `@acme/app` for `app/session.py`." in gh.issue_comments_[7][0]["body"]

def test_owner_groups_after_the_display_limit_use_correct_number():
    for number, suffix in [(4, "1 more owner group."), (5, "2 more owner groups.")]:
        paths = [f"file{i}.py" for i in range(number)]
        rules = codeowners.parse("\n".join(f"{path} @owner{i}" for i, path in enumerate(paths)))
        assert codeowners.owners_note(rules, paths)[0].endswith(suffix)
