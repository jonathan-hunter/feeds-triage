"""One run of both pipelines: academic alerts, personal triage, then Inoreader labels for both.

Usage:
    uv run --env-file .env python run.py --dry-run            # score + log JSONL; no gist push, no labels
    uv run --env-file .env python run.py                      # the timer's run
    uv run --env-file .env python run.py --only academic      # no Inoreader token needed
    uv run --env-file .env python run.py --force-poll         # poll OpenAlex even if it ran < POLL_HOURS ago

The academic step runs first, and a failure there is re-raised only after the personal pipeline and
labelling have run, so one pipeline can't starve the other. State lives in state/state.sqlite.
"""

from __future__ import annotations

import argparse

import anthropic

from core import academic, personal
from core.common import log, open_db
from core.inoreader import Client

EFFORTS = ["low", "medium", "high"]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="score and log; no gist push, no edit-tag calls")
    p.add_argument("--only", choices=["academic", "personal"], help="run one pipeline")
    p.add_argument("--effort", choices=EFFORTS, default="low", help="personal pipeline (default low)")
    p.add_argument("--academic-effort", choices=EFFORTS, default="medium", help="academic pipeline (default medium)")
    p.add_argument("--force-poll", action="store_true", help=f"poll OpenAlex regardless of the {academic.POLL_HOURS} h gate")
    args = p.parse_args()

    db = open_db()
    ai = anthropic.Anthropic()
    failure = None
    if args.only != "personal":
        try:
            academic.run(ai, db, dry_run=args.dry_run, effort=args.academic_effort, force_poll=args.force_poll)
        except Exception as e:  # noqa: BLE001 - logged, then re-raised after the personal run
            log("academic_failed", error=repr(e)[:300])
            failure = e
    if args.only != "academic":
        c = Client.from_env()
        feed_items = personal.run(c, ai, db, dry_run=args.dry_run, effort=args.effort, skip=academic.is_feed_item)
        academic.match_items(db, feed_items, args.dry_run)
        if not args.dry_run:
            personal.label(c, db)
            academic.label(c, db)
        L = c.limits
        log("done", zone1=f"{L.zone1_usage}/{L.zone1_limit}", zone2=f"{L.zone2_usage}/{L.zone2_limit}",
            reset_after_s=L.reset_after_s)
    if failure:
        raise failure


if __name__ == "__main__":
    main()
