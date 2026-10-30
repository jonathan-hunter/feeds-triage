"""Personal triage: score unread Inoreader items against rubrics/personal.md (0-10) into the items table.

Run through run.py. Per run: <= MAX_PAGES Zone 1 calls, then label() makes <= 5 Zone 2 calls
(+2 on a must-dedupe takeover).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit

import anthropic
from pydantic import BaseModel

from core.common import (MODEL, READ_TAG, STAR_TAG, apply_labels, get_meta, html_to_text, insert, log,
                    now_iso, parse_call, read_rubric, retrying, rubric_slugs, set_meta)
from core.inoreader import Client

RUBRIC = read_rubric("personal.md")
CATEGORIES = rubric_slugs(RUBRIC, r"^- category: (.+)$")  # from the rubric's <output>
BATCH = 40
MAX_PAGES = 3
SUMMARY_CAP = 1500
FIRST_RUN_LOOKBACK_S = 24 * 3600
# ponytail: rolling 24 h cap on articles classified (~$0.15/day at 2000). A backlog that stays
# over the cap drifts the watermark back; Inoreader clamps oldest-first lookback to ~1 month.
DAILY_MAX = 2000
SEEN_HOURS = 72  # dedupe window: stories scored >= SEEN_MIN this recently are sent as <already_scored>
SEEN_MIN = 7  # LLM-week and up; at >= 5 the list ran ~290 entries and Haiku missed repeats in it
SEEN_MAX = 200  # ponytail: cap keeps the list ~5k tokens; raise if dupes slip through on busy days
MUST_TAG = "user/-/label/LLM-must"
TAGS = {  # Inoreader tag -> SQL condition on items; one edit-tag call per distinct condition per run
    MUST_TAG: "score >= 9",
    "user/-/label/LLM-week": "score BETWEEN 7 AND 8",
    "user/-/label/LLM-maybe": "score BETWEEN 5 AND 6",
    "user/-/label/LLM-wild": "wildcard >= 8",
    STAR_TAG: "score >= 9",
    # LLM-maybe and below (tag kept, so still browsable). NULL score (refused) and LLM-wild stay unread.
    READ_TAG: "score < 7 AND COALESCE(wildcard, 0) < 8",
}
MUST_DEDUPE_PROMPT = """\
You check one reader's must-read RSS items for duplicates. Group items that report the same story: \
the same release, launch, advisory, paper or event. Related but distinct stories are never grouped.
For each group of two or more, keep exactly one item. The creator's own communication (the vendor's or \
project's blog, release notes, changelog, system card, paper, or the advisory itself) beats technical \
analysis, which beats news coverage; among equals keep the most detailed. Judge from feed, domain, URL \
and title. Output only groups that drop at least one item; reason is <= 12 words on why keep wins."""
NO_RESULT = dict(reason="no result (refused or failed)", category=None, tier=None, score=None,
                 wildcard=None, confidence=None)


class Verdict(BaseModel):  # field order matches rubric <output>: reason before score
    id: int
    reason: str
    category: Literal[CATEGORIES]
    tier: Literal[1, 2, 3] | None
    score: int
    wildcard: int | None
    confidence: Literal["high", "low"]


class Verdicts(BaseModel):
    items: list[Verdict]


class MustGroup(BaseModel):
    reason: str
    keep: int
    drop: list[int]


class MustGroups(BaseModel):
    groups: list[MustGroup]


def prep(item: dict) -> dict:
    url = (item.get("canonical") or item.get("alternate") or [{}])[0].get("href", "")
    body = (item.get("summary") or item.get("content") or {}).get("content", "")
    return {
        "feed": item.get("origin", {}).get("title", ""),
        "domain": urlsplit(url).netloc.removeprefix("www."),
        "title": item.get("title", ""),
        "summary": html_to_text(body),
        "url": url,
    }


def classify(ai: anthropic.Anthropic, batch: list[dict], effort: str, seen: list[dict]) -> list[Verdict | None]:
    """One verdict per input, in order; None where the model refused or failed even singly."""
    prior = "\n".join(json.dumps(s, ensure_ascii=False) for s in seen)

    def call(sub: list[dict]) -> list[Verdict | None]:
        lines = [
            json.dumps({"id": i, "feed": p["feed"], "domain": p["domain"], "title": p["title"],
                        "summary": p["summary"][:SUMMARY_CAP]}, ensure_ascii=False)
            for i, p in enumerate(sub, 1)
        ]
        content = "<items>\n" + "\n".join(lines) + "\n</items>"
        if seen:
            content = f"<already_scored>\n{prior}\n</already_scored>\n{content}"
        return parse_call(ai, RUBRIC, content, effort, Verdicts, len(sub))

    return retrying(call, batch)


def recent_scored(db: sqlite3.Connection) -> list[dict]:
    """Stories already worth a label, so batches and runs can mark repeats as "dup s<rowid>"."""
    rows = db.execute(
        "SELECT rowid, title, score FROM items WHERE score >= ? AND classified_at >= ? "
        "ORDER BY rowid DESC LIMIT ?", (SEEN_MIN, now_iso(-timedelta(hours=SEEN_HOURS)), SEEN_MAX))
    return [{"id": f"s{r}", "title": t, "score": s} for r, t, s in rows]


def fetch(c: Client, since_usec: int) -> list[dict]:
    """Unread items newer than the watermark, oldest first, so a capped run resumes cleanly."""
    items, cont = [], None
    for _ in range(MAX_PAGES):
        page = c.stream_contents(n=100, newer_than=since_usec // 1_000_000, oldest_first=True,
                                 continuation=cont)
        items += page.get("items", [])
        if not (cont := page.get("continuation")):
            break
    return list({it["id"]: it for it in items}.values())  # an id repeated across pages would break the insert


def store(db: sqlite3.Connection, chunk: list[dict], preps: list[dict], verdicts: list[Verdict | None],
          effort: str, state: str) -> None:
    """Insert one scored batch, rewriting batch-local "dup N" to "dup s<rowid>" so dedupe_must can follow it."""
    now = now_iso()
    rowids = [insert(db, "items", {
        "id": it["id"], "ts_usec": int(it["timestampUsec"]), **{k: pr[k] for k in ("feed", "domain", "title", "url")},
        **(v.model_dump(exclude={"id"}) if v else NO_RESULT),
        "model": MODEL, "effort": effort, "classified_at": now, "label_state": state,
    }) for it, pr, v in zip(chunk, preps, verdicts)]
    for rowid, v in zip(rowids, verdicts):
        if v and (m := re.match(r"dup (\d+)\b", v.reason)) and 1 <= int(m[1]) <= len(rowids):
            db.execute("UPDATE items SET reason = ? WHERE rowid = ?",
                       (f"dup s{rowids[int(m[1]) - 1]}{v.reason[m.end():]}", rowid))
    db.commit()


def dedupe_must(ai: anthropic.Anthropic, c: Client, db: sqlite3.Connection, effort: str, dry_run: bool) -> None:
    """Second pass, every run: one must-read per story, preferring the creator's own post.

    Candidates are must-reads from the last SEEN_HOURS plus the first pass's "dup s<rowid>" of them,
    so an official post that arrived after a news rewrite (and was marked dup) can take over.
    Rows this pass already demoted are never offered again, so repeated runs can't flip-flop.
    ponytail: a takeover costs up to 2 extra Zone 2 calls.
    """
    must = "score >= 9 AND classified_at >= :cutoff"
    rows = db.execute(f"""
        SELECT rowid, id, feed, domain, url, title, score, reason, label_state FROM items
        WHERE ({must}) OR (reason LIKE 'dup s%' AND reason NOT LIKE '%(must dedupe)%' AND classified_at >= :cutoff
               AND CAST(substr(reason, 6) AS INTEGER) IN (SELECT rowid FROM items WHERE {must}))
        """, {"cutoff": now_iso(-timedelta(hours=SEEN_HOURS))}).fetchall()
    # Only re-judge when a candidate is new: re-asking the same question every run for SEEN_HOURS
    # gives the model ~36 chances at a wrong (and irreversible) grouping.
    if len(rows) < 2 or all(r[8] == "done" for r in rows):
        return
    by_rowid = {r[0]: r for r in rows}
    lines = [json.dumps({"id": r[0], "feed": r[2], "domain": r[3], "url": r[4], "title": r[5]},
                        ensure_ascii=False) for r in rows]
    try:
        resp = ai.messages.parse(
            model=MODEL, max_tokens=16000, system=MUST_DEDUPE_PROMPT,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            output_config={"effort": effort}, output_format=MustGroups,
        )
    except ValueError as e:
        log("must_dedupe_failed", error=str(e)[:200])
        return
    log("must_dedupe_call", n=len(rows), stop_reason=resp.stop_reason, input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens)
    if resp.stop_reason != "end_turn" or not resp.parsed_output:
        return

    keeps = {g.keep for g in resp.parsed_output.groups if g.keep in by_rowid}
    promote, demote = {}, {}  # rowid -> (new score, new reason)
    for g in resp.parsed_output.groups:
        drops = [d for d in g.drop if d in by_rowid and d not in keeps]
        if g.keep not in by_rowid or not drops:
            continue
        group_score = max(by_rowid[r][6] or 0 for r in [g.keep, *drops])
        if group_score < 9:  # no must-read in this group; nothing to fix
            continue
        if by_rowid[g.keep][6] < 9:
            promote[g.keep] = (group_score, f"official source over s{drops[0]}: {g.reason}")
        for d in drops:
            if by_rowid[d][6] >= 9:
                demote[d] = (3, f"dup s{g.keep} (must dedupe): {g.reason}")
    if not (promote or demote):
        return
    log("must_dedupe", dry_run=dry_run, promote=sorted(promote), demote=sorted(demote))
    if dry_run:
        return

    untag = [by_rowid[r][1] for r in demote if by_rowid[r][8] == "done"]  # already tagged must + starred
    unread = [by_rowid[r][1] for r in promote if by_rowid[r][8] == "done"]  # first pass marked it read
    try:
        if untag:
            c.edit_tag(untag, remove=[MUST_TAG, STAR_TAG])
        if unread:
            c.edit_tag(unread, remove=READ_TAG)
    except RuntimeError as e:
        log("must_dedupe_failed", error=str(e))
        return
    for rowid, (score, reason) in {**promote, **demote}.items():  # pending: label() re-tags them
        db.execute("UPDATE items SET score = ?, reason = ?, label_state = CASE label_state "
                   "WHEN 'dry-run' THEN 'dry-run' ELSE 'pending' END WHERE rowid = ?", (score, reason, rowid))
    db.commit()


def run(c: Client, ai: anthropic.Anthropic, db: sqlite3.Connection, *, dry_run: bool, effort: str,
        skip: Callable[[dict], bool]) -> list[dict]:
    """Fetch, score and store new unread items. Returns the fetched items `skip` claimed (the academic
    feed's), which are neither scored nor stored here. Labels are written separately, by label()."""
    since = int(get_meta(db, "watermark_usec") or (time.time() - FIRST_RUN_LOOKBACK_S) * 1_000_000)
    day_ago = now_iso(-timedelta(hours=24))
    budget = DAILY_MAX - db.execute("SELECT COUNT(*) FROM items WHERE classified_at >= ?", (day_ago,)).fetchone()[0]
    items = []
    if budget <= 0:  # skip the fetch too, saving Zone 1; pending labels still go out
        log("daily_cap", limit=DAILY_MAX)
    else:
        try:
            items = fetch(c, since)
        except RuntimeError as e:  # HTTP 429 (Zone 1 spent) or network; Zone 2 is separate, so still label
            log("fetch_failed", error=str(e))
    skipped = [it for it in items if skip(it)]
    skip_ids = {it["id"] for it in skipped}
    new = [it for it in items if it["id"] not in skip_ids
           and not db.execute("SELECT 1 FROM items WHERE id = ?", (it["id"],)).fetchone()]
    capped = len(new) > budget
    new = new[:max(budget, 0)]  # oldest first, so the watermark resumes at the first skipped item
    preps = [prep(it) for it in new]
    log("fetched", items=len(items), new=len(new), skipped=len(skipped), capped=capped, since_usec=since,
        truncated=sum(len(x["summary"]) > SUMMARY_CAP for x in preps))

    state = "dry-run" if dry_run else "pending"
    complete = True
    for start in range(0, len(new), BATCH):
        chunk, cprep = new[start:start + BATCH], preps[start:start + BATCH]
        try:
            verdicts = classify(ai, cprep, effort, recent_scored(db))
        except anthropic.APIError as e:  # SDK already retried; leave the rest for the next run
            log("classify_failed", error=str(e)[:300])
            complete = False
            break
        store(db, chunk, cprep, verdicts, effort, state)
        set_meta(db, "watermark_usec", str(max(int(it["timestampUsec"]) for it in chunk)))
    if complete and items and not capped:
        set_meta(db, "watermark_usec", str(max(int(it["timestampUsec"]) for it in items)))

    try:
        dedupe_must(ai, c, db, effort, dry_run)
    except anthropic.APIError as e:  # optional pass: a failure must not block labelling
        log("must_dedupe_failed", error=str(e)[:300])
    return skipped


def label(c: Client, db: sqlite3.Connection) -> None:
    apply_labels(c, db, "items", TAGS, "id")
