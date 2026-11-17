from stratum_open.checks import awaits, run_checks
from stratum_open.diff import parse_diff
from stratum_open.policy import Policy


def added_file(path, text):
    """A diff adding every line of `text`, and the file's full text."""
    lines = text.strip("\n").split("\n")
    head = [f"diff --git a/{path} b/{path}", "new file mode 100644", "--- /dev/null", f"+++ b/{path}", f"@@ -0,0 +1,{len(lines)} @@"]
    return parse_diff("\n".join(head + [f"+{line}" for line in lines]) + "\n"), {path: "\n".join(lines) + "\n"}


def flagged(text, path="app.py"):
    files, contents = added_file(path, text)
    return [(f.line_start, f.title) for f in awaits.check(files, contents)]


def test_a_call_awaited_elsewhere_in_the_file_but_not_here_is_flagged():
    assert flagged("""
async def fetch(client):
    return await client.get("/a")

async def fetch_both(client):
    first = client.get("/b")
    return first
""") == [(5, "`client.get()` isn't awaited")]


def test_awaited_handed_on_or_collected_calls_are_fine():
    assert flagged("""
import asyncio

async def job():
    await asyncio.sleep(1)

async def run(tasks):
    await job()
    task = asyncio.create_task(job())
    done = await asyncio.gather(job(), job())
    pending = [job() for _ in range(3)]
    later = job()
    await later
    return task, done, pending
""") == []


def test_async_with_and_a_coroutine_passed_to_an_awaited_call_are_fine():
    # `async with` awaits the context manager itself, and `wait_for(coro)` awaits the coroutine.
    assert flagged("""
import asyncio


class Repo:
    async def one(self):
        conn = await self.pool.acquire()
        return conn

    async def two(self):
        async with self.pool.acquire() as conn:
            return conn

    async def three(self):
        coro = self.fetch()
        return await asyncio.wait_for(coro, timeout=5)

    async def four(self):
        return await self.fetch()

    async def five(self):
        coro = self.fetch()
        return await asyncio.wait_for(fut=coro, timeout=5)
""") == []
    # Still a miss: a bare call, and a coroutine that's only returned.
    assert flagged("""
class Repo:
    async def one(self):
        return await self.pool.acquire()

    async def two(self):
        self.pool.acquire()
        coro = self.pool.acquire()
        return coro
""") == [(6, "`self.pool.acquire()` isn't awaited"), (7, "`self.pool.acquire()` isn't awaited")]


def test_sync_functions_and_nested_sync_defs_are_left_alone():
    assert flagged("""
async def save(store, item):
    await store.put(item)

def save_sync(store, item):
    store.put(item)

async def outer(store):
    def callback(item):
        store.put(item)
    await store.put(1)
    return callback
""") == []


def test_self_attributes_count_only_within_their_own_class():
    # One class's `self._file` is an async file, the other's a plain one: a
    # call in the second isn't missing an await just because the first awaits.
    assert flagged("""
class AsyncStream:
    async def close(self):
        await self._file.close()

class SyncStream:
    async def close(self):
        self._file.close()
""") == []
    assert flagged("""
class Stream:
    async def read(self):
        return await self._file.read()

    async def close(self):
        self._file.read()
""") == [(6, "`self._file.read()` isn't awaited")]


def test_a_call_the_prs_removed_line_awaited_counts_without_the_full_text():
    diff = "\n".join([
        "diff --git a/app.py b/app.py", "--- a/app.py", "+++ b/app.py",
        "@@ -10,3 +10,3 @@ async def export(self, item):",
        "     if self.ready:",
        "-        await self.lock.acquire()",
        "+        self.lock.acquire()",
        "     return item",
    ]) + "\n"
    findings = awaits.check(parse_diff(diff))
    assert [(f.line_start, f.rule) for f in findings] == [(11, "code/missing-await")]


def test_unparseable_files_and_other_languages_are_skipped():
    assert flagged("async def broken(:\n    await x()\n    x()\n") == []
    assert flagged("async function f() { await g(); g(); }", path="app.js") == []


def test_a_file_too_deep_to_parse_is_skipped_without_an_error():
    # One long generated expression: ast.parse raises RecursionError past ~3000 terms.
    files, _ = added_file("gen.py", "TOTAL = 1\nasync def f():\n    await g()\n")
    contents = {"gen.py": "TOTAL = " + " + ".join(["1"] * 3100) + "\n"}
    assert [f.rule for f in run_checks(files, Policy(), contents=contents) if f.rule == "checks/error"] == []


def test_async_defs_inside_loops_handlers_and_matches_are_checked():
    assert flagged("""
try:
    import uvloop
except ImportError:
    async def a(client):
        await client.close()
        client.close()
for _ in range(1):
    async def b(client):
        await client.close()
        client.close()
while False:
    async def c(client):
        await client.close()
        client.close()
match 1:
    case 1:
        async def d(client):
            await client.close()
            client.close()
""") == [(n, "`client.close()` isn't awaited") for n in (6, 10, 14, 19)]


def test_async_defs_inside_except_star_are_checked():
    assert flagged("""
try:
    async def a(client):
        await client.close()
        client.close()
except* ValueError:
    pass
""") == [(4, "`client.close()` isn't awaited")]


def test_async_defs_inside_async_for_are_checked():
    assert flagged("""
async def outer(stream):
    async for _ in stream:
        async def a(client):
            await client.close()
            client.close()
""") == [(5, "`client.close()` isn't awaited")]


def test_it_runs_with_the_other_checks():
    files, contents = added_file("app.py", "async def a(s):\n    await s.send()\n    s.send()\n")
    rules = [f.rule for f in run_checks(files, Policy(), contents=contents)]
    assert "code/missing-await" in rules


def test_touches_async():
    files, _ = added_file("app.py", "async def a():\n    pass\n")
    assert awaits.touches_async(files[0])
    files, _ = added_file("app.py", "def a():\n    pass\n")
    assert not awaits.touches_async(files[0])


def test_a_removed_self_await_counts_only_in_the_class_its_hunk_is_in():
    # The PR drops `await self._file.close()` from AsyncStream and adds a
    # plain `self._file.close()` to SyncStream: the second isn't missing one.
    new = "\n".join([
        "class AsyncStream:",
        "    async def close(self):",
        "        pass",
        "",
        "class SyncStream:",
        "    async def close(self):",
        "        self._file.close()",
    ])
    diff = "\n".join([
        "diff --git a/app.py b/app.py", "--- a/app.py", "+++ b/app.py",
        "@@ -1,3 +1,3 @@", " class AsyncStream:", "     async def close(self):", "-        await self._file.close()", "+        pass",
        "@@ -5,3 +5,3 @@", " class SyncStream:", "     async def close(self):", "-        pass", "+        self._file.close()",
    ]) + "\n"
    assert awaits.check(parse_diff(diff), {"app.py": new + "\n"}) == []
    # Within the same class it still counts.
    same = "\n".join([
        "diff --git a/app.py b/app.py", "--- a/app.py", "+++ b/app.py",
        "@@ -1,3 +1,3 @@", " class Stream:", "     async def close(self):", "-        await self._file.close()", "+        self._file.close()",
    ]) + "\n"
    text = "class Stream:\n    async def close(self):\n        self._file.close()\n"
    assert [f.line_start for f in awaits.check(parse_diff(same), {"app.py": text})] == [3]


def test_without_the_full_text_a_call_handed_on_or_collected_is_left_alone():
    def diff(added):
        return parse_diff("\n".join([
            "diff --git a/app.py b/app.py", "--- a/app.py", "+++ b/app.py",
            "@@ -10,2 +10,2 @@ async def run(self):", "-    await job()", f"+    {added}", "     return None",
        ]) + "\n")
    for added in ("task = asyncio.create_task(job())", "await asyncio.gather(job())", "jobs = [job()]", "log(job())"):
        assert awaits.check(diff(added)) == [], added
    for added in ("job()", "result = job()", "return job()"):
        assert [f.line_start for f in awaits.check(diff(added))] == [10], added


def sync_flagged(text):
    files, contents = added_file("app.py", text)
    return sorted((f.line_start, f.rule) for f in awaits.check_sync_files(files, contents))


def test_awaiting_a_plain_file_from_open_is_flagged():
    # The shape that shipped in encode/httpx#3670: a plain file awaited like an async one.
    assert sync_flagged("""
class MultiPartStream:
    async def read(self):
        if self._fin is None:
            self._fin = open(self._path, "rb")
        chunk = await self._fin.read(65536)
        await self._fin.close()
        return chunk

async def dump(path):
    with open(path) as f:
        return await f.read()
""") == [(5, "code/await-sync-file"), (6, "code/await-sync-file"), (11, "code/await-sync-file")]


def test_files_from_an_async_open_are_left_alone():
    assert sync_flagged("""
import aiofiles

async def dump(path):
    async with aiofiles.open(path) as f:
        return await f.read()
""") == []
    assert sync_flagged("""
from anyio import open_file as open

async def dump(path):
    f = open(path)
    return await f.read()
""") == []
    assert sync_flagged("""
class Stream:
    def __init__(self, fin):
        self._fin = fin

    async def read(self):
        return await self._fin.read()
""") == []


def test_a_plain_file_in_one_function_says_nothing_about_another():
    assert sync_flagged("""
def save(path):
    with open(path) as f:
        f.write("x")

async def load(path):
    async with anyio.open_file(path) as f:
        return await f.read()
""") == []


def test_a_name_rebound_to_something_else_is_no_longer_a_plain_file():
    assert sync_flagged("""
async def load(path):
    f = open(path)
    f = anyio.wrap_file(f)
    return await f.read()
""") == []


def test_awaited_expression_results_do_not_flag_the_calls():
    assert flagged("""
async def get(fresh):
    await load()
    await (load() if fresh else load())
    await (load() or load())
""") == []
    assert [line for line, _ in flagged("""
async def get(fresh, x, y):
    await load()
    await (x if load() else y)
    return load() if fresh else None
""")] == [3, 4]
