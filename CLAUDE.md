# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

feeds-triage: two Claude Haiku triage pipelines feeding Inoreader, run together every 2 h as a one-shot Docker Compose job on a Raspberry Pi 5 (arm64).
- **Personal:** scores unread Inoreader items against `rubrics/personal.md` and writes `LLM-*` labels.
- **Academic:** scores OpenAlex papers against `rubrics/academic.md` and publishes them as an Atom feed in a secret gist. Inoreader subscribes to that feed, and the papers get `ACAD-*` labels.

**Read `README.md` before writing code.** It has the verified Inoreader and OpenAlex constraints, the cost and budget reasoning, the deploy steps and a Roadmap checklist. Keep that checklist current.

This repository is public. Rubrics in `rubrics/` and deployment notes in `CLAUDE.local.md` are personal and gitignored, so never quote them in tracked files, commits or examples.

## Commands

```zsh
uv run --env-file .env python run.py --dry-run          # both pipelines, log JSONL; no gist push, no labels (see dry-run caveat)
uv run --env-file .env python run.py                    # the timer's run
uv run --env-file .env python run.py --only academic --force-poll   # academic only, ignoring the 20 h OpenAlex gate
.venv/bin/python -m tests.test_personal                 # offline checks, no API calls
.venv/bin/python -m tests.test_academic                 # offline checks of academic + the combined run
.venv/bin/python -m doctest core/common.py core/personal.py core/academic.py
# Needs INOREADER_CLIENT_ID, INOREADER_CLIENT_SECRET, INOREADER_REDIRECT_URI in the env
python3 -m core.inoreader auth           # OAuth bootstrap: interactive, paste the redirected URL back
python3 -m core.inoreader test           # read-only smoke test: spends ~4 Zone 1 requests
python3 -m core.inoreader test --write   # adds then removes two probe labels: 2 Zone 2 + 3 Zone 1
```

`.env` holds `ANTHROPIC_API_KEY`, the `INOREADER_*` variables, `OPENALEX_API_KEY`, `GITHUB_GIST_TOKEN`, `ACAD_GIST_ID` and `ACAD_FEED_URL`, which must be the gist's raw URL. Dependencies are managed with `uv` (`pyproject.toml`).

**Rubrics:** `rubrics/{personal,academic}.md` and `rubrics/academic_queries.toml` are the user's own and are gitignored. The tracked templates are in `rubrics/examples/`, and the offline tests run against them (`tests/__init__.py` sets `RUBRICS_DIR`). The schema enums come from the rubric: `personal.CATEGORIES` from the `- category:` line in `<output>`, and `academic.EXCLUSIONS` from the `` `slug`: `` bullets under `## Hard exclusions`. A change to the structure the code depends on must be made in the templates as well.

**Dry-run caveat:** on the live `state/state.sqlite`, a dry run stores new articles and papers as `dry-run`. Those rows are never labelled or published, and the watermark still advances. Test against a copy of `state/`, or use the offline tests.

## Architecture

- `run.py`: the entrypoint. It runs `academic.run`, then `personal.run(skip=academic.is_feed_item)`, which returns the academic feed's items. Next come `academic.match_items`, then `personal.label` and `academic.label`. An academic exception is logged, then re-raised after the personal pipeline and labelling have run.
- `core/common.py`: shared code.
  - `open_db()` owns the whole schema (`items`, `papers`, `meta`). `state/state.sqlite` is the calibration dataset, so don't change existing columns without a migration.
  - `parse_call()`: one structured-output call with the rubric as a cached system prompt, returning a verdict or None per item.
  - `retrying()`: if a whole call fails (truncated or invalid JSON, or a refusal of the batch), the batch is halved and retried. If only some items are refused or omitted, each one is retried on its own. A final None is stored with a NULL score and never retried.
  - `apply_labels(c, db, table, tag_map, id_col)`: one `edit-tag` per distinct SQL condition, so tags that share a condition (must + starred) go in one call. Rows stay `pending` until it succeeds.
  - `http()`: turns HTTP and network errors into `RuntimeError`.
- `core/inoreader.py`: stdlib-only API client and CLI. It is the API layer: extend it, don't rewrite it.
  - `edit_tag(add=, remove=)` takes a tag or a list of tags; a list repeats `a`/`r` in one call (confirmed by `test --write` on 2026-10-30).
  - `Client._request` refreshes the token automatically 5 minutes before expiry. It records the rate-limit headers in `Client.limits` and returns parsed JSON, or a raw string for non-JSON responses (`edit-tag` returns `OK`). HTTP and network errors raise `RuntimeError`, and callers treat them like a 429. A failed token refresh is not converted: it crashes the run on purpose, because it needs a human.
  - Refresh tokens **rotate**. Every refresh atomically rewrites the token file (`$INOREADER_TOKEN_FILE`, default `~/.config/inoreader-llm/tokens.json`, mode 0600). Two processes sharing one token file can invalidate each other's token, so never run them concurrently.
- `core/personal.py`: the personal pipeline. Its structured output (`Verdict`) must match `rubrics/personal.md`'s `<output>` fields: `id`, `reason`, `category`, `tier`, `score`, `wildcard`, `confidence`. Category values are an enum listed in that file.
  1. Fetch unread items newer than the watermark, oldest first, up to `MAX_PAGES` pages.
  2. Hand back items that `skip` claims, and skip IDs already in `items`.
  3. Classify in batches of `BATCH`, then `store()` each batch.
  4. Run `dedupe_must()`. `label()` writes tags from `TAGS`: four `LLM-*` labels, starred for scores of 9 or higher, and read for anything below 7. An `LLM-maybe` article keeps its tag, so it can still be browsed under that tag. Unscored (refused) and `LLM-wild` articles stay unread.
  - Each request also carries an `<already_scored>` list: items scored `SEEN_MIN` (7) or higher in the last 72 h (`SEEN_HOURS`), up to `SEEN_MAX`. At 5 the list ran to about 290 entries, and Haiku missed repeats inside it. This lets Haiku mark repeats of a story from another batch or run as `dup s<rowid>`. The rule that goes with it is in the rubric's Duplicates section. Inside a batch, Haiku writes `dup N` with the batch-local ID, and `store()` rewrites that to `dup s<rowid>` so that `dedupe_must` can follow it. Duplicate reasons must start with `dup <id>`; free-text reasons such as "Duplicate of …" can't be followed.
  - `dedupe_must()` sends Haiku the must-reads (score 9 or higher) from the last `SEEN_HOURS`, plus the first pass's `dup s<rowid>` entries that point at them, and gets back one `keep` per story, using `MUST_DEDUPE_PROMPT`. The creator's own post beats analysis, which beats news.
    - Losers drop to score 3 with the reason `dup s<keep> (must dedupe): …`. If they were already tagged, their must tag and star are removed in one call.
    - A winner that the first pass had marked as a duplicate is promoted to the group's score and marked unread again.
    - It calls the model only when a candidate is not yet `done`. Each extra call is another chance at a wrong, irreversible grouping. Rows it has already demoted are never offered again, so decisions can't flip-flop.
  - At most `DAILY_MAX` (2000) articles are classified in any rolling 24 h, oldest first. The watermark stops at the last one classified. Once the cap is reached, runs skip the fetch but still label.
  - The watermark advances per completed batch, which together with the ID check makes reruns idempotent. If the fetch fails (Zone 1 429 or network), the run still labels, because Zone 2 is a separate budget.
  - `items.label_state` is `pending`, `done` or `dry-run`.
- `core/academic.py`: the academic pipeline. It never touches the Inoreader token. `PaperVerdict` follows `rubrics/academic.md` (0–100).
  - `run()`: `poll()` at most every `POLL_HOURS` (20, via `meta.openalex_polled_at`; a partial poll isn't recorded), then `score_unscored()`, then `publish()`.
  - `poll()` re-reads a 30-day publication window per query in `rubrics/academic_queries.toml`, because `from_created_date` and `from_updated_date` are paid-only. Unseen works go in as `unscored`, or as `dup` by DOI or title key.
  - `publish()` renders `state/feed.xml` (score 40 or more, last 30 days) and `PATCH`es the gist only when the hash in `meta.feed_sha256` changes.
  - `is_feed_item()` matches `origin.streamId == feed/$ACAD_FEED_URL`. `match_items()` finds the paper by link, else by title without its `[score] ` prefix. In a dry run it only logs.
  - `papers.label_state` is `unscored`, `dup`, `dry-run`, `new` (in the feed, not yet seen in Inoreader), `pending` or `done`. Only `new`, `pending` and `done` rows are published.
- Deployment (reference: Raspberry Pi 5, arm64, Ubuntu Server), from `~/containers/RSS`:
  - **Do not rename anything the installed timer or live services depend on:** the compose service `classifier`, `systemd/rss-triage.{service,timer}`, `WorkingDirectory`, the `ACAD_FEED_URL`/stream ID, label names, the gist file name `feed.xml`, and the Atom feed/entry IDs.
  - `Dockerfile` copies `run.py`, `core/` and `rubrics/`, never `.env` or `state/`. `run.py` is the entrypoint, so `docker compose run --rm classifier --dry-run` passes flags straight through. The image is `feeds-triage`.
  - `compose.yaml` mounts `./state` at `/app/state`, which holds `tokens.json`, `state.sqlite` and `feed.xml`. It runs the container as `PUID:PGID` (default `1000:1000`) so the host user owns those files.
  - The systemd units are **user** units, with linger enabled. They run every 2 h, and the JSONL logs go to `journalctl --user -u rss-triage`.
  - After changing code, run `docker compose build` on the Pi. `uv.lock` pins the dependencies, and the Dockerfile pins uv.
  - Only one machine may hold the Inoreader token file; `CLAUDE.local.md` says which one.

## API budget

Inoreader Pro: Zone 1 (reads) and Zone 2 (writes) each allow **100 requests/day**, and every live run spends real quota.

- Make one `edit-tag` call per label condition per run, never one per item. `edit-tag` accepts many `i` params.
- HTTP 429 means the zone budget is spent. Stop cleanly and leave the remaining items for the next run.
- Don't run `test` in loops or use live calls to debug. Prefer the offline tests, a dry run against a copy of `state/`, or `py_compile`.

OpenAlex (free key): $1/day. A search costs $0.001, a list $0.0001, and a full poll about $0.04.

## Conventions

- Python 3.11+, modern typing, stdlib first. `anthropic` (2.x) and `pydantic` are the only non-stdlib dependencies.
- UK English and ISO-8601 dates.
- Be terse and technical, challenge weak assumptions, and leave venv and install boilerplate out of explanations.
- `.env` and `state/` must be gitignored, and secrets are never echoed.
