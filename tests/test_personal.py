"""Offline check of personal.py: retry/remap, label states, daily cap, must dedupe. Run: .venv/bin/python -m tests.test_personal"""

import json
import sqlite3
from types import SimpleNamespace

import run as R
from core import common
from core import personal as P


class FakeAI:
    """Batch calls omit `omit` titles; single calls refuse `refuse` titles."""

    def __init__(self, omit: set[str], refuse: set[str]) -> None:
        self.omit, self.refuse, self.calls = omit, refuse, []
        self.messages = self

    def parse(self, **kw):
        if kw["output_format"] is P.MustGroups:  # second pass: no duplicates found
            return SimpleNamespace(stop_reason="end_turn", stop_details=None, parsed_output=P.MustGroups(groups=[]),
                                   usage=SimpleNamespace(input_tokens=0, output_tokens=0))
        content = kw["messages"][0]["content"]
        rows = [json.loads(l) for l in content.splitlines() if l.startswith("{")]
        items = [r for r in rows if isinstance(r["id"], int)]
        self.calls.append(len(items))
        self.seen_ids = [r["id"] for r in rows if isinstance(r["id"], str)]
        usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0)
        if len(items) == 1 and items[0]["title"] in self.refuse:
            return SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="x"),
                                   parsed_output=None, usage=usage)
        keep = [i for i in items if len(items) == 1 or i["title"] not in self.omit]
        out = P.Verdicts(items=[P.Verdict(id=i["id"], reason=i["title"], category="other", tier=None,
                                          score=9, wildcard=None, confidence="high") for i in keep])
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, parsed_output=out, usage=usage)


batch = [{"feed": "f", "domain": "d", "title": t, "summary": "s"} for t in "abcd"]
ai = FakeAI(omit={"b", "c"}, refuse={"c"})
got = P.classify(ai, batch, "low", [{"id": "s7", "title": "old story", "score": 8}])
assert [v and v.reason for v in got] == ["a", "b", None, "d"], got
assert ai.calls == [4, 1, 1], ai.calls  # one batch, then singles for b and c only
assert ai.seen_ids == ["s7"], ai.seen_ids  # dedupe context survives into single retries


class FailBig(FakeAI):
    """Whole-call failure (truncated JSON) on any request over two items."""

    def parse(self, **kw):
        out = super().parse(**kw)
        if self.calls[-1] > 2:
            raise ValueError("truncated")
        return out


ai = FailBig(set(), set())
got = P.classify(ai, batch, "low", [])
assert [v.reason for v in got] == list("abcd") and ai.calls == [4, 2, 2], ai.calls  # halves, not 4 singles


class Pages:  # the same id on every page
    def stream_contents(self, **kw):
        return {"items": [{"id": "p"}], "continuation": "c"}


assert P.fetch(Pages(), 0) == [{"id": "p"}]

from core import inoreader

cl = object.__new__(inoreader.Client)
cl._request = lambda method, path, params: params
assert cl.edit_tag(["x"], add=["A", "B"], remove="C") == [("i", "x"), ("a", "A"), ("a", "B"), ("r", "C")]


class FakeInoreader:
    def __init__(self, fail: bool) -> None:
        self.fail, self.calls = fail, []

    def edit_tag(self, ids, add):
        if self.fail:
            raise RuntimeError("POST edit-tag -> HTTP 429")
        self.calls.append((add, sorted(ids)))


db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE items (id TEXT, score INTEGER, wildcard INTEGER, label_state TEXT)")
db.executemany("INSERT INTO items VALUES (?, ?, ?, ?)", [
    ("must", 9, None, "pending"), ("wild", 2, 8, "pending"), ("none", None, None, "pending"),
    ("maybe", 5, None, "pending"), ("noise", 1, None, "pending"), ("dry", 10, None, "dry-run"),
])
P.label(FakeInoreader(fail=True), db)
assert db.execute("SELECT COUNT(*) FROM items WHERE label_state = 'pending'").fetchone()[0] == 5
ino = FakeInoreader(fail=False)
P.label(ino, db)
assert ino.calls == [
    (["user/-/label/LLM-must", "user/-/state/com.google/starred"], ["must"]),  # same condition: one call
    (["user/-/label/LLM-maybe"], ["maybe"]), (["user/-/label/LLM-wild"], ["wild"]),
    (["user/-/state/com.google/read"], ["maybe", "noise"]),  # maybe is read too; not wild, not unscored
], ino.calls
assert dict(db.execute("SELECT id, label_state FROM items")) == {
    "must": "done", "wild": "done", "none": "done", "maybe": "done", "noise": "done", "dry": "dry-run"}

db = sqlite3.connect(":memory:")
db.execute("CREATE TABLE items (title TEXT, score INTEGER, classified_at TEXT)")
db.executemany("INSERT INTO items VALUES (?, ?, ?)", [
    ("fresh", 8, "2999-01-01T00:00:00+00:00"), ("maybe", 6, "2999-01-01T00:00:00+00:00"),
    ("stale", 9, "2000-01-01T00:00:00+00:00"), ("refused", None, "2999-01-01T00:00:00+00:00"),
])
assert P.recent_scored(db) == [{"id": "s1", "title": "fresh", "score": 8}], P.recent_scored(db)

# Daily cap: classify only DAILY_MAX, keep the watermark at the last classified item, then stop fetching.
import sys
import tempfile
from pathlib import Path


class FakeClient:
    limits = SimpleNamespace(zone1_usage=0, zone1_limit=100, zone2_usage=0, zone2_limit=100, reset_after_s=0)
    fetches = 0

    def stream_contents(self, **kw):
        self.fetches += 1
        return {"items": [{"id": f"i{n}", "timestampUsec": str(n * 1_000_000), "title": f"t{n}"}
                          for n in range(1, 6)]}


fake = FakeClient()
R.Client = SimpleNamespace(from_env=lambda: fake)
R.anthropic.Anthropic = lambda: FakeAI(set(), set())
common.DB_PATH = Path(tempfile.mkdtemp()) / "state.sqlite"
P.DAILY_MAX = 3
sys.argv = ["run.py", "--dry-run", "--only", "personal"]
R.main()
R.main()
db = sqlite3.connect(common.DB_PATH)
assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3
assert db.execute("SELECT value FROM meta WHERE key = 'watermark_usec'").fetchone()[0] == "3000000"
assert fake.fetches == 1, fake.fetches  # second run hit the cap before fetching


# Batch-local "dup N" is stored as "dup s<rowid>" so dedupe_must can follow it.
class DupClient(FakeClient):
    def stream_contents(self, **kw):
        return {"items": [{"id": "x1", "timestampUsec": "1", "title": "a"},
                          {"id": "x2", "timestampUsec": "2", "title": "dup 1: same story"}]}


R.Client = SimpleNamespace(from_env=DupClient)
common.DB_PATH = Path(tempfile.mkdtemp()) / "state.sqlite"
P.DAILY_MAX = 2000
R.main()
db = sqlite3.connect(common.DB_PATH)
assert db.execute("SELECT reason FROM items WHERE id = 'x2'").fetchone()[0] == "dup s1: same story"

# Must dedupe: an official post that the first pass marked "dup s1" of an already-tagged rewrite takes over.
from datetime import UTC, datetime, timedelta


class FakeDedupeAI:
    def __init__(self, groups) -> None:
        self.groups, self.sent = groups, None
        self.messages = self

    def parse(self, **kw):
        self.sent = [json.loads(l)["id"] for l in kw["messages"][0]["content"].splitlines()]
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, usage=SimpleNamespace(input_tokens=0, output_tokens=0),
                               parsed_output=P.MustGroups(groups=self.groups))


class RecordingIno:
    def __init__(self) -> None:
        self.calls = []

    def edit_tag(self, ids, add=None, remove=None):
        self.calls.append((add, remove, sorted(ids)))


iso = lambda d: (datetime.now(UTC) + d).isoformat(timespec="seconds")
common.DB_PATH = Path(tempfile.mkdtemp()) / "state.sqlite"
db = common.open_db()
db.executemany("INSERT INTO items (id, title, score, reason, classified_at, label_state) VALUES (?, ?, ?, ?, ?, ?)", [
    ("news", "Rewrite of launch", 9, "launch", iso(timedelta(hours=-1)), "done"),         # rowid 1
    ("official", "Vendor launch post", 2, "dup s1", iso(timedelta(0)), "pending"),        # rowid 2, this run
    ("other", "Unrelated must-read", 10, "other", iso(timedelta(hours=-1)), "done"),      # rowid 3
    ("old", "Ancient must-read", 10, "old", iso(timedelta(days=-5)), "done"),             # outside window
])
db.commit()
ai = FakeDedupeAI([P.MustGroup(reason="vendor's own post", keep=2, drop=[1])])
ino = RecordingIno()
P.dedupe_must(ai, ino, db, effort="low", dry_run=False)
assert ai.sent == [1, 2, 3], ai.sent
assert ino.calls == [(None, [P.MUST_TAG, P.STAR_TAG], ["news"])], ino.calls  # official was pending
rows = {r[0]: r[1:] for r in db.execute("SELECT id, score, reason, label_state FROM items")}
assert rows["official"][0] == 9 and rows["official"][2] == "pending", rows
assert rows["news"][0] == 3 and rows["news"][1].startswith("dup s2") and rows["news"][2] == "pending", rows
assert rows["other"] == (10, "other", "done"), rows
P.label(ino, db)
assert ([P.MUST_TAG, P.STAR_TAG], None, ["official"]) in ino.calls and ([P.READ_TAG], None, ["news"]) in ino.calls, ino.calls

# Next run, nothing new: no call. A new must-read triggers one, and the demoted row is not offered again.
ai2 = FakeDedupeAI([])
P.dedupe_must(ai2, ino, db, effort="low", dry_run=False)
assert ai2.sent is None, ai2.sent
db.execute("INSERT INTO items (id, title, score, reason, classified_at, label_state) VALUES "
           "('new', 'New must-read', 9, 'new', ?, 'pending')", (iso(timedelta(0)),))
P.dedupe_must(ai2, ino, db, effort="low", dry_run=False)
assert ai2.sent == [2, 3, 5], ai2.sent
print("ok")
