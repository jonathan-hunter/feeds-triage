"""Offline check of academic.py and the combined run.py. Run: .venv/bin/python -m tests.test_academic"""

import json
import os
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import anthropic

import run as R
from core import academic as A
from core import common
from core import personal as P

tmp = Path(tempfile.mkdtemp())
common.DB_PATH, A.FEED_PATH = tmp / "state.sqlite", tmp / "feed.xml"
os.environ.update(OPENALEX_API_KEY="k", ACAD_GIST_ID="g", GITHUB_GIST_TOKEN="t")
os.environ.pop("ACAD_FEED_URL", None)


def work(n: int, title: str, author: str = "Ann Lee", doi: str | None = None, abstract: str | None = "an abstract"):
    return {"id": f"https://openalex.org/W{n}", "doi": doi, "display_name": title, "publication_date": "2026-10-20",
            "type": "article", "primary_location": {"source": {"display_name": "Genome Biology"}},
            "authorships": [{"author": {"display_name": author}, "institutions": [{"display_name": "Example University"}]}],
            "abstract_inverted_index": {w: [i] for i, w in enumerate(abstract.split())} if abstract else None}


WORKS = {  # query name -> results
    "formats": [work(1, "AnnData 2.1 & <i>beyond</i>", doi="https://doi.org/10.1/a"),
                work(2, "Single-cell atlas of mouse liver", doi="https://doi.org/10.1/b"),
                work(3, "scQC: reporting QC", abstract=None)],
    "frameworks": [work(1, "AnnData 2.1 & <i>beyond</i>", doi="https://doi.org/10.1/a"),  # same work, second query
                   work(4, "scQC reporting QC", author="A Lee", doi="https://doi.org/10.1/c")],  # published W3
}
SCORES = {"AnnData 2.1 & beyond": 95, "Single-cell atlas of mouse liver": 20, "scQC: reporting QC": 70}
A.QUERIES = tmp / "q.toml"
A.QUERIES.write_text('window_days = 30\ntypes = ["article"]\n'
                     '[[query]]\nname = "formats"\nsearch = "AnnData"\n[[query]]\nname = "frameworks"\nsearch = "scverse"\n')
oa_calls = []


def fake_oa_get(path, params):
    name = "formats" if "AnnData" in params["filter"] else "frameworks"
    oa_calls.append(name)
    return {"results": WORKS[name], "meta": {"next_cursor": None}}, {"X-RateLimit-Remaining-Usd": "0.99"}


class FakeAI:
    def __init__(self) -> None:
        self.messages, self.calls, self.fail_next = self, 0, False

    def parse(self, **kw):
        if self.fail_next:
            self.fail_next = False
            raise Exception.__new__(anthropic.APIConnectionError)  # any APIError; skips its request arg
        self.calls += 1
        usage = SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0)
        if kw["output_format"] is P.MustGroups:
            return SimpleNamespace(stop_reason="end_turn", stop_details=None, usage=usage,
                                   parsed_output=P.MustGroups(groups=[]))
        rows = [json.loads(l) for l in kw["messages"][0]["content"].splitlines() if l.startswith("{")]
        if kw["output_format"] is P.Verdicts:
            out = P.Verdicts(items=[P.Verdict(id=r["id"], reason="r", category="other", tier=None, score=2,
                                              wildcard=None, confidence="high") for r in rows])
        else:
            assert kw["system"][0]["text"] == A.RUBRIC
            out = A.PaperVerdicts(items=[A.PaperVerdict(
                id=r["id"], rationale=f"why {r['title']}", tier="core", focus=True, exclusion=None,
                score=SCORES[r["title"]], confidence="high" if r["abstract"] else "low") for r in rows])
        return SimpleNamespace(stop_reason="end_turn", stop_details=None, usage=usage, parsed_output=out)


A.oa_get = fake_oa_get
pushes = []
A.push_gist = pushes.append
ai = FakeAI()
R.anthropic.Anthropic = lambda: ai


def run(*argv: str) -> None:
    sys.argv = ["run.py", *argv]
    R.main()


# An API error after the poll leaves the papers unscored; the next run scores them without polling again.
ai.fail_next = True
run("--only", "academic")
db = sqlite3.connect(common.DB_PATH)
assert dict(db.execute("SELECT label_state, COUNT(*) FROM papers GROUP BY 1")) == {"unscored": 3, "dup": 1}
assert len(oa_calls) == 2 and len(pushes) == 1 and "<entry>" not in pushes[0], (oa_calls, pushes)  # empty feed
run("--only", "academic")
assert len(oa_calls) == 2, oa_calls  # gated: polled < POLL_HOURS ago

rows = {r[0].rsplit("/", 1)[-1]: r[1:] for r in db.execute(
    "SELECT openalex_id, score, label_state, queries, rationale, confidence FROM papers")}
assert rows["W1"][:3] == (95, "new", "formats,frameworks"), rows  # found twice, scored once
assert rows["W2"][:2] == (20, "new"), rows
assert rows["W3"][:2] == (70, "new") and rows["W3"][4] == "low", rows  # no abstract
assert rows["W4"][1:] == ("dup", "frameworks", "dup https://openalex.org/W3", None), rows  # published version of W3
assert ai.calls == 1, ai.calls  # three papers, one batch

assert len(pushes) == 2
feed = pushes[-1]
assert feed.count("<entry>") == 2 and "W2" not in feed, feed  # 20 is below PUBLISH_MIN
assert "<title>[95] AnnData 2.1 &amp; beyond</title>" in feed, feed
assert '<link href="https://doi.org/10.1/a"/>' in feed and '<link href="https://openalex.org/W3"/>' in feed, feed
assert "&lt;p&gt;Score 95 · core · focus · high confidence&lt;/p&gt;" in feed, feed  # HTML content, escaped once
ET.fromstring(feed)  # well-formed
assert A.FEED_PATH.read_text() == feed

# Forced poll, nothing new: feed unchanged apart from its timestamp, so no push and no model call.
run("--only", "academic", "--force-poll")
assert len(pushes) == 2 and ai.calls == 1 and len(oa_calls) == 4, (pushes, ai.calls, oa_calls)

# A dry run's papers stay out of the published feed.
WORKS["formats"].append(work(5, "AnnData 2.1 & <i>beyond</i>", author="Bo Chan"))  # 4 words: author matters
SCORES["AnnData 2.1 & beyond"] = 88
run("--only", "academic", "--dry-run", "--force-poll")
assert db.execute("SELECT score, label_state FROM papers WHERE openalex_id LIKE '%W5'").fetchone() == (88, "dry-run")
assert len(pushes) == 2 and A.FEED_PATH.read_text().count("<entry>") == 3  # local preview includes it
assert len(A.feed_rows(db, dry_run=False)) == 2

# Combined run: feed items are matched to papers and labelled, never scored or stored in items.
FEED_URL = "https://gist.githubusercontent.com/u/g/raw/feed.xml"
os.environ["ACAD_FEED_URL"] = FEED_URL


class FakeClient:
    limits = SimpleNamespace(zone1_usage=0, zone1_limit=100, zone2_usage=0, zone2_limit=100, reset_after_s=0)

    def __init__(self) -> None:
        self.tagged = []

    def stream_contents(self, **kw):
        acad = {"origin": {"streamId": f"feed/{FEED_URL}", "title": "Academic alerts"}, "timestampUsec": "2"}
        return {"items": [
            {**acad, "id": "ino-1", "title": "[95] AnnData 2.1 &amp; beyond",
             "canonical": [{"href": "https://doi.org/10.1/a"}]},
            {**acad, "id": "ino-3", "title": "[70] scQC: reporting QC", "alternate": [{"href": "https://elsewhere/x"}]},
            {**acad, "id": "ino-9", "title": "[50] Not in the database"},
            {"id": "news-1", "timestampUsec": "1", "title": "Some news", "origin": {"title": "Blog"}},
        ]}

    def edit_tag(self, ids, add=None, remove=None):
        self.tagged.append((add, sorted(ids)))


client = FakeClient()
R.Client = SimpleNamespace(from_env=lambda: client)
before = list(db.execute("SELECT * FROM papers"))
A.match_items(db, client.stream_contents()["items"][:3], dry_run=True)
assert list(db.execute("SELECT * FROM papers")) == before  # a dry run leaves papers untouched

run()
assert len(oa_calls) == 6, oa_calls  # gated: the forced polls above also reset the gate
assert [r[0] for r in db.execute("SELECT id FROM items")] == ["news-1"]
got = dict(db.execute("SELECT inoreader_id, label_state FROM papers WHERE inoreader_id IS NOT NULL"))
assert got == {"ino-1": "done", "ino-3": "done"}, got  # by link, then by title when the link differs
assert (["user/-/label/ACAD-core", common.STAR_TAG], ["ino-1"]) in client.tagged, client.tagged
assert (["user/-/label/ACAD-read"], ["ino-3"]) in client.tagged, client.tagged
assert not any("ino-9" in ids for _, ids in client.tagged), client.tagged


# An academic failure doesn't stop the personal pipeline, but the run still fails.
def boom(*a, **k):
    raise RuntimeError("bug")


real, A.score_unscored = A.score_unscored, boom
n_items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
client.stream_contents = lambda **kw: {"items": [{"id": "news-2", "timestampUsec": "3", "title": "More news"}]}
try:
    run()
except RuntimeError as e:
    assert str(e) == "bug"
else:
    raise AssertionError("academic failure was swallowed")
assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == n_items + 1  # personal still ran
A.score_unscored = real
print("ok")
