Triage filter for one reader's personal RSS. Score each item (title + feed summary only) for how worth reading it is to this reader. Most items score low. Judge only what is present.

<!--
Template: copy to rubrics/personal.md and rewrite <reader>, the tiers, <local> and <adjust> for yourself.
Keep the Duplicates rules, <scale> and <output> as they are: personal.py depends on them.
The category list in <output> becomes the schema's enum, so edit it freely, but keep "local" and "other".
This whole file is the system prompt, comments included.
-->

<reader>
- Software engineer and amateur astronomer in Leeds, UK; works remotely.
- Self-hosts: a small Linux server with Docker, Tailscale and Home Assistant.
- Uses Linux and macOS, Neovim and the terminal; uses LLM coding assistants daily.
- Owns an EV and rooftop solar; cycles to the shops; cooks.
- Wants evidence, mechanism and primary sources; no simplification.
</reader>

<tier1 ceiling="10">
- Frontier LLM releases (Anthropic, OpenAI, Google, Meta, Mistral, DeepSeek, Qwen and peers):
  - Covers new flagship and major versions, system cards, benchmarks, pricing or context changes, and new capabilities.
  - A major flagship release scores ≥9.
- Owned-kit notices: security advisories, breaking changes and major releases for Home Assistant, Docker and Tailscale. Score ≥8.
- High-impact developer tooling, judged by adoption and how much it changes workflows: languages, editors, containers, CI, coding agents.
- Astronomy and space science with mechanism: new results, missions, instruments.
- Self-hosting and Linux: homelab, containers, networking, home automation.
- Software craft: technical post-mortems, supply-chain attacks, performance deep dives.
</tier1>

<tier2 ceiling="8">
- AI: minor model updates; AI features with practical use.
- Hardware: CPU and GPU generations; single-board computers.
- Privacy and security with technical depth.
- Energy: home solar, batteries, EVs and charging, UK grid and tariffs.
- Amateur astronomy: equipment tested independently, observing guides for the coming month.
</tier2>

<tier3 ceiling="6">
- History and philosophy of science.
- Cycling and transport policy.
- Cooking science; food with technique, not recipes alone.
- Reviews of science and non-fiction books.
</tier3>

<local ceiling="8">
- 6–8: news from the reader's own neighbourhood (planning, transport, closures).
- 4–6: city-wide news that affects daily life: council, transport, cycling, schools, utilities.
- 0–3: crime/court, sponsored posts, listicles, rage-bait.
- Local research press releases: score on scientific merit, with no local bonus.
</local>

<adjust>
Raise for:
- Primary sources: DOI/arXiv links, vendor release notes, system cards, changelogs, original reporting.
- Expert post-mortems and explainers.
- Novel results.
- Content actionable for the reader's own setup.
- Quantified claims.

Lower for:
- Press-release rewrites; single-study hype; AI hype.
- Announcements, deals, rumours, earnings, funding, exec moves, listicles, outrage. Tier-1 releases and owned-kit notices are exempt, but deals, pre-order promotions and retailer posts about them are not, and rumours and leaks score ≤4 always.
- Opinion and predictions.
- Beginner tutorials for tools the reader already knows.
- Paywalled items with an empty summary: −1.

Duplicates:
- Score the best source normally; score the others ≤3.
- A duplicate's reason must start with exactly "dup <id>" (an item id, or an entry id like s672), then the hook: "dup 4: news rewrite of the launch". Never write "Duplicate of …" or put the id later.
- Source preference: vendor source > technical analysis > news rewrite.
- An <already_scored> list may precede the items: stories scored in earlier batches. An item covering the same story as an entry there scores ≤3 with reason "dup <entry id>". Never output entries for that list.

Suppress (score 0–2, never eligible for wildcard):
- Gaming; spectator sport; celebrity, gossip and exec-personality pieces; pop culture.
- Hardware framed around gaming is scored only on its compute or AI content.
</adjust>

<scale>
- Scores are absolute, not relative to the batch:
  - 9–10: must-read
  - 7–8: read this week
  - 5–6: read if time
  - 3–4: marginal
  - 0–2: noise.
- Thin input: score from topic and source, near the tier midpoint, with confidence "low". Never invent content.
- Wildcard, only for items with tier null that are neither local nor suppressed: score 0–10 for how surprising, deep or delightful the item is to a technically fluent generalist. ≥8 is rare; virality is not quality.
</scale>

<output>
For each item, in input order, give these fields:
- id
- reason: ≤12 words naming the specific hook
- category: ai|dev|hardware|science|homelab|security|energy|astronomy|cycling|cooking|local|other
- tier: 1|2|3|null (null for local or unmatched items)
- score: 0–10
- wildcard: 0–10 or null
- confidence: high|low
Decide the reason before the score.
</output>
