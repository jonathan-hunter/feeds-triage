"""Shared by both pipelines: logging, the state database, model calls with retry, HTTP and Inoreader labels."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import anthropic

ROOT = Path(__file__).parent.parent  # project root
RUBRICS = Path(os.environ.get("RUBRICS_DIR", ROOT / "rubrics"))  # your own rubrics; templates are in rubrics/examples/
DB_PATH = ROOT / "state" / "state.sqlite"
MODEL = "claude-haiku-5-5"
STAR_TAG = "user/-/state/com.google/starred"
READ_TAG = "user/-/state/com.google/read"


def log(event: str, **fields) -> None:
    print(json.dumps({"ts": now_iso(), "event": event, **fields}), flush=True)


def now_iso(delta: timedelta = timedelta(0)) -> str:
    return (datetime.now(UTC) + delta).isoformat(timespec="seconds")


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """
    >>> html_to_text("<p>Fish &amp; <b>chips</b></p>\\n<p>today</p>")
    'Fish & chips today'
    """
    p = _Text()
    p.feed(html)
    return " ".join(" ".join(p.parts).split())


def read_rubric(name: str) -> str:
    path = RUBRICS / name
    if not path.exists():
        raise SystemExit(f"{path} not found: copy rubrics/examples/{name} there and edit it")
    return path.read_text()


def rubric_slugs(text: str, pattern: str) -> tuple[str, ...]:
    """Enum values a rubric defines, so the verdict schema follows the rubric rather than the code.

    >>> rubric_slugs("- category: ai|dev|other", r"^- category: (.+)$")
    ('ai', 'dev', 'other')
    >>> rubric_slugs("- `press-release`: x", r"^- `([a-z-]+)`:")
    ('press-release',)
    """
    found = tuple(v for m in re.findall(pattern, text, re.M) for v in m.split("|"))
    if not found:
        raise SystemExit(f"rubric has no line matching {pattern!r}")
    return found


def open_db() -> sqlite3.Connection:
    """state/state.sqlite is the calibration dataset: don't change existing columns without a migration."""
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS items (  -- personal.py; scores are 0-10
            id TEXT PRIMARY KEY, ts_usec INTEGER, feed TEXT, domain TEXT, title TEXT, url TEXT,
            reason TEXT, category TEXT, tier INTEGER, score INTEGER, wildcard INTEGER, confidence TEXT,
            model TEXT, effort TEXT, classified_at TEXT,
            label_state TEXT  -- pending | done | dry-run
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS papers (  -- academic.py; scores are 0-100
            openalex_id TEXT PRIMARY KEY, doi TEXT, link TEXT, title TEXT, title_key TEXT, venue TEXT,
            pub_date TEXT, type TEXT, authors TEXT, institutions TEXT, abstract TEXT, queries TEXT,
            rationale TEXT, tier TEXT, focus INTEGER, exclusion TEXT, score INTEGER, confidence TEXT,
            model TEXT, effort TEXT, classified_at TEXT,
            inoreader_id TEXT,
            label_state TEXT  -- unscored | dup | dry-run | new (in the feed, not yet in Inoreader) | pending | done
        );
        CREATE INDEX IF NOT EXISTS papers_doi ON papers (doi);
        CREATE INDEX IF NOT EXISTS papers_title_key ON papers (title_key);
        CREATE INDEX IF NOT EXISTS papers_link ON papers (link);
        CREATE INDEX IF NOT EXISTS papers_label_state ON papers (label_state);
    """)
    return db


def get_meta(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
    db.commit()


def insert(db: sqlite3.Connection, table: str, rec: dict) -> int:
    sql = f"INSERT INTO {table} ({', '.join(rec)}) VALUES ({', '.join(':' + k for k in rec)})"
    return db.execute(sql, rec).lastrowid


def http(req: urllib.request.Request, what: str) -> tuple[bytes, dict]:
    """Body and headers; HTTP and network errors (DNS, reset, timeout) raise RuntimeError."""
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{what} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    except OSError as e:
        raise RuntimeError(f"{what} -> {e}") from e


def retrying(call, batch: list) -> list:
    """call(batch) -> one result or None per input, in order. A whole failed call is halved and retried;
    otherwise only the items it missed are retried, singly. None where even the single call failed."""
    out = call(batch)
    missing = [i for i, v in enumerate(out) if v is None]
    if len(batch) > 1 and len(missing) == len(batch):  # whole call failed: halve, not len(batch) singles
        mid = len(batch) // 2
        return retrying(call, batch[:mid]) + retrying(call, batch[mid:])
    if len(batch) > 1:  # a refusal or omission sinks only the items it hit
        for i in missing:
            out[i] = call([batch[i]])[0]
    return out


def parse_call(ai: anthropic.Anthropic, system: str, content: str, effort: str, fmt, n: int) -> list:
    """One structured-output call over n items with 1-based ids; None for each item it didn't return.
    `fmt` must have an `items` list whose entries carry `id`."""
    out = [None] * n
    try:
        resp = ai.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort},
            output_format=fmt,
        )
    except ValueError as e:  # truncated / schema-invalid JSON
        log("parse_failed", n=n, error=str(e)[:200])
        return out
    u = resp.usage
    log("model_call", n=n, stop_reason=resp.stop_reason, input_tokens=u.input_tokens,
        output_tokens=u.output_tokens, cache_read=u.cache_read_input_tokens,
        refusal_category=getattr(resp.stop_details, "category", None))
    if resp.stop_reason == "end_turn" and resp.parsed_output:
        for v in resp.parsed_output.items:
            if 1 <= v.id <= n:
                out[v.id - 1] = v
    return out


def apply_labels(c, db: sqlite3.Connection, table: str, tag_map: dict[str, str], id_col: str) -> None:
    """One edit-tag call per distinct condition, for rows still pending. Retries on the next run if
    Zone 2 is exhausted: rows stay pending, and edit-tag add is idempotent."""
    by_cond: dict[str, list[str]] = {}  # tags sharing a condition (must + starred) go in one call
    for tag, cond in tag_map.items():
        by_cond.setdefault(cond, []).append(tag)
    try:
        for cond, tags in by_cond.items():
            ids = [r[0] for r in db.execute(f"SELECT {id_col} FROM {table} WHERE label_state = 'pending' AND {cond}")]
            if ids:
                c.edit_tag(ids, add=tags)
                log("tagged", tag="+".join(t.rsplit("/", 1)[-1] for t in tags), n=len(ids))
    except RuntimeError as e:
        log("label_failed", table=table, error=str(e))
        return
    db.execute(f"UPDATE {table} SET label_state = 'done' WHERE label_state = 'pending'")
    db.commit()
