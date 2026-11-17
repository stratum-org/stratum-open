from stratum_open.diff import approx_tokens, pack, parse_diff, render
from stratum_open.findings import Finding, anchor_to_diff, assign_fingerprints, dedupe, placeable, rank, risk_score

DIFF = """diff --git a/app/users.py b/app/users.py
index 3f1c2a1..9b2e7d0 100644
--- a/app/users.py
+++ b/app/users.py
@@ -1,6 +1,8 @@ import sqlite3
 import sqlite3
+import requests


 def get_user(conn, user_id):
-    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
+    cur = conn.execute(f"SELECT * FROM users WHERE id = {user_id}")
+    log(user_id)
     return cur.fetchone()
@@ -20,3 +22,4 @@ def delete_user(conn, user_id):
     conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
+    conn.commit()
     return True
\\ No newline at end of file
diff --git a/docs/new.md b/docs/new.md
new file mode 100644
--- /dev/null
+++ b/docs/new.md
@@ -0,0 +1,2 @@
+# New
+Hello
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
diff --git a/a.py b/b.py
similarity index 100%
rename from a.py
rename to b.py
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
"""


def test_parse_counts_lines_on_the_new_side():
    files = parse_diff(DIFF)
    assert [(f.path, f.status) for f in files] == [
        ("app/users.py", "modified"), ("docs/new.md", "added"), ("old.txt", "deleted"), ("b.py", "renamed"), ("logo.png", "modified"),
    ]
    users = files[0]
    assert users.added() == [
        (2, "import requests"), (6, '    cur = conn.execute(f"SELECT * FROM users WHERE id = {user_id}")'),
        (7, "    log(user_id)"), (23, "    conn.commit()"),
    ]
    assert (users.additions, users.deletions) == (4, 1)
    assert users.new_side()[8] == "    return cur.fetchone()"
    assert files[3].old_path == "a.py"
    assert files[4].binary


def test_can_comment_only_inside_one_hunk():
    users = parse_diff(DIFF)[0]
    assert users.can_comment(6, 7)
    assert users.can_comment(1)
    assert not users.can_comment(8, 22)  # spans two hunks
    assert not users.can_comment(15)  # between hunks


def test_a_submodule_is_binary_added_or_updated():
    commit = "+Subproject commit 769b12c6371165c97ca06837aa68da2e2c971e0c\n"
    added = parse_diff("diff --git a/lib b/lib\nnew file mode 160000\nindex 0000000..769b12c\n--- /dev/null\n+++ b/lib\n@@ -0,0 +1 @@\n" + commit)
    updated = parse_diff("diff --git a/lib b/lib\nindex 1234567..769b12c 160000\n--- a/lib\n+++ b/lib\n@@ -1 +1 @@\n-Subproject commit 1234567\n" + commit)
    assert added[0].status == "added" and added[0].binary and updated[0].binary
    assert pack(added, budget_tokens=10_000, max_chunks=3).chunks == []


def test_render_numbers_new_lines_only():
    text = render(parse_diff(DIFF)[0])
    assert "     6 +      cur = conn.execute(f\"SELECT" in text
    assert "       -      cur = conn.execute(\"SELECT" in text


def test_pack_skips_what_the_model_cant_use_and_respects_the_budget():
    files = parse_diff(DIFF)
    packed = pack(files, budget_tokens=10_000, max_chunks=3, skip=lambda p: p.endswith(".md"))
    assert packed.reviewed == ["app/users.py"]
    assert set(packed.skipped) == {"docs/new.md", "old.txt", "b.py", "logo.png"}

    big = parse_diff(DIFF)[:1]
    tiny = pack(big, budget_tokens=approx_tokens(render(big[0], big[0].hunks[:1])) + 5, max_chunks=1)
    assert tiny.clipped == ["app/users.py"] and len(tiny.chunks) == 1

    none = pack(big, budget_tokens=5, max_chunks=1)
    assert none.omitted == ["app/users.py"] and none.chunks == []


def test_findings_are_anchored_fingerprinted_and_deduped():
    files = parse_diff(DIFF)
    a = Finding("SQL injection", "Built from a string.", "high", "security", "app/users.py", 6, 6)
    same_spot = Finding("Unsafe query", "Same thing in other words.", "medium", "security", "app/users.py", 6, 7)
    off_diff = Finding("Somewhere else", "Not in this PR.", "low", "bug", "lib/other.py", 3, 3)
    between = Finding("Between hunks", "Line 15 isn't shown.", "low", "bug", "app/users.py", 15, 15, replacement="x")
    kept = anchor_to_diff([a, same_spot, off_diff, between], files)
    assert off_diff not in kept
    assert (between.line_start, between.replacement) == (None, None)
    assign_fingerprints(kept, files)
    assert dedupe(kept) == [a, between]


def test_fingerprint_follows_the_code_not_the_line_number():
    first = parse_diff(DIFF)
    moved = parse_diff(DIFF.replace("@@ -1,6 +1,8 @@", "@@ -1,6 +11,8 @@"))
    f1 = Finding("SQL injection", "x", "high", "security", "app/users.py", 6, 6)
    f2 = Finding("Injection risk", "y", "high", "security", "app/users.py", 16, 16)
    assign_fingerprints([f1], first)
    assign_fingerprints([f2], moved)
    assert f1.fingerprint == f2.fingerprint


def test_rank_and_risk():
    low = Finding("a", "b", "low")
    critical = Finding("c", "d", "critical")
    assert rank([low, critical]) == [critical, low]
    assert risk_score([]) == 5
    assert risk_score([low]) == 20
    assert risk_score([low, critical]) == 90


def test_unknown_severity_and_category_are_normalised():
    finding = Finding("t", "d", severity="urgent", category="weird")
    assert (finding.severity, finding.category, finding.rule) == ("medium", "bug", "model:t")


def test_only_newline_ends_a_line():
    crafted = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a = 1\u2028+fake = 2\n-b = 1\n+b = 2\r\n"
    [file] = parse_diff(crafted)
    assert file.added() == [(2, "b = 2")]
    assert file.new_side()[1] == "a = 1\u2028+fake = 2"


def test_two_check_findings_on_one_line_stay_apart():
    files = parse_diff(DIFF)
    a = Finding("New dependency: `pyyaml`", "x", "low", "supply-chain", "app/users.py", 2, source="check", rule="deps/new")
    b = Finding("New dependency: `requests`", "x", "low", "supply-chain", "app/users.py", 2, source="check", rule="deps/new")
    assign_fingerprints([a, b], files)
    assert a.fingerprint != b.fingerprint and dedupe([a, b]) == [a, b]


def test_a_hunk_that_only_removes_lines_is_one_note():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,5 +1,2 @@ def f():\n x = 1\n-y = 2\n-z = 3\n-w = 4\n x2 = 5\n"
        "@@ -10,2 +7,3 @@\n a = 1\n+b = 2\n c = 3\n"
    )
    text = render(parse_diff(diff)[0])
    assert "(3 lines removed after line 1)" in text
    assert "y = 2" not in text and "z = 3" not in text
    assert "     8 +  b = 2" in text  # a hunk that adds lines is shown in full


MOVED = "diff --git a/tests/helper.py b/app/helper.py\nsimilarity index 100%\nrename from tests/helper.py\nrename to app/helper.py\n"
HELPER = "import os\n\ndef run(cmd):\n    eval(cmd)\n    eval(cmd + '!')\n"


def test_a_check_finding_off_the_diff_keeps_its_line_but_isnt_placed_inline():
    files = parse_diff(MOVED)
    check = Finding("`eval()` runs a string as code", "x", "high", "security", "app/helper.py", 4, 4,
                    replacement="safe(cmd)", source="check", rule="code/eval")
    model = Finding("Unsafe", "y", "high", "security", "app/helper.py", 4, 4)
    kept = anchor_to_diff([check, model], files)
    assert (check.line_start, check.replacement) == (4, None)
    assert model.line_start is None
    assert placeable(kept, files) == []
    on_diff, users = Finding("SQL injection", "z", "high", "security", "app/users.py", 6, 6), parse_diff(DIFF)
    assert placeable(anchor_to_diff([on_diff], users), users) == [on_diff]


def test_check_findings_off_the_diff_key_on_their_own_line_of_the_file():
    files = parse_diff(MOVED)
    first, second = (Finding("`eval()` runs a string as code", "x", "high", "security", "app/helper.py", n, n, source="check", rule="code/eval")
                     for n in (4, 5))
    assign_fingerprints([first, second], files, {"app/helper.py": HELPER})
    assert first.fingerprint != second.fingerprint and dedupe([first, second]) == [first, second]

    # The same line added in a diff gets the same fingerprint, so an ignore or a thread follows it.
    added = parse_diff("diff --git a/app/helper.py b/app/helper.py\nnew file mode 100644\n--- /dev/null\n+++ b/app/helper.py\n"
                       "@@ -0,0 +1,5 @@\n" + "".join(f"+{line}\n" for line in HELPER.splitlines()))
    again = Finding("`eval()` runs a string as code", "x", "high", "security", "app/helper.py", 4, 4, source="check", rule="code/eval")
    assign_fingerprints([again], added)
    assert again.fingerprint == first.fingerprint


def test_a_finding_spanning_hunks_is_narrowed_to_its_first_line():
    span = Finding("Spans two hunks", "detail", "high", "bug", "app/users.py", 6, 22)
    anchor_to_diff([span], parse_diff(DIFF))
    assert (span.line_start, span.line_end) == (6, 6)


def test_deduplication_prefers_the_anchored_finding():
    loose = Finding("Same issue", "file-level copy", "high", "bug", "app/users.py", confidence=0.9)
    pinned = Finding("Same issue", "anchored copy", "high", "bug", "app/users.py", 6, 6, confidence=0.9)
    loose.fingerprint = pinned.fingerprint = "same"
    assert dedupe([loose, pinned]) == [pinned]


def test_risk_score_is_capped_at_one_hundred():
    assert risk_score([Finding("Title", "Detail", "critical")] * 10) == 100


def test_finding_deserialization_reads_only_usable_numeric_lines():
    for value, expected in [(True, None), (3.0, 3), ("7", 7), ("0", None), (-1, None)]:
        data = {"title": "Title", "detail": "Detail", "path": "a.py", "line_start": value}
        assert Finding.from_dict(data).line_start == expected
