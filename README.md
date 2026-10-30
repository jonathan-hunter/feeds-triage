# feeds-triage

Two Claude Haiku triage pipelines that write their results into Inoreader. One run of both happens every 2 h, as a one-shot Docker Compose job started by a systemd user timer. The reference deployment is a Raspberry Pi 5 (Ubuntu Server, arm64), but any Docker host works.

What you judge as worth reading lives in plain-Markdown rubrics, not in code. The repository ships example rubrics for two fictional readers; you write your own (see [Rubrics](#rubrics)).

**You need:**
- an [Inoreader](https://www.inoreader.com) **Pro** account (API access) and a registered API app;
- an [Anthropic API](https://console.anthropic.com) key: Haiku costs cents a day here;
- for the academic pipeline, a free [OpenAlex](https://openalex.org) API key and a GitHub token with the `gist` scope.

- **Personal:** scores unread Inoreader articles against [`rubrics/personal.md`](rubrics/personal.md) (0–10) and labels them `LLM-*`.
- **Academic:** polls OpenAlex for new papers, scores them against [`rubrics/academic.md`](rubrics/academic.md) (0–100), and publishes those scoring 40 or more as an Atom feed in a secret GitHub gist. Inoreader subscribes to that feed, and the next run labels the papers `ACAD-*`.

Each run (`run.py`):

1. **Academic:** poll OpenAlex (at most every 20 h), score any unscored papers, and push the feed to the gist if it changed.
2. **Personal:** fetch unread articles newer than the watermark, oldest first. Hand items from the academic feed back to step 3, and score the rest in batches of 40. Then pick one must-read per story when several sources cover it.
3. **Academic:** match the feed items to their stored papers.
4. **Labels:** write the Inoreader labels for both.

A failure in the academic pipeline is logged, then raised once the personal pipeline and labelling have run, so systemd still records it. Every result is stored in `state/state.sqlite`, which serves as the calibration dataset.

## Labels

| Personal score (0–10) | Inoreader result |
|---|---|
| 9–10 | `LLM-must` and starred |
| 7–8 | `LLM-week` |
| 5–6 | `LLM-maybe`, marked read |
| 0–4 | marked read |
| wildcard ≥ 8 | `LLM-wild`, left unread |
| no score (refused) | left unread |

A wildcard score rates how surprising or delightful an article that matches no tier is; the rubric's `<scale>` section defines it. Repeat coverage of a story scores 3 or less, with a reason of `dup <id>`.

| Academic score (0–100) | Inoreader result |
|---|---|
| 80–100 | `ACAD-core` and starred |
| 60–79 | `ACAD-read` |
| 40–59 | `ACAD-maybe` |
| below 40 | not published |

## Rubrics

Your rubrics are the system prompts, used verbatim, and they're gitignored: copy the templates and rewrite them.

```sh
cp rubrics/examples/* rubrics/
```

| File | What to change | What the code depends on |
|---|---|---|
| `personal.md` | `<reader>`, the tier lists, `<local>`, `<adjust>`, and the `category:` values | the Duplicates rules; `<scale>`; `<output>`'s fields, whose `category:` values become the schema's enum (keep `local` and `other`) |
| `academic.md` | the reader, topics, modifiers, current focus and calibration anchors | tiers core/adjacent/peripheral/out; a Current focus section; `## Hard exclusions` bullets that start with a `` `slug`: ``, which become the `exclusion` enum; a 0–100 scale |
| `academic_queries.toml` | the OpenAlex queries for your field | `window_days`, `types`, and `[[query]]` entries with a `name` plus `sources`, `search` or `title` |

Describe the reader as a concrete person, with their kit and goals, and anchor the scale with scored examples. Set `RUBRICS_DIR` to keep your rubrics elsewhere, such as a private repository; the offline tests use `rubrics/examples/`.

Both pipelines store every verdict in `state/state.sqlite`, so after a week or two you can query the scores, find the misses, and tighten the rubric.

## Layout

- `run.py`: the entrypoint, one run of both pipelines.
  - `--dry-run`: score and log; no gist push and no labels.
  - `--only academic|personal`: run one pipeline. `--only academic` needs no Inoreader token.
  - `--effort` (personal, default `low`) and `--academic-effort` (default `medium`).
  - `--force-poll`: ignore the 20 h OpenAlex gate.
- `core/`:
  - `common.py`: logging, the SQLite schema (`open_db`), structured-output calls with halve-and-retry (`parse_call`, `retrying`), grouped `edit-tag` labelling (`apply_labels`), and HTTP error handling.
  - `personal.py`: the personal pipeline (`run`, `label`).
  - `academic.py`: the academic pipeline (`run`, `is_feed_item`, `match_items`, `label`).
  - `inoreader.py`: the Inoreader OAuth2 client and CLI, standard library only.
    - `python -m core.inoreader auth`: one-off authorisation. Open the printed URL, then paste the redirected URL back.
    - `python -m core.inoreader test [--write]`: smoke test. `--write` also adds and removes two probe labels in one call each.
- `rubrics/`: your rubrics (gitignored; see [Rubrics](#rubrics)).
  - `personal.md`, `academic.md`: the system prompts, used verbatim. Each must match its pipeline's verdict schema (`Verdict` and `PaperVerdict`).
  - `academic_queries.toml`: the OpenAlex queries.
  - `examples/`: the templates.
- `tests/`: offline checks, no API calls.
- `Dockerfile`, `compose.yaml`, `systemd/`: deployment. The compose service and the systemd units keep their original names (`classifier`, `rss-triage`), because the installed timer refers to them.

## Setup

Copy the rubric templates and edit them (see [Rubrics](#rubrics)). Register an Inoreader app at https://www.inoreader.com/developers with a redirect URI such as `http://localhost:8765/callback`, then create `.env` (`chmod 600`, gitignored):

```sh
ANTHROPIC_API_KEY=...
INOREADER_CLIENT_ID=...
INOREADER_CLIENT_SECRET=...
INOREADER_REDIRECT_URI=http://localhost:8765/callback  # used only by `auth`; must match the app registration
OPENALEX_API_KEY=...
GITHUB_GIST_TOKEN=...      # classic PAT, `gist` scope only
ACAD_GIST_ID=...
ACAD_FEED_URL=https://gist.githubusercontent.com/<user>/<gist_id>/raw/feed.xml   # the raw URL, not the gist page
```

Authorise Inoreader once. Refresh tokens rotate on every refresh, so **only one machine may hold the token file**. After you move it, stop running against it anywhere else.

```zsh
INOREADER_TOKEN_FILE=state/tokens.json uv run --env-file .env python -m core.inoreader auth
```

For the academic feed, create a secret gist containing a file named `feed.xml`, and subscribe to `ACAD_FEED_URL` in the Inoreader web UI.

Run locally:

```zsh
uv run --env-file .env python run.py --dry-run   # score + log JSONL; no gist push, no labels
uv run --env-file .env python run.py             # the timer's run
.venv/bin/python -m tests.test_personal && .venv/bin/python -m tests.test_academic
.venv/bin/python -m doctest core/common.py core/personal.py core/academic.py
```

When running locally, set `INOREADER_TOKEN_FILE` as in the auth example. Otherwise the client reads `~/.config/inoreader-llm/tokens.json`.

**Dry runs are not free on the live database.** They store new articles and papers as `dry-run`, those rows are never labelled or published, and the watermark still advances. Use a copy of `state/` to try changes. To release dry-run papers, run `UPDATE papers SET label_state = 'new' WHERE label_state = 'dry-run'`. A dry run's feed matching (`match_items`) only logs.

## Deploy

The host (a Raspberry Pi 5 here) needs Docker and the Compose plugin, and your user must be in the `docker` group. `systemd/rss-triage.service` expects the checkout at `~/containers/RSS`; edit its `WorkingDirectory` if you clone elsewhere.

```sh
git clone https://github.com/jonathan-hunter/feeds-triage.git ~/containers/RSS && cd ~/containers/RSS
cp rubrics/examples/* rubrics/   # then edit them
# add .env, and copy state/tokens.json from the machine that ran `auth`
docker compose build

mkdir -p ~/.config/systemd/user && cp systemd/rss-triage.* ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now rss-triage.timer
sudo loginctl enable-linger "$USER"
journalctl --user -u rss-triage -f   # JSONL logs
```

The container runs as `PUID:PGID` (by default `1000:1000`, and the service passes your own IDs), so `state/` stays owned by you. After changing code, run `docker compose build` again. `run.py`'s flags pass straight through, as in `docker compose run --rm classifier --only academic --force-poll`.

## Limits and cost

Verified 2026-10-29. Inoreader **Pro** is required for API access.

| Item | Value |
|---|---|
| Zone 1 (reads: stream/contents, subscription/list, tag/list, user-info) | **100 requests/day** |
| Zone 2 (writes: edit-tag, mark-all-as-read, subscription edits) | **100 requests/day** |
| Reset | `X-Reader-Limits-Reset-After` gives the seconds until reset. Usage headers: `X-Reader-Zone{1,2}-{Limit,Usage}` |
| Auth | OAuth2 auth code: `/oauth2/auth`, `/oauth2/token`, scope `read write`, `Authorization: Bearer` |
| stream/contents | `n` ≤ 100; `xt=user/-/state/com.google/read` (unread only); `ot` (newer than; with `r=o`, the lookback is clamped to about 1 month); `c` (continuation) |
| edit-tag | POST; `i` repeats (many items per call); `a`/`r` take tags like `user/-/label/Name`, several per call (confirmed 2026-10-30). The response body is `OK` |

- **Per run:**
  - Reads: up to 3 (fetch pages). Academic items arrive in the same fetch.
  - Writes: one `edit-tag` per distinct label condition that has articles, so up to 5 personal (`LLM-must` and starred share one) and up to 3 academic. Add up to 2 when the must-read dedupe swaps which item wins.
  - Typical totals at 12 runs a day: about 36 reads and about 45 writes.
  - A 429 is handled cleanly: rows stay `pending` and are labelled on the next run. Hourly runs would push writes to the limit.
- **Daily cap:** at most 2,000 personal articles are scored in any rolling 24 h; the rest wait for a later run.
- **Model:** `claude-haiku-5-5` ($0.10/$0.50 per MTok), with structured outputs and the rubric as a cached system prompt.
  - About 100 articles per run costs well under £0.01, and the 2,000-article daily cap is about $0.15. At that cost, the 50%-off Message Batches API isn't worth its extra complexity.
  - Haiku 5.5 doesn't support prefill or a non-default `temperature`/`top_p`.
  - Refusals have no server-side fallback. A refused batch is split and retried, and an item that still fails is stored unscored and never retried.
- **OpenAlex** (free key): a search costs $0.001 and a list $0.0001, against $1 a day. A poll re-reads the whole 30-day window for about $0.04, so polls are gated to every 20 h.

## Academic pipeline

```
run.py, every 2 h
  academic: poll (<= every 20 h): rubrics/academic_queries.toml -> OpenAlex /works -> papers (unscored | dup)
            score unscored -> Haiku + rubrics/academic.md -> papers (new)
            state/feed.xml (score >= 40) -> PATCH gist when changed
  personal: unread fetch -> items from feed/$ACAD_FEED_URL are handed back, not scored
  academic: match_items: by link, else title -> papers (pending) -> ACAD-* labels
Inoreader polls the gist's raw URL on its own schedule.
```

1. **Poll.** For each query in `academic_queries.toml`, `GET /works` with these filters:
   - `from_publication_date` = today − `window_days` (30) and `to_publication_date` = today + 7, which cuts records dated a year ahead;
   - `type` in `types`, which drops Zenodo and Mendeley datasets;
   - plus the query's `sources`, `search` (`title_and_abstract.search`) or `title` (`title.search`).

   It uses cursor paging, up to 5 pages per query. A poll in which any query fails isn't recorded, so the next run polls again.
   - The right "new since" filters (`from_created_date`, `from_updated_date`) need a paid plan.
   - A paper that OpenAlex indexes more than 30 days after its publication date is missed.
2. **Deduplicate** by OpenAlex ID, then DOI, then normalised title. Titles under 6 words also need the first author's surname to match. This catches preprint and published pairs that OpenAlex hasn't merged, even when their author order differs. A repeat is stored as `dup` with reason `dup <openalex id>`.
3. **Score** rows in `unscored` in batches of 20, at most 500 a run.
   - The model gets the title, venue, date, type, the first 10 authors, the distinct institutions and the abstract (rebuilt from `abstract_inverted_index` and capped at 3,000 characters).
   - Structured output `PaperVerdict`: `rationale`, `tier`, `focus`, `exclusion`, `score` and `confidence`.
   - An API error leaves the rest `unscored` for the next run. A refusal is stored with a NULL score and never retried.
4. **Publish.** Write `state/feed.xml` from papers scoring 40 or more and classified in the last 30 days, at most 200 entries.
   - Entry `id`: the OpenAlex URL. `link`: the DOI, falling back to the landing page.
   - Title: `[82] Title`.
   - Content: venue, date, authors, institutions, score, tier and rationale, then the abstract.

   Then `PATCH` the gist, unless the feed is unchanged since the last push. Every push sends the whole file, so the next run retries a failed one.

**Measured cost** (2026-10-30, the 30-day backlog): OpenAlex returned 290 works for about $0.05. Haiku scored 286 papers in 15 calls (213k input and 39k output tokens) for about $0.04. Daily volume after the backlog is about 10 papers.

### Still to check

- **Inoreader:** that it accepts the gist's `text/plain` content type, and that feed items match. Look for `acad_matched` and `acad_unmatched` in `journalctl --user -u rss-triage`.
- **Indexing lag:** after a month, compare `pub_date` with `classified_at` to see whether 30 days is wide enough.
- **Calibration:** after two weeks, review the score distribution and the false positives in `papers`, then tighten noisy queries or the rubric.

## Roadmap

- [ ] Academic pipeline: confirm Inoreader accepts the gist feed and that its items match (see Still to check).
- [ ] Academic pipeline: calibrate against two weeks of stored scores.
- [ ] Compare `low` and `medium` effort on a sample of stored articles.

## Sources

- https://inoreader.com/developers (Pro requirement, method index)
- https://www.inoreader.com/developers/oauth
- https://www.inoreader.com/developers/rate-limiting
- https://www.inoreader.com/developers/stream-contents
- https://www.inoreader.com/developers/edit-tag
- https://help.openalex.org/how-to-use-the-api/rate-limits-and-authentication
- https://docs.github.com/en/rest/gists/gists#update-a-gist

## Licence

MIT; see [LICENSE](LICENSE).
