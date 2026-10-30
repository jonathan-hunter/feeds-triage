"""Academic alerts: OpenAlex -> Claude Haiku against rubrics/academic.md (0-100) -> Atom feed in a secret gist.

Run through run.py. Each run:
  1. polls OpenAlex, at most every POLL_HOURS, storing unseen works as `unscored` (or `dup`);
  2. scores whatever is `unscored`;
  3. rebuilds state/feed.xml and pushes it to the gist when it changed (never on a dry run).
Inoreader subscribes to the gist's raw URL ($ACAD_FEED_URL). personal.run() hands those items back,
match_items() ties each to its stored paper, and label() gives it ACAD-* labels from the stored score.
OpenAlex prices: search $0.001, list $0.0001 per call, against a $1/day free-key budget.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tomllib
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Literal
from xml.sax.saxutils import escape

import anthropic
from pydantic import BaseModel, Field

from core.common import (MODEL, ROOT, RUBRICS, STAR_TAG, apply_labels, get_meta, html_to_text, http, insert, log, now_iso,
                    parse_call, read_rubric, retrying, rubric_slugs, set_meta)

RUBRIC = read_rubric("academic.md")
EXCLUSIONS = rubric_slugs(RUBRIC.partition("\n## Hard exclusions")[2].split("\n## ")[0], r"^- `([a-z-]+)`:")
QUERIES = RUBRICS / "academic_queries.toml"
FEED_PATH = ROOT / "state" / "feed.xml"
OPENALEX = "https://api.openalex.org"
SELECT = "id,doi,display_name,publication_date,type,primary_location,authorships,abstract_inverted_index"
POLL_HOURS = 20  # a poll re-reads the whole window (~$0.04), and journals publish daily
BATCH = 20
MAX_PAGES = 5  # per query per poll (500 works); ponytail: a query this broad needs tightening, not paging
RUN_MAX = 500  # papers scored per run; the rest stay unscored for the next run
ABSTRACT_CAP = 3000
MAX_AUTHORS = 10
MAX_INSTITUTIONS = 15
PUBLISH_MIN = 40
FEED_DAYS = 30
FEED_MAX = 200
TAGS = {  # Inoreader tag -> SQL condition on papers (0-100); below PUBLISH_MIN never reaches Inoreader
    "user/-/label/ACAD-core": "score >= 80",
    STAR_TAG: "score >= 80",
    "user/-/label/ACAD-read": "score BETWEEN 60 AND 79",
    "user/-/label/ACAD-maybe": "score BETWEEN 40 AND 59",
}
INSTRUCTIONS = ("Score each paper in <papers> independently against the rubric. "
                "Return exactly one item per paper, using its id.")
PROMPT_FIELDS = ("title", "venue", "pub_date", "type", "authors", "institutions", "abstract")


class PaperVerdict(BaseModel):  # field order: rationale before score, as the rubric asks
    id: int
    rationale: str = Field(description="<= 25 words: the specific contribution and why it matters to this "
                                       "reader; for low scores, the exclusion or missing contribution")
    tier: Literal["core", "adjacent", "peripheral", "out"]
    focus: bool = Field(description="matches a Current focus item")
    exclusion: Literal[EXCLUSIONS] | None = Field(
        description="the hard exclusion that applies, if any")
    score: int = Field(description="0-100, after modifiers and exclusions")
    confidence: Literal["high", "low"]


class PaperVerdicts(BaseModel):
    items: list[PaperVerdict]


# --- OpenAlex -------------------------------------------------------------------------------------

def abstract(inv: dict[str, list[int]] | None) -> str:
    """Rebuild OpenAlex's abstract_inverted_index.

    >>> abstract({"world": [1], "Hello": [0], "again": [3], "hello": [2]})
    'Hello world hello again'
    >>> abstract(None)
    ''
    """
    words = sorted((i, w) for w, pos in (inv or {}).items() for i in pos)
    return " ".join(w for _, w in words)


def title_key(title: str, first_author: str) -> str:
    """Matches a preprint to its published version when OpenAlex hasn't merged them. Short titles also need
    the first author's surname; long ones don't, because author order differs between versions.

    >>> title_key("scQC: a <i>versatile</i> way!", "Jane Q. Smith") == title_key("ScQC a versatile way", "J Smith")
    True
    >>> title_key("A report on single cell data workflows", "Goto")
    'a report on single cell data workflows'
    >>> title_key("", "Smith")  # no title: never a duplicate
    ''
    """
    words = re.findall(r"[a-z0-9]+", html_to_text(title).lower())
    if len(words) >= 6:
        return " ".join(words)
    last = (re.findall(r"[a-z]+", first_author.lower()) or [""])[-1]
    return " ".join(words) + "|" + last if words else ""


def query_filter(q: dict, window_days: int, types: list[str], today: date) -> str:
    """
    >>> query_filter({"sources": ["S1", "S2"], "search": "a OR b"}, 30, ["article"], date(2026, 10, 30))
    'from_publication_date:2026-09-30,to_publication_date:2026-11-06,type:article,primary_location.source.id:S1|S2,title_and_abstract.search:a OR b'
    """
    f = [f"from_publication_date:{today - timedelta(days=window_days)}",
         f"to_publication_date:{today + timedelta(days=7)}",  # cuts records dated a year ahead
         "type:" + "|".join(types)]
    if "sources" in q:
        f.append("primary_location.source.id:" + "|".join(q["sources"]))
    if "search" in q:
        f.append("title_and_abstract.search:" + q["search"])
    if "title" in q:
        f.append("title.search:" + q["title"])
    return ",".join(f)


def oa_get(path: str, params: dict) -> tuple[dict, dict]:
    """GET from OpenAlex; errors raise RuntimeError (429 = daily budget spent)."""
    req = urllib.request.Request(f"{OPENALEX}/{path}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f"Bearer {os.environ['OPENALEX_API_KEY']}"})
    body, headers = http(req, f"GET {path}")
    return json.loads(body), headers


def fetch_works(cfg: dict, today: date) -> tuple[dict[str, tuple[dict, list[str]]], bool]:
    """(openalex_id -> (work, names of the queries that found it), whether every query completed).
    Stops at the first failed call."""
    found: dict[str, tuple[dict, list[str]]] = {}
    headers: dict = {}
    for q in cfg["query"]:
        flt = query_filter(q, cfg["window_days"], cfg["types"], today)
        cursor, n = "*", 0
        try:
            for _ in range(MAX_PAGES):
                data, headers = oa_get("works", {"filter": flt, "per_page": 100, "select": SELECT, "cursor": cursor})
                for w in data["results"]:
                    found.setdefault(w["id"], (w, []))[1].append(q["name"])
                n += len(data["results"])
                if not (cursor := data["meta"].get("next_cursor")) or not data["results"]:
                    break
        except RuntimeError as e:
            log("openalex_failed", query=q["name"], error=str(e))
            return found, False
        log("query", name=q["name"], works=n)
    log("openalex", works=len(found), remaining_usd={k.lower(): v for k, v in headers.items()}
        .get("x-ratelimit-remaining-usd"))
    return found, True


def prep(w: dict) -> dict:
    loc = w.get("primary_location") or {}
    auths = w.get("authorships") or []
    insts = list(dict.fromkeys(i["display_name"] for a in auths for i in a.get("institutions") or []
                               if i.get("display_name")))
    authors = [(a.get("author") or {}).get("display_name") or "" for a in auths]
    return {
        "doi": w.get("doi"),
        "link": w.get("doi") or loc.get("landing_page_url") or w["id"],
        "title": html_to_text(w.get("display_name") or ""),
        "venue": (loc.get("source") or {}).get("display_name") or "",
        "pub_date": w.get("publication_date"),
        "type": w.get("type"),
        "authors": ", ".join(authors[:MAX_AUTHORS]) + (" et al." if len(authors) > MAX_AUTHORS else ""),
        "institutions": "; ".join(insts[:MAX_INSTITUTIONS]),
        "abstract": abstract(w.get("abstract_inverted_index"))[:ABSTRACT_CAP],
        "title_key": title_key(w.get("display_name") or "", authors[0] if authors else ""),
    }


def duplicate_of(db: sqlite3.Connection, pr: dict) -> str | None:
    """An earlier paper with the same DOI, else the same title key."""
    for col in ("doi", "title_key"):
        if pr[col] and (row := db.execute(f"SELECT openalex_id FROM papers WHERE {col} = ?", (pr[col],)).fetchone()):
            return row[0]
    return None


def poll(db: sqlite3.Connection) -> bool:
    """Store every unseen work as `unscored`, or as `dup` of an earlier paper. True if all queries completed."""
    found, complete = fetch_works(tomllib.loads(QUERIES.read_text()), date.today())
    new = dups = 0
    for oid, (w, names) in found.items():
        if db.execute("SELECT 1 FROM papers WHERE openalex_id = ?", (oid,)).fetchone():
            continue
        pr = prep(w)
        if dup := duplicate_of(db, pr):  # earlier rows in this loop count too: they're inserted already
            insert(db, "papers", {"openalex_id": oid, **{k: pr[k] for k in ("doi", "link", "title", "title_key")},
                                  "queries": ",".join(names), "rationale": f"dup {dup}", "classified_at": now_iso(),
                                  "label_state": "dup"})
            dups += 1
        else:
            insert(db, "papers", {"openalex_id": oid, **pr, "queries": ",".join(names), "label_state": "unscored"})
            new += 1
    db.commit()
    log("polled", found=len(found), new=new, dups=dups, complete=complete)
    return complete


# --- Scoring --------------------------------------------------------------------------------------

def score(ai: anthropic.Anthropic, batch: list[dict], effort: str) -> list[PaperVerdict | None]:
    def call(sub: list[dict]) -> list[PaperVerdict | None]:
        lines = [json.dumps({"id": i, **{k: p[k] for k in PROMPT_FIELDS}}, ensure_ascii=False)
                 for i, p in enumerate(sub, 1)]
        content = f"{INSTRUCTIONS}\n<papers>\n" + "\n".join(lines) + "\n</papers>"
        return parse_call(ai, RUBRIC, content, effort, PaperVerdicts, len(sub))

    return retrying(call, batch)


def score_unscored(ai: anthropic.Anthropic, db: sqlite3.Connection, effort: str, dry_run: bool) -> None:
    """A refusal is stored unscored for good; an API error leaves the rest `unscored` for the next run."""
    db.row_factory = sqlite3.Row
    todo = db.execute("SELECT * FROM papers WHERE label_state = 'unscored' ORDER BY rowid LIMIT ?",
                      (RUN_MAX,)).fetchall()
    db.row_factory = None
    state = "dry-run" if dry_run else "new"
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        try:
            verdicts = score(ai, [dict(r) for r in chunk], effort)
        except anthropic.APIError as e:  # SDK already retried
            log("score_failed", error=str(e)[:300])
            return
        now = now_iso()
        for r, v in zip(chunk, verdicts):
            verdict = ({**v.model_dump(exclude={"id"}), "score": max(0, min(100, v.score))} if v else
                       dict(rationale="no result (refused or failed)", tier=None, focus=None, exclusion=None,
                            score=None, confidence=None))
            sets = {**verdict, "model": MODEL, "effort": effort, "classified_at": now, "label_state": state}
            db.execute(f"UPDATE papers SET {', '.join(f'{k} = :{k}' for k in sets)} WHERE openalex_id = :oid",
                       {**sets, "oid": r["openalex_id"]})
        db.commit()
        log("scored", n=len(chunk), kept=sum(1 for v in verdicts if v and v.score >= PUBLISH_MIN))


# --- Feed -----------------------------------------------------------------------------------------

def render_feed(rows: list[sqlite3.Row], now: str) -> str:
    entries = []
    for r in rows:
        body = "".join(f"<p>{escape(x)}</p>" for x in (
            " · ".join(filter(None, [r["venue"], r["pub_date"], r["type"]])),
            r["authors"], r["institutions"],
            f"Score {r['score']} · {r['tier']}" + (" · focus" if r["focus"] else "")
            + (f" · {r['exclusion']}" if r["exclusion"] else "") + f" · {r['confidence']} confidence",
            r["rationale"], r["abstract"] or "(no abstract)") if x)
        entries.append(f"""<entry>
<id>{escape(r['openalex_id'])}</id>
<title>{escape(f"[{r['score']}] {r['title']}")}</title>
<link href="{escape(r['link'], {'"': '&quot;'})}"/>
<updated>{r['classified_at']}</updated>
<author><name>{escape(r['authors'].split(', ')[0] if r['authors'] else r['venue'] or 'unknown')}</name></author>
<content type="html">{escape(body)}</content>
</entry>""")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n'
            f"<title>Academic alerts</title>\n<id>urn:rss-triage:academic</id>\n<updated>{now}</updated>\n"
            + "\n".join(entries) + "\n</feed>\n")


def feed_rows(db: sqlite3.Connection, dry_run: bool) -> list[sqlite3.Row]:
    states = "('new', 'pending', 'done', 'dry-run')" if dry_run else "('new', 'pending', 'done')"
    db.row_factory = sqlite3.Row
    rows = db.execute(f"SELECT * FROM papers WHERE score >= ? AND classified_at >= ? AND label_state IN {states} "
                      "ORDER BY classified_at DESC, score DESC LIMIT ?",
                      (PUBLISH_MIN, now_iso(-timedelta(days=FEED_DAYS)), FEED_MAX)).fetchall()
    db.row_factory = None
    return rows


def push_gist(content: str) -> None:
    body = json.dumps({"files": {"feed.xml": {"content": content}}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/gists/{os.environ['ACAD_GIST_ID']}", data=body, method="PATCH",
        headers={"Authorization": f"Bearer {os.environ['GITHUB_GIST_TOKEN']}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "rss-triage"})
    http(req, "PATCH gist")


def publish(db: sqlite3.Connection, dry_run: bool) -> None:
    """Write state/feed.xml; push it unless it is a dry run or unchanged since the last push. Every push
    sends the whole file, so a failed one is simply retried by the next run."""
    rows = feed_rows(db, dry_run)
    xml = render_feed(rows, now_iso())
    FEED_PATH.write_text(xml)
    digest = hashlib.sha256(render_feed(rows, "").encode()).hexdigest()  # ignores the feed-level timestamp
    changed = get_meta(db, "feed_sha256") != digest
    log("feed", entries=len(rows), changed=changed, dry_run=dry_run)
    if dry_run or not changed:
        return
    try:
        push_gist(xml)
    except RuntimeError as e:
        log("gist_failed", error=str(e))
        return
    set_meta(db, "feed_sha256", digest)


# --- Inoreader side -------------------------------------------------------------------------------

def is_feed_item(item: dict) -> bool:
    """True for items Inoreader fetched from our gist feed."""
    url = os.environ.get("ACAD_FEED_URL")
    return bool(url) and item.get("origin", {}).get("streamId") == f"feed/{url}"


def match_items(db: sqlite3.Connection, items: list[dict], dry_run: bool) -> None:
    """Feed items carry no verdict of their own: find the stored paper by link, else by title (the feed
    prefixes titles with "[score] "), and queue it for labels. A dry run only logs the match: writing would
    take the paper out of the feed, and the advanced watermark never refetches the item."""
    for it in items:
        links = [x.get("href", "") for k in ("canonical", "alternate") for x in it.get(k) or []]
        title = re.sub(r"^\[\d+\] ", "", html_to_text(it.get("title", "")))
        hit = None
        for link in filter(None, links):
            hit = hit or db.execute("SELECT openalex_id FROM papers WHERE link = ?", (link,)).fetchone()
        hit = hit or db.execute("SELECT openalex_id FROM papers WHERE title = ? AND label_state != 'dup' "
                                "ORDER BY classified_at DESC", (title,)).fetchone()
        if not hit:
            log("acad_unmatched", title=title[:120], links=links[:2])
            continue
        log("acad_matched", title=title[:120], openalex_id=hit[0], dry_run=dry_run)
        if not dry_run:
            db.execute("UPDATE papers SET inoreader_id = ?, label_state = 'pending' "
                       "WHERE openalex_id = ? AND inoreader_id IS NULL", (it["id"], hit[0]))
    db.commit()


def label(c, db: sqlite3.Connection) -> None:
    apply_labels(c, db, "papers", TAGS, "inoreader_id")


# --- One run --------------------------------------------------------------------------------------

def run(ai: anthropic.Anthropic, db: sqlite3.Connection, *, dry_run: bool, effort: str,
        force_poll: bool = False) -> None:
    last = get_meta(db, "openalex_polled_at")
    if force_poll or not last or last <= now_iso(-timedelta(hours=POLL_HOURS)):
        if poll(db):  # a partial poll is retried on the next run
            set_meta(db, "openalex_polled_at", now_iso())
    else:
        log("poll_skipped", last=last, every_h=POLL_HOURS)
    score_unscored(ai, db, effort, dry_run)
    publish(db, dry_run)
