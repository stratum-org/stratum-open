from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
import requests

from stratum_open.checks import dependencies, run_checks
from stratum_open.checks.dependencies import Advisories, PackageInfo, PackageLookup, find_new, find_pinned, lookalike, normalise
from stratum_open.diff import parse_diff
from stratum_open.policy import Dependencies, Policy, parse_policy

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def diff(path, added=(), removed=(), context=(), *, start=1, new=False):
    """A one-hunk git diff: the context lines, then the removed, then the added."""
    head = [f"diff --git a/{path} b/{path}"]
    head += ["new file mode 100644", "--- /dev/null"] if new else [f"--- a/{path}"]
    head.append(f"+++ b/{path}")
    head.append(f"@@ -{start},{len(context) + len(removed)} +{start},{len(context) + len(added)} @@")
    body = [f" {line}" for line in context] + [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "\n".join(head + body) + "\n"


def new_names(text, contents=None):
    return [(d.ecosystem, d.name, d.line, d.from_registry) for d in find_new(parse_diff(text), contents)]


def check(text, policy=None, lookup=None, contents=None):
    return dependencies.check(parse_diff(text), policy or Policy(), lookup=lookup, contents=contents, now=NOW)


def rules(findings):
    return [f.rule for f in findings]


# ── requirements files ───────────────────────────────────────────────────


def test_a_new_requirement_is_found():
    assert new_names(diff("requirements.txt", ["httpx>=0.27"], context=["flask==3.0"])) == [("pypi", "httpx", 2, True)]


def test_a_version_bump_is_not_new():
    assert new_names(diff("requirements.txt", ["Flask==3.1"], removed=["flask==3.0"])) == []


def test_options_comments_urls_and_paths_are_not_dependencies():
    lines = [
        "# tools", "-r base.txt", "-e .", "--index-url https://pypi.example/simple", "./vendor/pkg",
        "https://files.example/pkg-1.0.tar.gz", "git+https://github.com/x/y.git#egg=y", "",
        "    --hash=sha256:abcd", "rich[jupyter]>=13 ; python_version >= '3.11'  # pretty output",
    ]
    assert new_names(diff("requirements-dev.txt", lines)) == [("pypi", "rich", 10, True)]


def test_a_requirement_from_a_url_is_new_but_not_from_the_registry():
    assert new_names(diff("requirements.txt", ["mylib @ git+https://github.com/me/mylib.git"])) == [("pypi", "mylib", 1, False)]


@pytest.mark.parametrize("path,kind", [
    ("requirements.txt", "requirements"), ("requirements/dev.in", "requirements"), ("dev-requirements.txt", "requirements"),
    ("requirements_test.txt", "requirements"), ("MANIFEST.in", None), ("docs/notes.txt", None),
    ("web/package.json", "npm"), ("package-lock.json", None), ("pyproject.toml", "pyproject"), ("crates/a/Cargo.toml", "cargo"),
    ("go.mod", "go"), ("go.sum", None),
])
def test_manifests_are_recognised(path, kind):
    assert dependencies.manifest_kind(path) == kind


def test_moving_a_dependency_between_files_is_not_new():
    text = diff("requirements.txt", [], removed=["httpx>=0.27"]) + diff(
        "pyproject.toml", ['    "httpx>=0.27",'], context=["[project]", "dependencies = ["])
    assert new_names(text) == []


def test_pypi_names_are_normalised():
    assert normalise("Typing_Extensions", "pypi") == "typing-extensions"
    assert normalise("zope.interface", "pypi") == "zope-interface"
    assert normalise("serde_json", "crates") == "serde-json"
    assert new_names(diff("requirements.txt", ["typing_extensions>=4"], removed=["Typing-Extensions>=3"])) == []


# ── pyproject.toml ───────────────────────────────────────────────────────

PYPROJECT = """[build-system]
requires = ["setuptools>=68"]

[project]
name = "demo"
keywords = ["review", "pytest"]
classifiers = [
    "License :: OSI Approved :: MIT License",
]
dependencies = [
    "requests>=2.31",
    "click",
]

[project.optional-dependencies]
test = ["pytest>=8", "hypothesis"]

[dependency-groups]
dev = [{include-group = "test"}, "ruff"]

[tool.poetry.dependencies]
python = "^3.11"
rich = "^13"
mylocal = { path = "../mylocal" }
mygit = { git = "https://github.com/me/mygit.git" }

[tool.ruff]
select = ["E", "F"]
"""


def test_pyproject_dependencies_are_found_in_every_section():
    lines = PYPROJECT.splitlines()
    found = new_names(diff("pyproject.toml", lines, new=True))
    assert [(name, registry) for _, name, _, registry in found] == [
        ("setuptools", True), ("requests", True), ("click", True), ("pytest", True), ("hypothesis", True),
        ("ruff", True), ("rich", True), ("mygit", False),
    ]
    assert found[2][2] == lines.index('    "click",') + 1


def test_a_pyproject_line_whose_section_is_hidden_must_look_like_a_requirement():
    text = (
        "diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n+++ b/pyproject.toml\n"
        "@@ -20,3 +20,6 @@\n"
        '     "requests>=2.31",\n'
        '+    "httpx>=0.27",\n'
        '+    "anyio",\n'
        '+    "Programming Language :: Python :: 3",\n'
        "     ]\n"
    )
    assert new_names(text) == [("pypi", "httpx", 21, True)]
    # With the whole file the section is known, and a bare name counts too.
    full = "\n".join(["# pad"] * 17 + ["[project]", "dependencies = [", '    "requests>=2.31",', '    "httpx>=0.27",', '    "anyio",',
                                      '    "Programming Language :: Python :: 3",', "]"])
    assert [n for _, n, _, _ in new_names(text, {"pyproject.toml": full})] == ["httpx", "anyio"]


def test_a_visible_dependencies_array_counts_bare_names_without_its_table():
    text = diff("pyproject.toml", ['    "anyio",'], context=["dependencies = [", '    "httpx>=0.27",'], start=30)
    assert new_names(text) == [("pypi", "anyio", 32, True)]


def test_a_dependency_already_listed_elsewhere_in_the_file_is_not_new():
    text = diff("pyproject.toml", ['test = ["pytest>=8", "requests"]'],
                context=["[project]", "dependencies = [", '    "requests>=2",', "]", "[project.optional-dependencies]"])
    assert [n for _, n, _, _ in new_names(text)] == ["pytest"]


# ── package.json ─────────────────────────────────────────────────────────

PACKAGE_JSON = """{
  "name": "demo",
  "version": "1.0.0",
  "scripts": {
    "test": "jest",
    "lint": "eslint ."
  },
  "engines": {
    "node": ">=18"
  },
  "dependencies": {
    "express": "^4.19.2",
    "lodash4": "npm:lodash@^4.17.21",
    "@acme/utils": "workspace:*",
    "my-fork": "github:me/my-fork#main"
  },
  "devDependencies": {
    "@types/node": "^20.0.0",
    "jest": "29.x"
  }
}"""


def test_package_json_dependencies_are_found_in_their_sections():
    found = new_names(diff("package.json", PACKAGE_JSON.splitlines(), new=True))
    assert [(name, registry) for _, name, _, registry in found] == [
        ("express", True), ("lodash", True), ("my-fork", False), ("@types/node", True), ("jest", True),
    ]


def test_a_package_json_line_whose_section_is_hidden_is_judged_by_its_value():
    text = (
        "diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n"
        "@@ -30,2 +30,7 @@\n"
        '     "react": "^18.3.0",\n'
        '+    "react-dom": "^18.3.0",\n'
        '+    "version": "2.0.0",\n'
        '+    "node": ">=20",\n'
        '+    "build": "tsc -p .",\n'
        '+    "@acme/ui": "workspace:^",\n'
        "   },\n"
    )
    assert new_names(text) == [("npm", "react-dom", 31, True)]


def test_a_package_json_version_bump_is_not_new():
    assert new_names(diff("package.json", ['    "express": "^4.20.0",'], removed=['    "express": "^4.19.2",'])) == []


def test_moving_a_package_to_dev_dependencies_is_not_new():
    text = diff("package.json", ['    "jest": "^29.0.0"'], removed=['    "jest": "^29.0.0",'], context=['  "devDependencies": {'])
    assert new_names(text) == []


# ── Cargo.toml and go.mod ────────────────────────────────────────────────

CARGO = """[package]
name = "demo"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { version = "1", features = ["derive"] }
tokio = "1.38"
json = { package = "serde_json", version = "1" }
local = { path = "../local" }
shared = { workspace = true }
fromgit = { git = "https://github.com/me/fromgit" }

[target.'cfg(windows)'.dependencies]
winreg = "0.52"

[dependencies.regex]
version = "1.10"

[features]
default = ["std"]
"""


def test_cargo_dependencies_are_found():
    found = new_names(diff("Cargo.toml", CARGO.splitlines(), new=True))
    assert [(name, registry) for _, name, _, registry in found] == [
        ("serde", True), ("tokio", True), ("serde_json", True), ("fromgit", False), ("winreg", True), ("regex", True),
    ]


def test_a_cargo_line_whose_section_is_hidden_must_look_like_a_dependency():
    text = (
        "diff --git a/Cargo.toml b/Cargo.toml\n--- a/Cargo.toml\n+++ b/Cargo.toml\n"
        "@@ -12,1 +12,4 @@\n"
        ' anyhow = "1"\n'
        '+thiserror = "1.0"\n'
        '+edition = "2021"\n'
        '+lto = "fat"\n'
    )
    assert new_names(text) == [("crates", "thiserror", 13, True)]


GO_MOD = """module example.com/demo

go 1.22

require github.com/spf13/cobra v1.8.0

require (
	golang.org/x/sync v0.7.0
	github.com/inconshreveable/mousetrap v1.1.0 // indirect
)

replace github.com/old/thing => github.com/new/thing v1.0.0
"""


def test_go_requirements_are_found_and_indirect_ones_left_out():
    found = new_names(diff("go.mod", GO_MOD.splitlines(), new=True))
    assert [(name, line) for _, name, line, _ in found] == [("github.com/spf13/cobra", 5), ("golang.org/x/sync", 8)]


def test_go_lines_whose_block_is_hidden():
    text = diff("go.mod", ["require github.com/spf13/pflag v1.0.5", "\tgithub.com/acme/thing v0.3.0", "\tgithub.com/a/b v1.0.0 // indirect"],
                context=["\tgolang.org/x/sync v0.7.0"], start=12)
    assert [name for _, name, _, _ in new_names(text)] == ["github.com/spf13/pflag", "github.com/acme/thing"]


def test_a_go_version_bump_is_not_new():
    assert new_names(diff("go.mod", ["\tgolang.org/x/sync v0.8.0"], removed=["\tgolang.org/x/sync v0.7.0"], context=["require ("])) == []


# ── findings for new dependencies ────────────────────────────────────────


def test_each_new_dependency_is_a_low_supply_chain_finding():
    (finding,) = check(diff("requirements.txt", ["flask==3.0"], context=["click==8"]))
    assert finding.rule == "deps/new" and finding.severity == "low" and finding.category == "supply-chain"
    assert finding.needs_human is True and finding.line_start == 2 and "`flask`" in finding.detail


# ── look-alike names ─────────────────────────────────────────────────────


@pytest.mark.parametrize("name,ecosystem,similar", [
    ("reqeusts", "pypi", "requests"),
    ("requets", "pypi", "requests"),
    ("tqmd", "pypi", "tqdm"),
    ("python_dateutil2", "pypi", "python-dateutil"),
    ("pythondateutil", "pypi", "python-dateutil"),
    ("requests-oauthlb", "pypi", "requests-oauthlib"),
    ("scikit-lean", "pypi", "scikit-learn"),
    ("djangorestframwork", "pypi", "djangorestframework"),
    ("reactdom", "npm", "react-dom"),
    ("expresss", "npm", "express"),
    ("@bable/core", "npm", "@babel/core"),
    ("tokioo", "crates", "tokio"),
    ("serde-json", "crates", None),  # the same crate: crates.io treats - and _ alike
    ("pytest-env", "pypi", None),  # a different plugin, not a typo of pytest-cov
    ("preact", "npm", None),
    ("sha3", "crates", None),
    ("numba", "pypi", None),
    ("cattrs", "pypi", None),  # a real package beside attrs
    ("requests", "pypi", None),
    ("cobra", "go", None),
])
def test_lookalike_names(name, ecosystem, similar):
    assert lookalike(name, ecosystem) == similar


def test_a_lookalike_is_high_and_needs_a_maintainer():
    findings = check(diff("requirements.txt", ["reqeusts==2.31"]))
    finding = next(f for f in findings if f.rule == "deps/lookalike")
    assert finding.severity == "high" and finding.needs_human and "`reqeusts` looks like `requests`" == finding.title


def test_a_dependency_from_git_gets_no_lookalike_check():
    assert rules(check(diff("requirements.txt", ["reqeusts @ git+https://example.org/r.git"]))) == ["deps/new"]


# ── registry lookups ─────────────────────────────────────────────────────


class FakeLookup:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def info(self, ecosystem, name):
        self.asked.append((ecosystem, name))
        answer = self.answers.get(name)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_package_missing_from_its_registry_is_high():
    lookup = FakeLookup({"fastjsonx": PackageInfo(exists=False)})
    findings = check(diff("requirements.txt", ["fastjsonx==1.0"]), lookup=lookup)
    finding = next(f for f in findings if f.rule == "deps/missing")
    assert finding.severity == "high" and finding.needs_human and "slopsquatting" in finding.detail
    assert "PyPI" in finding.title and lookup.asked == [("pypi", "fastjsonx")]


def test_a_brand_new_package_is_high_and_says_its_age():
    lookup = FakeLookup({"left-padx": PackageInfo(True, NOW - timedelta(days=3))})
    findings = check(diff("package.json", ['    "left-padx": "^1.0.0"'], context=["{", '  "dependencies": {']), lookup=lookup)
    finding = next(f for f in findings if f.rule == "deps/young")
    assert finding.severity == "high" and finding.needs_human and "3 days ago" in finding.detail
    assert lookup.asked == [("npm", "left-padx")]


def test_an_established_package_gets_no_registry_finding():
    lookup = FakeLookup({"flask": PackageInfo(True, NOW - timedelta(days=5000))})
    assert rules(check(diff("requirements.txt", ["flask==3.0"]), lookup=lookup)) == ["deps/new"]


def test_the_age_threshold_comes_from_the_policy():
    lookup = FakeLookup({"newish": PackageInfo(True, NOW - timedelta(days=45))})
    strict = replace(Policy(), dependencies=Dependencies(min_package_age_days=60))
    assert "deps/young" not in rules(check(diff("requirements.txt", ["newish==1"]), lookup=lookup))
    assert "deps/young" in rules(check(diff("requirements.txt", ["newish==1"]), strict, lookup=lookup))


@pytest.mark.parametrize("answer", [None, requests.ConnectionError("offline"), RuntimeError("bug in a lookup")])
def test_a_lookup_that_cant_answer_gives_no_finding(answer):
    lookup = FakeLookup({"flask": answer})
    assert rules(check(diff("requirements.txt", ["flask==3.0"]), lookup=lookup)) == ["deps/new"]


def test_go_modules_and_git_dependencies_are_not_looked_up():
    lookup = FakeLookup({})
    text = diff("go.mod", ["require github.com/spf13/cobra v1.8.0"]) + diff("requirements.txt", ["x @ git+https://e.org/x.git"])
    check(text, lookup=lookup)
    assert lookup.asked == []


def test_lookups_stop_at_the_cap(monkeypatch):
    monkeypatch.setattr(dependencies, "MAX_LOOKUPS", 2)
    lookup = FakeLookup({})
    check(diff("requirements.txt", ["aaa-one==1", "bbb-two==1", "ccc-three==1"]), lookup=lookup)
    assert [name for _, name in lookup.asked] == ["aaa-one", "bbb-two"]


# ── PackageLookup against fake registries ────────────────────────────────


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakeSession:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append((url, timeout, headers))
        reply = self.replies[url]
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_pypi_reports_the_earliest_upload():
    url = "https://pypi.org/pypi/my-pkg/json"
    releases = {
        "1.0": [{"upload_time_iso_8601": "2024-03-01T10:00:00.000000Z"}],
        "0.1": [{"upload_time": "2023-01-15T08:30:00"}, {"upload_time_iso_8601": "2023-01-15T08:31:00Z"}],
        "0.0.1": [],
    }
    session = FakeSession({url: FakeResponse(200, {"releases": releases})})
    info = PackageLookup(session).info("pypi", "My_Pkg")
    assert info == PackageInfo(True, datetime(2023, 1, 15, 8, 30, tzinfo=timezone.utc))
    assert session.calls[0][1] == 10.0 and "stratum-open" in session.calls[0][2]["User-Agent"]


def test_npm_scoped_names_are_encoded_and_created_is_read():
    url = "https://registry.npmjs.org/@acme%2Fwidget"
    session = FakeSession({url: FakeResponse(200, {"time": {"created": "2026-10-30T12:00:00.000Z"}})})
    info = PackageLookup(session).info("npm", "@acme/widget")
    assert info.exists and info.first_published == datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def test_crates_io_gets_a_user_agent_and_created_at_is_read():
    url = "https://crates.io/api/v1/crates/serde"
    session = FakeSession({url: FakeResponse(200, {"crate": {"created_at": "2014-12-05T20:20:39.487502+00:00"}})})
    info = PackageLookup(session).info("crates", "serde")
    assert info.first_published.year == 2014
    assert session.calls[0][2]["User-Agent"].startswith("stratum-open/")


def test_a_404_means_missing():
    session = FakeSession({"https://pypi.org/pypi/nope-nope/json": FakeResponse(404)})
    assert PackageLookup(session).info("pypi", "nope-nope") == PackageInfo(exists=False)


@pytest.mark.parametrize("reply", [
    FakeResponse(500), FakeResponse(429), FakeResponse(200, ValueError("not json")), FakeResponse(200, ["odd"]),
    requests.Timeout("slow"), requests.ConnectionError("offline"),
])
def test_anything_else_is_unknown(reply):
    session = FakeSession({"https://pypi.org/pypi/flask/json": reply})
    assert PackageLookup(session).info("pypi", "flask") is None


def test_answers_are_cached_for_the_run():
    session = FakeSession({"https://pypi.org/pypi/flask/json": FakeResponse(200, {"releases": {}})})
    lookup = PackageLookup(session)
    assert lookup.info("pypi", "flask") == lookup.info("pypi", "Flask") == PackageInfo(True, None)
    assert len(session.calls) == 1


def test_go_modules_have_no_lookup():
    session = FakeSession({})
    assert PackageLookup(session).info("go", "github.com/spf13/cobra") is None and session.calls == []


# ── exact versions ───────────────────────────────────────────────────────


def pins(text, contents=None):
    return [(d.ecosystem, d.name, version, d.line) for d, version in find_pinned(parse_diff(text), contents)]


def test_only_an_exact_requirement_is_a_pin():
    lines = [
        "requests==2.19.1", "Flask[async] == 3.0.0 ; python_version >= '3.9'  # web", "urllib3==1.26.5 \\", "    --hash=sha256:abcd",
        "idna==3.7 --hash=sha256:abcd", "django>=2.2", "numpy~=1.26.0", "click==8.*", "attrs==23.1.0,<24", "six===1.16.0", "pytz", "tzdata==banana",
        "mylib @ git+https://github.com/me/mylib.git@v1.0.0",
    ]
    assert pins(diff("requirements.txt", lines)) == [
        ("pypi", "requests", "2.19.1", 1), ("pypi", "Flask", "3.0.0", 2), ("pypi", "urllib3", "1.26.5", 3), ("pypi", "idna", "3.7", 5),
    ]


def test_a_bump_is_a_pin_and_an_unchanged_line_is_not():
    text = diff("requirements.txt", ["requests==2.19.1"], removed=["requests==2.18.0"], context=["flask==0.12"])
    assert new_names(text) == []
    assert pins(text) == [("pypi", "requests", "2.19.1", 2)]


def test_pins_in_pyproject_are_pep_508_pins_and_bare_poetry_versions():
    lines = [
        "[project]", 'dependencies = ["requests==2.19.1", "click>=8"]', "[project.optional-dependencies]", "test = [", '    "pytest==8.0.0",', "]",
        "[tool.poetry.dependencies]", 'python = "^3.11"', 'rich = "13.0.0"', 'jinja2 = "==2.11.2"', 'httpx = "^0.27.0"',
        'pyyaml = { version = "5.3", optional = true }', 'mylocal = { path = "../mylocal" }',
    ]
    assert [(name, version) for _, name, version, _ in pins(diff("pyproject.toml", lines, new=True))] == [
        ("requests", "2.19.1"), ("pytest", "8.0.0"), ("rich", "13.0.0"), ("jinja2", "2.11.2"), ("pyyaml", "5.3"),
    ]


def test_pins_in_package_json_are_whole_versions_without_a_range():
    lines = [
        "{", '  "dependencies": {', '    "lodash": "4.17.15",', '    "minimist": "=1.2.0",', '    "lodash4": "npm:lodash@4.17.20",',
        '    "express": "^4.19.2",', '    "left-pad": "~1.3.0",', '    "react": "18.x",', '    "chalk": "4.1",', '    "debug": "latest",',
        '    "@acme/utils": "workspace:*",', '    "ms": "2.1.3 || 3.0.0"', "  }", "}",
    ]
    assert [(name, version) for _, name, version, _ in pins(diff("package.json", lines, new=True))] == [
        ("lodash", "4.17.15"), ("minimist", "1.2.0"), ("lodash", "4.17.20"),
    ]


def test_a_bare_cargo_version_is_a_range_and_a_go_requirement_is_a_pin():
    cargo = ["[dependencies]", 'smallvec = "=1.6.0"', 'time = { version = "= 0.1.43", features = ["std"] }', 'tokio = "1.38.0"', 'serde = "=1"']
    assert [(name, version) for _, name, version, _ in pins(diff("Cargo.toml", cargo, new=True))] == [("smallvec", "1.6.0"), ("time", "0.1.43")]
    go = ["require (", "\tgolang.org/x/crypto v0.1.0", "\tgithub.com/dgrijalva/jwt-go v3.2.0+incompatible", "\tgithub.com/a/b v1.0.0 // indirect", ")"]
    assert [(e, name, version) for e, name, version, _ in pins(diff("go.mod", go, new=True))] == [
        ("go", "golang.org/x/crypto", "0.1.0"), ("go", "github.com/dgrijalva/jwt-go", "3.2.0+incompatible"),
    ]


@pytest.mark.parametrize("ecosystem,ordered", [
    ("pypi", ["0.9", "1.0.dev1", "1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.post1", "1.0.1", "1.1", "1.10", "2.0", "1!0.1"]),
    ("npm", ["0.9.9", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.2.0", "1.10.0", "2.0.0"]),
])
def test_versions_sort_as_their_ecosystem_sorts_them(ecosystem, ordered):
    keys = [dependencies.version_key(version, ecosystem) for version in ordered]
    assert None not in keys and keys == sorted(keys) and len(set(keys)) == len(keys)


def test_text_that_is_not_a_whole_version_has_no_key():
    assert dependencies.version_key("2.0", "pypi") == dependencies.version_key("2.0.0", "pypi") == dependencies.version_key("v2", "pypi")
    assert dependencies.version_key("1.0.0+build.5", "npm") == dependencies.version_key("v1.0.0", "go")
    for ecosystem, text in [("pypi", "banana"), ("pypi", ""), ("pypi", "2.*"), ("pypi", ">=2"), ("npm", "4.17"), ("npm", "banana"), ("npm", "^1.0.0"), ("go", "")]:
        assert dependencies.version_key(text, ecosystem) is None, (ecosystem, text)


# ── findings for known vulnerabilities ───────────────────────────────────

VULNERABLE = replace(Policy(), dependencies=Dependencies(known_vulnerabilities=True))
GHSA = ("GHSA-x84v-xcm2-53pg", "GHSA-9hjg-9r4m-mvj7", "GHSA-9wx4-h78v-vm56", "GHSA-gc5v-m9x4-r6x2", "GHSA-j8r2-6x86-q33q")


class FakeAdvisories(FakeLookup):
    """A lookup that also answers for pinned versions, from a table."""

    def __init__(self, listed, answers=None):
        super().__init__(answers or {})
        self.listed = listed
        self.pins = []

    def advisories(self, pins):
        self.pins.append(list(pins))
        if isinstance(self.listed, Exception):
            raise self.listed
        return self.listed


def test_a_pin_with_advisories_is_a_finding_that_names_them_and_the_fix():
    lookup = FakeAdvisories({("pypi", "requests", "2.19.1"): Advisories(GHSA, "high", "2.33.0")})
    text = diff("requirements.txt", ["requests==2.19.1"], removed=["requests==2.18.0"], context=["flask==3.0.0"])
    (finding,) = check(text, VULNERABLE, lookup=lookup)
    assert finding.rule == "deps/vulnerable" and finding.severity == "high" and finding.category == "supply-chain"
    assert (finding.path, finding.line_start, finding.needs_human) == ("requirements.txt", 2, False)
    assert finding.title == "`requests` 2.19.1 has known vulnerabilities"
    assert "lists 5 security advisories for that version: GHSA-x84v-xcm2-53pg, GHSA-9hjg-9r4m-mvj7, GHSA-9wx4-h78v-vm56 and 2 more." in finding.detail
    assert "The most severe is rated high. 2.33.0 is the first version with all of them fixed." in finding.detail
    assert finding.suggestion == "Use 2.33.0, or a later release that has the fixes."
    assert lookup.pins == [[("pypi", "requests", "2.19.1")]], "only the added pin is asked about"


def test_one_advisory_with_no_severity_and_no_fix_is_medium_and_says_so():
    lookup = FakeAdvisories({("npm", "flatmap-stream", "0.1.1"): Advisories(("GHSA-mh6f-8j2x-4483",))})
    findings = check(diff("package.json", ['    "flatmap-stream": "0.1.1"'], context=["{", '  "dependencies": {']), VULNERABLE, lookup=lookup)
    finding = next(f for f in findings if f.rule == "deps/vulnerable")
    assert finding.severity == dependencies.UNRATED == "medium" and finding.title == "`flatmap-stream` 0.1.1 has a known vulnerability"
    assert "lists a security advisory for that version: GHSA-mh6f-8j2x-4483. OSV gives it no severity. OSV names no version with it fixed." in finding.detail
    assert "Check whether it applies" in finding.suggestion


@pytest.mark.parametrize("label", ["low", "medium", "high", "critical"])
def test_the_severity_is_the_one_osv_gives(label):
    lookup = FakeAdvisories({("pypi", "pyyaml", "5.3"): Advisories(("GHSA-6757-jp84-gxfx",), label, "5.4")})
    (finding,) = [f for f in check(diff("requirements.txt", ["pyyaml==5.3"]), VULNERABLE, lookup=lookup) if f.rule == "deps/vulnerable"]
    assert finding.severity == label and f"It is rated {label}. 5.4 is the first version with it fixed." in finding.detail


def test_a_range_a_clean_pin_and_a_git_dependency_get_no_finding():
    lookup = FakeAdvisories({("pypi", "requests", "2.19.1"): Advisories(GHSA, "high", "2.33.0")})
    text = diff("requirements.txt", ["requests>=2.19.1", "urllib3==2.5.0", "requests @ git+https://github.com/psf/requests@v2.19.1"])
    assert "deps/vulnerable" not in rules(check(text, VULNERABLE, lookup=lookup))
    assert lookup.pins == [[("pypi", "urllib3", "2.5.0")]]


def test_known_vulnerabilities_are_off_unless_the_policy_turns_them_on():
    lookup = FakeAdvisories({("pypi", "requests", "2.19.1"): Advisories(GHSA, "high", "2.33.0")})
    text = diff("requirements.txt", ["requests==2.19.1"])
    assert rules(check(text, lookup=lookup)) == ["deps/new"] and lookup.pins == []
    assert rules(check(text, VULNERABLE)) == ["deps/new"], "no lookup, no question"
    assert parse_policy("").dependencies.known_vulnerabilities is False
    assert parse_policy("dependencies:\n  known_vulnerabilities: true\n").dependencies.known_vulnerabilities is True
    assert parse_policy("dependencies:\n  known_vulnerabilities: 1\n").errors == ("dependencies.known_vulnerabilities must be true or false.",)


def test_registry_lookup_off_means_osv_is_not_asked_either():
    lookup = FakeAdvisories({("pypi", "requests", "2.19.1"): Advisories(GHSA, "high", "2.33.0")})
    files = parse_diff(diff("requirements.txt", ["requests==2.19.1"]))
    off = replace(Policy(), dependencies=Dependencies(registry_lookup=False, known_vulnerabilities=True))
    assert rules(run_checks(files, off, lookup=lookup)) == ["deps/new"] and lookup.pins == []
    assert "deps/vulnerable" not in rules(dependencies.check(files, off, lookup=lookup)) and lookup.pins == [], "nor by the check called on its own"
    assert rules(run_checks(files, VULNERABLE, lookup=lookup)) == ["deps/new", "deps/vulnerable"]


@pytest.mark.parametrize("listed", [
    {}, None, ["odd"], {("pypi", "requests", "2.19.1"): "odd"}, {("pypi", "requests", "2.19.1"): Advisories(())},
    requests.ConnectionError("offline"), RuntimeError("bug in a lookup"),
])
def test_an_osv_answer_that_cant_be_used_gives_no_finding(listed):
    lookup = FakeAdvisories(listed)
    assert rules(check(diff("requirements.txt", ["requests==2.19.1"]), VULNERABLE, lookup=lookup)) == ["deps/new"]


def test_a_lookup_from_before_this_check_still_works():
    lookup = FakeLookup({"requests": PackageInfo(True, NOW - timedelta(days=5000))})  # it has `info` and nothing else
    assert rules(check(diff("requirements.txt", ["requests==2.19.1"]), VULNERABLE, lookup=lookup)) == ["deps/new"]

    class Older(PackageLookup):  # a subclass with its own constructor and its own way to fetch
        def __init__(self):
            self.asked = []

        def info(self, ecosystem, name):
            self.asked.append(name)
            return None

    older = Older()
    assert older.advisories([("pypi", "requests", "2.19.1")]) == {}
    assert rules(check(diff("requirements.txt", ["requests==2.19.1"]), VULNERABLE, lookup=older)) == ["deps/new"] and older.asked == ["requests"]


def test_the_same_pin_is_asked_once_and_pins_stop_at_the_cap(monkeypatch):
    lookup = FakeAdvisories({("pypi", "aaa", "1.0"): Advisories(("GHSA-aaaa-aaaa-aaaa",), "low", "1.1")})
    text = diff("requirements.txt", ["aaa==1.0", "bbb==1.0", "ccc==1.0"]) + diff("requirements-dev.txt", ["aaa==1.0"])
    monkeypatch.setattr(dependencies, "MAX_PINS", 2)
    found = [f for f in check(text, VULNERABLE, lookup=lookup) if f.rule == "deps/vulnerable"]
    assert lookup.pins == [[("pypi", "aaa", "1.0"), ("pypi", "bbb", "1.0")]]
    assert [(f.path, f.line_start) for f in found] == [("requirements.txt", 1), ("requirements-dev.txt", 1)]


# ── PackageLookup against a fake OSV ─────────────────────────────────────

BATCH, QUERY = "https://api.osv.dev/v1/querybatch", "https://api.osv.dev/v1/query"


class FakeOSV:
    """A session that answers OSV's two endpoints: each URL has a list of
    replies, handed out in order (the last one repeats)."""

    def __init__(self, batch=(), query=()):
        self.replies = {BATCH: list(batch), QUERY: list(query)}
        self.calls = []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append((url, json, timeout, headers))
        replies = self.replies[url]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


def record(id, *, aliases=(), label=None, spans=(), name="requests", ecosystem="PyPI", kind="ECOSYSTEM", **extra):
    """An OSV record: `spans` are (introduced, fixed) pairs, each its own range, and `label` is the database's severity."""
    ranges = [{"type": kind, "events": [{"introduced": start}] + ([{"fixed": end}] if end else [])} for start, end in spans]
    return {
        "id": id, "aliases": list(aliases), "affected": [{"package": {"name": name, "ecosystem": ecosystem}, "ranges": ranges}],
        **({"database_specific": {"severity": label}} if label else {}), **extra,
    }


def listed(*ids):
    return {"vulns": [{"id": i, "modified": "2026-01-01T00:00:00Z"} for i in ids]}


REQUESTS_2_19_1 = [
    record("GHSA-x84v-xcm2-53pg", aliases=["CVE-2018-18074", "PYSEC-2018-28"], label="HIGH", spans=[("0", "2.20.0")]),
    record("PYSEC-2018-28", aliases=["CVE-2018-18074", "GHSA-x84v-xcm2-53pg"], spans=[("0", "2.20.0")]),
    record("GHSA-j8r2-6x86-q33q", aliases=["CVE-2023-32681"], label="MODERATE", spans=[("2.3.0", "2.31.0")]),
    record("PYSEC-2023-74", aliases=["CVE-2023-32681"], spans=[("2.3.0", "2.31.0")]),
]


def test_osv_is_sent_the_name_ecosystem_and_version_and_nothing_else():
    session = FakeOSV(batch=[FakeResponse(200, {"results": [listed("GHSA-x84v-xcm2-53pg"), {}, {}]})], query=[FakeResponse(200, {"vulns": REQUESTS_2_19_1})])
    found = PackageLookup(session).advisories([("pypi", "Requests", "2.19.1"), ("npm", "@babel/traverse", "7.23.2"), ("go", "golang.org/x/sync", "0.7.0")])
    assert found == {("pypi", "Requests", "2.19.1"): Advisories(("GHSA-x84v-xcm2-53pg", "GHSA-j8r2-6x86-q33q"), "high", "2.31.0")}
    asked = {"package": {"name": "requests", "ecosystem": "PyPI"}, "version": "2.19.1"}
    assert [(url, body) for url, body, _, _ in session.calls] == [
        (BATCH, {"queries": [
            asked,
            {"package": {"name": "@babel/traverse", "ecosystem": "npm"}, "version": "7.23.2"},
            {"package": {"name": "golang.org/x/sync", "ecosystem": "Go"}, "version": "0.7.0"},
        ]}),
        (QUERY, asked),  # the records are read only for the version the batch listed something for
    ]
    assert all(timeout == 10.0 and headers["User-Agent"].startswith("stratum-open/") for _, _, timeout, headers in session.calls)


def test_no_advisories_is_one_request_and_an_empty_answer():
    session = FakeOSV(batch=[FakeResponse(200, {"results": [{}, {}]})])
    assert PackageLookup(session).advisories([("pypi", "flask", "3.1.0"), ("npm", "react", "19.0.0"), ("pypi", "flask", "3.1.0")]) == {}
    assert len(session.calls) == 1
    assert PackageLookup(session).advisories([]) == {} and PackageLookup(session).advisories([("gems", "rails", "7.0.0")]) == {}
    # OSV answers for a version it can't read as if every version were meant, so one is never sent.
    assert PackageLookup(session).advisories([("npm", "lodash", "banana"), ("pypi", "requests", ""), ("npm", "lodash", "4.17")]) == {}
    assert len(session.calls) == 1, "nothing to ask, nothing sent"


def test_aliases_count_once_and_the_most_severe_comes_first():
    records = [
        record("PYSEC-2020-1", aliases=["CVE-2020-1"], spans=[("0", "1.5")]),
        record("GHSA-aaaa-aaaa-aaaa", aliases=["CVE-2020-1"], label="LOW", spans=[("0", "1.5")]),
        record("GHSA-bbbb-bbbb-bbbb", label="CRITICAL", spans=[("0", "1.2")]),
        record("PYSEC-2021-7", aliases=["CVE-2021-7"], spans=[("0", "1.1")]),  # no GHSA record: the CVE names it
        record("PYSEC-2022-9", spans=[("0", "1.3")]),
        record("GHSA-gone-gone-gone", label="CRITICAL", spans=[("0", "9.0")], withdrawn="2024-01-01T00:00:00Z"),
    ]
    session = FakeOSV(batch=[FakeResponse(200, {"results": [listed("PYSEC-2020-1")]})], query=[FakeResponse(200, {"vulns": records})])
    assert PackageLookup(session).advisories([("pypi", "requests", "1.0")]) == {
        ("pypi", "requests", "1.0"): Advisories(("GHSA-bbbb-bbbb-bbbb", "GHSA-aaaa-aaaa-aaaa", "CVE-2021-7", "PYSEC-2022-9"), "critical", "1.5"),
    }


def test_no_severity_label_means_no_severity():
    records = [record("GO-2025-4116", aliases=["CVE-2025-47913"], spans=[("0", "0.43.0")], name="golang.org/x/crypto", ecosystem="Go", kind="SEMVER",
                      severity=[{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"}])]
    session = FakeOSV(batch=[FakeResponse(200, {"results": [listed("GO-2025-4116")]})], query=[FakeResponse(200, {"vulns": records})])
    pin = ("go", "golang.org/x/crypto", "0.1.0")
    assert PackageLookup(session).advisories([pin]) == {pin: Advisories(("CVE-2025-47913",), None, "0.43.0")}


def test_the_fixed_version_is_clear_of_every_advisory():
    # 4.2.8 fixes the second advisory but is still inside the first one's 4.2 range, so the answer moves on to 4.2.11.
    records = [
        record("GHSA-aaaa-aaaa-aaaa", label="HIGH", spans=[("5.0", "5.0.3"), ("3.2", "3.2.25"), ("4.2", "4.2.11")], name="Django"),
        record("GHSA-bbbb-bbbb-bbbb", label="LOW", spans=[("0", "4.2.8")], name="django"),
    ]
    session = FakeOSV(batch=[FakeResponse(200, {"results": [listed("GHSA-aaaa-aaaa-aaaa")]})], query=[FakeResponse(200, {"vulns": records})])
    assert PackageLookup(session).advisories([("pypi", "django", "3.2.20")])[("pypi", "django", "3.2.20")].fixed == "4.2.11"


@pytest.mark.parametrize("records", [
    [record("GHSA-aaaa-aaaa-aaaa", spans=[("0", None)])],  # every version is affected
    [record("GHSA-aaaa-aaaa-aaaa", spans=[("0", "2.0")]), record("GHSA-bbbb-bbbb-bbbb", spans=[("0", None)])],  # one of two has no fix
    [record("GHSA-aaaa-aaaa-aaaa", spans=[("0", "2.0")], name="another-package")],  # the range is for a different package
    [record("GHSA-aaaa-aaaa-aaaa", spans=[("0", "2.0")], ecosystem="npm")],
    [record("GHSA-aaaa-aaaa-aaaa", spans=[("0", "c45d7c49ea75133e52ab22a8e9e13173938e36ff")], kind="GIT")],  # commits, not versions
    [record("GHSA-aaaa-aaaa-aaaa", spans=[("0", "the next release")])],
    [record("GHSA-aaaa-aaaa-aaaa", spans=[("1.5", "2.0")])],  # listed for 1.0, but by no range around it
    [{"id": "GHSA-aaaa-aaaa-aaaa", "affected": "odd"}],
    [{"id": "GHSA-aaaa-aaaa-aaaa", "affected": [{"package": {"name": "requests", "ecosystem": "PyPI"}, "ranges": [
        {"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"last_affected": "1.4"}]}]}]}],
])
def test_no_fixed_version_is_named_unless_osv_states_one_for_every_advisory(records):
    session = FakeOSV(batch=[FakeResponse(200, {"results": [listed("GHSA-aaaa-aaaa-aaaa")]})], query=[FakeResponse(200, {"vulns": records})])
    (found,) = PackageLookup(session).advisories([("pypi", "requests", "1.0")]).values()
    assert found.fixed is None and found.ids[0] == "GHSA-aaaa-aaaa-aaaa"


@pytest.mark.parametrize("reply", [
    FakeResponse(200, ValueError("not json")), FakeResponse(200, ["odd"]), FakeResponse(200, {"results": "odd"}), FakeResponse(200, {"results": []}),
    FakeResponse(200, {"results": [{}, {}]}), FakeResponse(200, {}), FakeResponse(400, {"code": 3, "message": "invalid ecosystem"}), FakeResponse(404),
])
def test_an_odd_answer_from_osv_is_unknown_and_is_not_asked_again(reply):
    session = FakeOSV(batch=[reply])
    assert PackageLookup(session).advisories([("pypi", "requests", "2.19.1")]) == {}
    assert len(session.calls) == 1


@pytest.mark.parametrize("failure", [requests.Timeout("slow"), requests.ConnectionError("offline"), FakeResponse(500), FakeResponse(429)])
def test_osv_out_of_reach_is_tried_once_more_and_then_left(failure):
    session = FakeOSV(batch=[failure])
    assert PackageLookup(session).advisories([("pypi", "requests", "2.19.1")]) == {}
    assert [url for url, _, _, _ in session.calls] == [BATCH, BATCH]

    answers = FakeResponse(200, {"vulns": REQUESTS_2_19_1})
    session = FakeOSV(batch=[failure, FakeResponse(200, {"results": [listed("GHSA-x84v-xcm2-53pg")]})], query=[answers])
    assert len(PackageLookup(session).advisories([("pypi", "requests", "2.19.1")])) == 1, "the second try answered"


def test_one_retry_covers_the_whole_call():
    hits = {"results": [listed("GHSA-x84v-xcm2-53pg")] * 3}
    session = FakeOSV(batch=[requests.Timeout("slow"), FakeResponse(200, hits)], query=[FakeResponse(200, {"vulns": REQUESTS_2_19_1}), requests.Timeout("slow")])
    found = PackageLookup(session).advisories([("pypi", "requests", version) for version in ("2.19.1", "2.19.0", "2.18.4")])
    assert list(found) == [("pypi", "requests", "2.19.1")]
    assert [url for url, _, _, _ in session.calls] == [BATCH, BATCH, QUERY, QUERY], "the retry was spent, so the first failure after it ends the asking"


def test_records_are_read_for_a_capped_number_of_versions(monkeypatch):
    monkeypatch.setattr(dependencies, "MAX_VULNERABLE", 2)
    versions = ["2.19.1", "2.19.0", "2.18.4"]
    session = FakeOSV(batch=[FakeResponse(200, {"results": [listed("GHSA-x84v-xcm2-53pg")] * 3})], query=[FakeResponse(200, {"vulns": REQUESTS_2_19_1})])
    found = PackageLookup(session).advisories([("pypi", "requests", version) for version in versions])
    assert [version for _, _, version in found] == versions[:2] and len(session.calls) == 3


@pytest.mark.parametrize("reply", [
    FakeResponse(200, {"vulns": REQUESTS_2_19_1, "next_page_token": "more"}), FakeResponse(200, {"vulns": "odd"}), FakeResponse(200, {}),
    FakeResponse(200, {"vulns": [{"modified": "no id"}, "odd"]}), FakeResponse(400),
])
def test_records_that_cant_be_read_in_full_give_no_answer(reply):
    session = FakeOSV(batch=[FakeResponse(200, {"results": [listed("GHSA-x84v-xcm2-53pg")]})], query=[reply])
    assert PackageLookup(session).advisories([("pypi", "requests", "2.19.1")]) == {}


def test_a_session_that_can_only_get_gives_no_advisories():
    session = FakeSession({})  # built for the registries: it has no `post`
    assert PackageLookup(session).advisories([("pypi", "requests", "2.19.1")]) == {}


def test_commented_pyproject_requirements_are_not_dependencies():
    lines = [
        "[project]",
        "dependencies = [",
        '  # "not-a-package>=1",',
        '  "real-package @ https://example.invalid/release#sha256=abc", # "other-package>=1"',
        "]",
    ]
    assert new_names(diff("pyproject.toml", lines)) == [("pypi", "real-package", 4, False)]
