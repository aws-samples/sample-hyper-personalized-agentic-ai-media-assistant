# Unified viewer profile — detailed design

Status: Sections 5-7 match what this repository builds (L1 behaviour, L2 in the stream processor, time-of-day taste). Other sections describe the target design; parts not built are marked **Not built** or **Deferred**.
Companion diagram: [`unified-profile-architecture.svg`](unified-profile-architecture.svg)

This document specifies how to build the unified viewer profile on top of the existing hyper-personalized media assistant (movie agent v2, voice agent, live sports agent, micro drama feed). It is written so an engineer can stand up the same solution in a different AWS account with the profile included. Where this design changes existing code or data, it says so explicitly.

---

## 1. Goals and non-goals

### Goals

1. One profile per viewer that every experience reads from: the recommendation agent, the micro drama / short-form feed, the live sports experience, and the standard home rows.
2. Learn from both what viewers **do** (playback, clips, live, agent interactions) and what they **say** (AgentCore Memory).
3. Keep the agent's prompt context compact, consistent, and deterministic, with no extra LLM call to build it.
4. Separate user-owned rules (declared preferences) from learned guesses, and enforce rules as hard filters everywhere.
5. Add Amazon Personalize as the standard ML recommender for non-conversational surfaces and as a reranker for the agent.
6. Support a **booth mode** in which persona profiles stay stable while sessions still adapt live.

### Non-goals

- A stored, LLM-synthesized "inferred profile" (themes, lifecycle, reconciliation). The inferred view is composed per session (Section 6). A stored version is a later extension (Section 22).
- Browse/search surfaces. The agent replaces search; home-row browse feedback is out of scope.
- Training a custom foundation model. Amazon Personalize provides the behavioral model.
- Multi-Region deployment.

---

## 2. Current state (what exists today)

| Area | Today | Location |
|---|---|---|
| Profile store | DynamoDB `movie-agent-user-profiles`, PK `user_id` = `{cognito_user}-{profile_slug}` (e.g. `demo-user-sports_fan`), SK `data_type` | `infra/layers/shared/python/ibc_common.py` (`build_pk`) |
| Profile items | `PREFERENCES` (seeded), `WATCH_HISTORY` (seeded list of title IDs), `top_picks`, `carousel_*`, `watch_list` | `backend/movie-ai-agent/mock_data/seed_*.py` (v1 agent, since removed), `infra/lambdas/*` |
| Agent profile read | `user_profile.get_profile_block()` reads `PREFERENCES` once per session | `backend/movie-ai-agent-v2/user_profile.py` |
| Conversational memory | AgentCore Memory; STM via `create_event`; custom User Preference strategy `TastePreferences` at `/preferences/{actorId}` (Claude Haiku 4.5 extraction/consolidation, single-subject records) | `backend/movie-ai-agent-v2/memory.py`, `setup_ltm_strategy.py` |
| Context assembly | `_combined_preferences()` concatenates the DynamoDB block and the LTM block; passed to `rewrite_query` and `reduce` as soft guidance | `backend/movie-ai-agent-v2/agent_v2.py` |
| Session state | In-process per runtime session: `_turns`, `_recommended_ids` (last batch, `MAX_EXCLUDE_IDS=5`), `_last_recommendations` | `memory.py`, `session.py`, `main.py` (`_sessions`) |
| Retrieval | OpenSearch Serverless index of titles with Nova 2 multimodal embeddings (1024-d), hybrid kNN + BM25 with RRF (`RRF_K=60`); prompt-cached shard fan-out | `retrieval.py`, `config.py` |
| Rows | `get_carousels` / `get_top_picks` Lambdas read seeded `carousel_*` / `top_picks` items | `infra/lambdas/` |
| Micro drama feed ordering | Hard-coded per persona in the frontend | `frontend/src/lib/clipsClient.ts` |
| Sports favorite player | Hard-coded `FAVORITE_PLAYER` | `frontend/src/data/sports.ts` |
| Behavioral events | None. No playback/clip events are sent to the backend | — |

---

## 3. Architecture overview

See [`unified-profile-architecture.svg`](unified-profile-architecture.svg). Components, left to right:

1. **Interaction surfaces**: long-form player, short-form feed (micro dramas and highlights), live sports, and the recommendation agent (conversational surface; `actorId = profileId`).
2. **Behavioral pipeline ("what users do")**: content graph and catalog (OpenSearch, Nova embeddings), unified event stream (Kinesis Data Streams), event log (S3 via Firehose), stream processor (Lambda).
3. **Stored profile**: L1 session state, L2 affinities, L3 declared preferences.
4. **AgentCore Memory ("what users say")**: short-term memory, long-term strategies, actor-scoped namespaces.
5. **Settings API**: the only writer of L3 declared.
6. **Agentic path**: agent retrieval tool (OpenSearch hybrid + L2 taste centroids → Personalize rerank → fusion) and the Profile Context module, which composes the L3 inferred view and the text-only context pack.
7. **Standard ML path**: Amazon Personalize (User-Personalization-v2, Personalized-Ranking-v2), standard recommendations (rows, short-form feed, post-play), recommendation cache.

Design rule: **the stored profile is what we know; Personalize and OpenSearch are how we use it.** Both paths learn from the same event stream and obey the same declared limits.

---

## 4. Identity

| Concept | Value | Notes |
|---|---|---|
| `account_id` | Cognito `sub` (or username) of the signed-in account | Household level |
| `profile_id` | `{account_user}-{profile_slug}`, e.g. `demo-user-sports_fan` | Same string as today's `build_pk()`. The person-level identity |
| AgentCore `actorId` | = `profile_id` | Already the case in v2 (`user_id` payload) |
| Personalize `USER_ID` | = `profile_id` | |
| `session_id` | AgentCore runtime session ID for the agent; client-generated UUID for player/feed sessions | The frontend should reuse one `session_id` across surfaces for the life of an app session |

`profile_id` must be derived server-side from the authenticated identity plus the selected profile, never trusted from free text or model output.

---

## 5. Data model

### 5.1 Tables

| Table | Purpose | Keys | TTL attribute | Streams |
|---|---|---|---|---|
| `movie-agent-user-profiles` (existing) | Durable profile: L2, L3 declared, plus existing `watch_list` and legacy items during migration | PK `user_id` (= `profile_id`), SK `data_type` | — | NEW_AND_OLD_IMAGES (audit of declared) |
| `user-interactions` (new) | Recent curated events, per-title watch state, L1 session items | PK `profile_id`, SK see below | `expires_at` | NEW_IMAGE (optional) |
| `recommendation-cache` (new) | Precomputed rows per surface | PK `profile_id`, SK `ROWS#<surface>` | `expires_at` | — |

All tables: on-demand capacity, encryption with an AWS managed key (or a customer managed key if required), point-in-time recovery on `movie-agent-user-profiles`.

### 5.2 `movie-agent-user-profiles` items

**`AFFINITIES` (L2)**: written only by the stream processor (and the household seed). Example: Action Fan after finishing Arrival.

```json
{
  "user_id": "p-116a44aca72c433e97bcfa7754beb13d",
  "data_type": "AFFINITIES",
  "version": 3,
  "updated_at": "2026-10-05T02:47:00Z",
  "entities": {
    "genre:sci-fi":       {"s": 0.895, "n": 12, "last": 1791171000, "label": "Sci-Fi"},
    "genre:drama":        {"s": 0.895, "n": 12, "last": 1791171000, "label": "Drama"},
    "person:amy_adams":   {"s": 0.195, "n": 2,  "last": 1791171000, "label": "Amy Adams"},
    "genre:horror":       {"s": -0.35, "n": 6,  "last": 1790300000, "label": "Horror"}
  },
  "contexts": {
    "weekend|evening": {
      "long_min": 4.33, "short_min": 0, "views": 1,
      "entities": {"genre:sci-fi": {"s": 0.12, "n": 1, "last": 1791171000, "label": "Sci-Fi"}}
    }
  },
  "tz_offset_min": -240,
  "centroids": [
    {"v": "<base64 float32 x1024, little-endian>", "w": 0.67, "n": 6},
    {"v": "<base64>", "w": 0.33, "n": 3}
  ],
  "notes": ["preferred era: 2010s-present"]
}
```

- `s` is clamped to [-1, 1]; `n` = supporting signal count; `last` = epoch seconds of the last supporting signal. Seeded entries have no `last` and do not fade until a real signal updates them.
- Entity keys are `genre:<slug>` and `person:<slug>` (the title's catalog genres and top 3 cast). Team, player and topic entities are **not built** (sports is parked).
- `contexts[bucket]`: per time-of-day bucket (Section 7.4), long- and short-form minutes, a count of scored views, and entity scores with the same formula and half-lives as `entities`. `tz_offset_min` is the member's last known UTC offset, used by the agent when the request carries none.
- `centroids`: up to 3 taste vectors (Section 7.4), each Nova embedding (1024 x float32, base64, ~5.5 KB), weight `w` = count / total and count `n`. Model and dimension are fixed by the catalog index, so they are not stored per item.
- `notes`: free-text persona lines written only by the seed. Nothing derives or updates them; the agent shows them under a "seeded, not from viewing" label.
- **Not built:** the top-50 entity cap and dropping `|s| < 0.05` (entity counts stay small at demo scale); `formats` and `habits` (`contexts` covers time of day, and nothing would read the rest).

**`L3#DECLARED` (L3 declared)**: written only by the Settings API (and the seeding script).

```json
{
  "user_id": "demo-user-sports_fan",
  "data_type": "L3#DECLARED",
  "version": 7,
  "updated_at": "2026-09-28T18:00:00Z",
  "updated_by": "settings-ui | agent-confirmed | seed",
  "excluded_genres": ["horror"],
  "avoid_themes": ["graphic violence"],
  "maturity_max": "PG-13",
  "languages": ["en"],
  "subtitles": "off",
  "max_length_minutes": null,
  "followed": {
    "teams": ["team:harbor-city-fc"],
    "players": ["player:kofi-asante-brook"],
    "drivers": [],
    "talent": ["person:james_mangold"]
  },
  "consent": {"personalization_from_history": true, "conversation_memory": true}
}
```

- `excluded_genres` values come from the catalog genre vocabulary (validated by the Settings API).
- `followed.*` are explicit positives (onboarding or confirmed chat). They replace the hard-coded `FAVORITE_PLAYER`.
- `consent.personalization_from_history = false` makes the Context module ignore L2 and the stream processor stop updating it.

**Legacy items**: `PREFERENCES`, `WATCH_HISTORY`, `top_picks`, `carousel_*` remain readable during migration (Section 13), then are removed. `watch_list` is unchanged.

### 5.3 `user-interactions` items

**Curated event (`EVT#<iso_ts>#<event_id>`)**: **Deferred.** The stream processor does not write these; only the household seed does, and nothing reads them. Their only planned reader was the nightly rebuild, which is also deferred (Section 7.4). The raw S3 event log is the fallback for replaying history. Intended shape: only significant events, TTL 180 days.

```json
{
  "profile_id": "demo-user-sports_fan",
  "sk": "EVT#2026-09-30T21:02:11Z#5f0c…",
  "event_type": "complete",
  "item_id": "hl-SYN-MAT-0001-0017",
  "content_type": "highlight",
  "engagement": 0.92,
  "device": "phone",
  "daypart": "weekday_evening",
  "session_id": "s-789",
  "expires_at": 1806000000
}
```

**Watch state (`TITLE#<item_id>`)**: upserted, no TTL (or 2 years).

```json
{
  "profile_id": "demo-user-action_fan",
  "sk": "TITLE#tt7970126",
  "content_type": "movie",
  "position_s": 4120,
  "duration_s": 9120,
  "completion": 0.45,
  "completed": false,
  "times_watched": 1,
  "last_watched": "2026-09-29T20:55:00Z"
}
```

Powers "continue watching", "already seen" exclusions, and the "because you watched" seed. Replaces `WATCH_HISTORY`.

The stream processor upserts it for every finished view of a catalog title (not for booth events): `last_watched`, `completion`, `content_type`; `completed = true` and `times_watched + 1` once completion reaches 0.9 (`completed` is never reset to false). The agent's long-term watched exclusion and the seed centroids (Section 7.4) read `completed` titles. `position_s`/`duration_s` are not written by the processor.

**Session state, behavior (`SESSION#behavior`)**: written only by the stream processor, TTL = last activity + 4 h. One item per member, holding **facts only** for the current session. It has no stored `intent` or mode label: each reader interprets the facts itself.

```json
{
  "profile_id": "p-116a44aca72c433e97bcfa7754beb13d",
  "sk": "SESSION#behavior",
  "session_id": "cf06652c-…",
  "seq": 13,
  "seen": ["<event_id>", "…"],
  "device": "phone",
  "tz_offset_min": -240,
  "last_ts": 1791150959,
  "expires_at": 1791165359,
  "views": {
    "tt9663946": {"item": "tt9663946", "type": "movie", "title": "Tenet", "duration_s": 7680,
                  "position_s": 32, "watched_s": 31, "plays": 1, "surface": "vod_player",
                  "started": 1791150900, "last_seen": 1791150931}
  },
  "recent": [
    {"item": "tt1001019", "type": "movie", "title": "Joke Night", "completion": 0.006,
     "watched_s": 43, "plays": 1, "reason": "closed", "surface": "vod_player",
     "ts": 1791150959, "bucket": "weekend|evening"}
  ],
  "format_mix": {"short_min": 0, "long_min": 0.72},
  "entities": {"genre:action": {"m": 0.713, "t": 1791150959, "label": "action"},
               "person:brad_pitt": {"m": 0.713, "t": 1791150959, "label": "Brad Pitt"}},
  "agent_accepts": ["tt9663946"],
  "list_adds": [],
  "impressions": {"agent": 10},
  "live": null
}
```

- `views`: open playbacks. `recent`: finished views, newest first, one entry per title (a rewatch replaces it and adds its `plays`), max 30. `bucket` is the local time-of-day bucket (Section 7.4), absent when the client sent no offset.
- `entities`: session attention in minutes watched (`m`) per genre and top-3 cast member, faded with a 20-minute half-life; below 0.01 dropped, top 20 kept.
- `agent_accepts`, `list_adds`: capped at 10, newest first. `seen`: the last 300 applied event IDs, which makes retried batches count once.
- A different `session_id` starts an empty record: nothing carries across sessions.
- Writes are conditional on the `seq` that was read (`attribute_not_exists(seq)` or `seq = :old`), retried on conflict.

**Session state, chat (`SESSION#chat`)**: **Not built, by decision.** The chat half of L1 lives in the agent runtime session instead: turns, the last batch and the session's shown titles stay in memory in the per-session microVM (the same runtime session ID routes to the same VM; 15 min idle timeout). All four frontend clients share one session ID. Titles shown this session are excluded from later picks (newest 15, `MAX_EXCLUDE_IDS`; titles asked for by name are exempt). Each turn is also written to AgentCore short-term memory, only so the long-term preference strategy can consolidate from it; the agent never reads it back.

### 5.4 `recommendation-cache` items

```json
{
  "profile_id": "demo-user-sports_fan",
  "sk": "ROWS#home",
  "generated_at": "2026-10-01T08:00:00Z",
  "source": "personalize:up-v2",
  "rows": [
    {"key": "top_picks", "title": "Top Picks for You", "item_ids": ["tt7970126", "…"]},
    {"key": "because_you_follow:player:kofi-asante-brook", "title": "Because you follow Kofi Asante-Brook", "item_ids": ["hl-SYN-MAT-0001-0017", "…"]},
    {"key": "theme:topic:motorsport", "title": "Start Your Engines", "item_ids": ["tt1979320", "…"]}
  ],
  "expires_at": 1790003600
}
```

Surfaces: `home`, `short_form_feed`, `post_play:<item_id>`. TTL 60 minutes by default. Invalidated (deleted) when L3 declared changes.

**Deferred:** today the rows are written once by the household seed and never change. Rebuilding them from L2 and refreshing them on L1 changes is left to Amazon Personalize (Section 9). There is no stored L1 intent to trigger invalidation; a refresh would be driven by L1 facts (e.g. joining a live game).

---

## 6. Profile layers and the composed inferred view

| Layer | Stored where | Writer | Freshness | Applied as |
|---|---|---|---|---|
| L1 session state | `user-interactions` `SESSION#behavior` (behaviour); agent runtime session (chat) | Stream processor (behaviour); agent (chat, in memory) | Seconds | Soft (one factual session line in rerank), hard (titles shown or played this session) |
| L2 affinities | `movie-agent-user-profiles` `AFFINITIES` | Stream processor | Seconds | Soft (taste text in rerank, centroids in retrieval) |
| L3 declared | `movie-agent-user-profiles` `L3#DECLARED` | Settings API only | On user action | Hard filters in every path |
| L3 inferred (view) | Not stored; composed per session | Profile Context module | Per session (cached) | Soft (agent prompt text) |
| Conversational LTM | AgentCore Memory `/actor/{actorId}/preferences/` | AgentCore extraction/consolidation | Seconds to minutes (async) | Input to the L3 inferred view |

### 6.1 Composing the L3 inferred view (no LLM)

Inputs: `AFFINITIES`, LTM preference records (top 10 by relevance), `L3#DECLARED`.

Built: steps 1 and 4, in `user_profile.render_affinities` (movie agent v2). **Not built:** steps 2 and 3 (LTM tagging and de-duplication) and the format line. Personas without an `AFFINITIES` item fall back to the legacy `PREFERENCES` text.

1. **Render L2 entities with templates.** Scores are first faded to now with the processor's half-lives (seeded entries without `last` do not fade). For the top 8 positive and top 3 negative entities:
   - `s ≥ 0.6` → "Strongly enjoys {label}"
   - `0.3 ≤ s < 0.6` → "Enjoys {label}"
   - `s ≤ -0.3` → "Tends to skip {label}"
   - Entities with `n < 3` are omitted (not enough evidence).
   - **Current time of day:** one line for the viewer's current bucket only (Section 7.4), from `AFFINITIES.contexts`, using this session's `tz_offset_min` or the stored one. It appears once the bucket has at least 3 scored views: up to 4 liked entities (`n ≥ 3`, `s ≥ 0.3`) and "full-length titles" or "short clips" when one form is at least 70% of the bucket's minutes, e.g. `- Weekend evenings (now): mostly Sci-Fi, Drama; full-length titles`.
   - **Seed notes:** `AFFINITIES.notes` are shown on their own line, `Seeded persona notes (not from viewing): …`, never as viewing-based taste.
   - ~~Top format: "Prefers {short clips|full-length titles} on {device} {daypart}"~~: not built (no `formats` field).
2. **Tag each LTM record.** Extract entity mentions by matching the record text against the catalog vocabulary (genres, teams, players, talent labels; case-insensitive, word-boundary match). Then:
   - same polarity and `|s| ≥ 0.3` in L2 → `confirmed by viewing`
   - opposite polarity and `|s| ≥ 0.3` → `stated, but viewing differs`
   - otherwise → `stated`
   - Records with no vocabulary match keep the tag `stated`.
3. **Remove duplicates.** If an LTM record and a rendered L2 line share an entity and polarity, keep the LTM wording with the `confirmed` tag and drop the L2 line.
4. **Never contradict declared.** Drop any line whose entity is in `excluded_genres`; declared limits are rendered separately as rules.

Optional improvement (not required for v1): when vocabulary matching finds nothing, embed the LTM text with Nova (`TEXT_RETRIEVAL`) and match against precomputed entity-label embeddings with cosine ≥ 0.6.

### 6.2 Context pack

The Profile Context module returns:

```python
ProfileContext(
    text_block: str,            # injected into rewrite/reduce prompts (≤ ~600 tokens)
    declared_filters: dict,     # structured, for OpenSearch and Personalize filters
    exclusions: list[str],      # session-recommended + skipped + completed (bounded)
    session_intent: dict,       # chat constraints only; no stored mode/focus (L1 is facts only)
    centroids: list[bytes],     # for retrieval only; never in the prompt
    followed: dict,             # teams/players/talent for sports and rows
    version: str,               # hash of inputs, logged for traceability
)
```

`text_block` format (sections omitted when empty):

```text
Viewer rules (must follow):
- Exclude genres: horror
- Avoid themes: graphic violence
- Maturity: up to PG-13; Language: English

Right now:
- Watching on phone, weekday morning; in quick-clips mode, focused on Kofi Asante-Brook
- This session: wants something light, under 2 hours

Viewer taste (soft guidance; never override the request):
- Strongly enjoys Sports; Enjoys Drama; Tends to skip Horror
- Prefers short clips on phone weekday mornings
- Loves underdog sports stories (confirmed by viewing)
- Likes arthouse cinema (stated, but viewing differs)
- Follows Harbor City FC and Kofi Asante-Brook
```

The existing guardrail in `rewrite.py` (preferences may only fill dimensions the conversation leaves open) stays unchanged.

### 6.3 Caching

As built: the taste text, the centroids and the watched list are cached on the agent's session object and re-read every `PROFILE_TTL_S` (60 s), regardless of input versions, so L2 changes reach a running session within a minute. `SESSION#behavior` is read on every turn. A single `ProfileContext` object (Section 6.2) is **not built**; these inputs are passed separately. The design below remains the target.

- Build once per `(profile_id, session_id)` on the first request; cache in the agent's session object.
- Rebuild in the background when any input version changes (`AFFINITIES.version`, `L3#DECLARED.version`, LTM refresh) or after `CONTEXT_TTL_S` (default 60 s). Requests always read the cache (same pattern as today's LTM refresher).
- `SESSION#*` items are read on every request (single `Query` on the partition, < 10 ms).

---

## 7. Behavioral pipeline

### 7.1 Event contract

Every event uses one envelope. As built, it is validated in code (`profile_events/contract.py`, shared by ingestion and the processor); the AWS Glue Schema Registry is **not used**.

```json
{
  "event_id": "5f0c9a2e-…",
  "schema_version": 1,
  "event_type": "progress",
  "profile_id": "demo-user-action_fan",
  "account_id": "demo-user",
  "session_id": "s-789",
  "device": "tv",
  "surface": "vod_player",
  "client_ts": "2026-09-29T20:55:00.120Z",
  "server_ts": "2026-09-29T20:55:00.480Z",
  "booth": false,
  "payload": {
    "item_id": "tt7970126",
    "content_type": "movie",
    "position_s": 4120,
    "watched_s": 30
  }
}
```

`event_type` values and payloads:

| Event type | Surface(s) | Payload | Notes |
|---|---|---|---|
| `play_start` | vod_player, clips_feed, sports | `item_id, content_type, position_s` | |
| `progress` | vod_player, clips_feed | `item_id, position_s, watched_s` | Heartbeat every 30 s (long-form), every 5 s (short-form) |
| `play_end` | vod_player, clips_feed | `item_id, position_s, reason` (`ended`, `closed`, `next`, `swipe`) | |
| `impression` | clips_feed, agent | `item_id, rank, slate_id` | Agent impressions = titles shown in a response |
| `agent_accept` | agent | `item_id, slate_id` | Viewer opened or played an agent recommendation (sent by the frontend) |
| `list_add` / `list_remove` | title_detail | `item_id` | From the existing save/remove list Lambdas |
| `live_join` | sports | `game_id, video_s` | |
| `highlight_open` | sports, home | `item_id, game_id` | Key moment / player highlight opened |

`agent_reject` is **dropped**: rejections ("not that", "something else") reach long-term memory through the conversation instead.

Optional envelope field `tz_offset_min`: the viewer's local time minus UTC, in minutes (−840 to +840), filled by the browser. It drives the time-of-day buckets (Section 7.4); events without it get no bucket.

Rules: no PII and no free text in events (conversation text stays in AgentCore Memory). `server_ts`, `account_id` and `booth` are set by the ingestion Lambda, not the client.

### 7.2 Ingestion

- **Client events**: `POST /events` on the existing API Gateway REST API, Cognito user pool authorizer. Body: `{"events": [ … up to 50 … ]}`. The client batches and flushes every 5 s, or on pause/close/app background.
- **Ingestion Lambda** (`ingest_events`): validates against the schema, rejects events whose `profile_id` does not belong to the caller's `account_id` (derived from the JWT), sets `server_ts`, drops events when `consent.personalization_from_history = false`, sets `booth` from the caller's Cognito `booth` group, and writes with `PutRecords` to Kinesis.
- **Server-side producers** (the agent): write directly to Kinesis (`PutRecords`) using the runtime execution role. The agent emits only `impression` (titles in each response) and attaches `slate_id = <session_id>:<turn>`; `agent_accept` comes from the frontend when the viewer opens a pick.

### 7.3 Stream and storage

- **Kinesis Data Streams** `profile-events`, on-demand mode, partition key `profile_id`, retention 7 days.
- **Event log**: Amazon Data Firehose `profile-events-to-s3` → S3 bucket `…-profile-event-log`, prefix `events/dt=YYYY-MM-DD/event_type=…/`, buffer 60 s / 64 MB, GZIP JSON (or Parquet with format conversion). Apache Iceberg tables are optional at this scale. Lifecycle: Standard 90 days → Glacier Instant Retrieval; delete after 2 years.
- **Dead-letter**: SQS `profile-events-dlq` for records the processor fails after retries.

### 7.4 Stream processor (Lambda)

Kinesis event source mapping: batch size 200, batching window 2 s, `ReportBatchItemFailures`, bisect on error, max retries 3, on-failure destination = DLQ. Parallelization factor 1 (keeps per-`profile_id` ordering per shard).

Per batch, grouped by `profile_id`:

As built (`infra/lambdas/profile_events/`): `l1.py` and `l2.py` are pure functions with no network calls; `processor.py` does the I/O. L2 is computed from **L1 changes**, not from raw events: after the L1 fold, the processor compares L1 before and after, and only what L1 newly recorded is scored. Steps 7 (curated `EVT#`), 8 (Personalize) and 9 (cache invalidation) are not built.

1. **Deduplicate** on `event_id`, across batches, using the last 300 applied IDs in `SESSION#behavior.seen`, so a retried batch counts once.
2. **Sessionize**: fold `play_start`, `progress` heartbeats and `play_end` into an open view per item in `SESSION#behavior.views`. A view is finished, and moved to `recent`, when `play_end` arrives or when an event for the same member finds it 10 minutes without a heartbeat (reason `timeout`). Completion = `position_s / duration_s`, or 1.0 for reason `ended`. One event credits at most 120 s of watching. A new `session_id` starts an empty record.
3. **Enrich** from the content catalog: `title`, `genres`, `cast` (top 3, roles stripped), and for liked titles the Nova vector. One OpenSearch lookup per batch, with an in-memory cache (10,000 items, 15 min TTL). If the lookup fails, facts are still written without entity attention.
4. **Score engagement** `e ∈ [-1, 1]` for each newly finished view and each new `agent_accept` / `list_add` (`l2.signals`):

   | Content type | Completion `c` | `e` |
   |---|---|---|
   | long-form (movie/episode) | watched < 120 s | not scored (sampling, not an abandon) |
   | | `c < 0.10` | −0.4 (early abandon) |
   | | `0.10 ≤ c < 0.50` | +0.1 |
   | | `0.50 ≤ c < 0.90` | +0.5 |
   | | `c ≥ 0.90` | +0.8 |
   | short-form (micro drama/highlight, ≤ 3 min) | watched < 3 s | −0.3 (skip) |
   | | `c < 0.5` | 0.0 |
   | | `0.5 ≤ c < 0.9` | +0.2 |
   | | `c ≥ 0.9` | +0.4 |
   | | `c ≥ 0.9`, rewatch | +0.6 |
   | any | `list_add` | +0.7 |
   | any | `agent_accept` | +0.5 |

   Short-form weights are deliberately lower than long-form so clip volume does not dominate taste. The 120-second minimum exists because a few seconds of a two-hour film (or the 2h 08m fallback duration) would otherwise always count as an early abandon. `agent_reject` is dropped (Section 7.1).
5. **Update L1** (`SESSION#behavior`, Section 5.3): `views`, `recent` (keep 30), `format_mix`, session `entities` (20-minute half-life, top 20), `agent_accepts`, `list_adds`, `impressions`, `live`, `device`, `tz_offset_min`. **Facts only**: there is no `intent` or `focus` field and no mode rules (`quick_clips`, `lean_back`, `following_live`); each consumer interprets the facts at read time. Written on every batch that applies an event, conditional on the `seq` that was read, retried on conflict. Each finished view gets a time-of-day **bucket** from `tz_offset_min`, derived from the clock only:

   | | Daytime (05:00-17:00) | Evening (17:00-05:00) |
   |---|---|---|
   | Weekday (Mon-Fri) | `weekday\|daytime` | `weekday\|evening` |
   | Weekend (Sat-Sun) | `weekend\|daytime` | `weekend\|evening` |

   Viewing after midnight belongs to the previous day's evening (Sunday 01:30 and Monday 01:30 are both `weekend|evening`; Friday night after midnight is a weekday evening).
6. **Update L2** (`AFFINITIES`) and watch state, unless the batch carries `booth = true`. Consent-off members never reach the processor: ingestion drops their events (Section 7.2). If the L2 write fails after L1 is committed, the error is logged and the batch still succeeds; a retry could not re-apply it, since L1 would skip the already-seen events.

   **Watch state:** every newly finished view of a catalog title upserts `TITLE#<item_id>` (Section 5.3), scored or not.

   **Entities**, for each scored signal and each entity `x` of the title (genres and top 3 cast):

   ```text
   s_x ← clamp( s_x · 2^(−Δt / H_x) + α · e , −1, 1 )
   n_x ← n_x + 1 ;  last_x ← signal_ts
   ```

   Half-lives `H`: genre 30 d, person 60 d (other kinds 30 d; theme, team, player and format entities are not built). Learning rate `α = 0.15`. Entities reached through `parent_item_id` (a clip's parent) get `α/2`. A seeded entity with no `last` is not faded.

   **Time-of-day contexts:** each scored view with a bucket also updates `AFFINITIES.contexts[bucket]` (Section 5.2): minutes by form, view count, and entity scores with the same formula. `AFFINITIES.tz_offset_min` stores the last known offset.

   **Taste centroids** (only for `e > 0`, max 3 centroids; same constants as the household seed and the agent, checked by a test):
   - If none are stored, first build them from the member's completed `TITLE#` titles (up to 50, oldest first) with the same grouping the agent uses, so the first update extends the existing taste instead of replacing it with one title.
   - `v` = item's Nova vector (normalized); find the nearest centroid by cosine.
   - If the best cosine ≥ 0.55: `c ← normalize(c + η·e·v)`, `η = 0.1`; its count goes up.
   - Else, if fewer than 3 centroids: create a new centroid = `v`.
   - Else: update the nearest centroid anyway with `η/2`.
   - Weights = count / total.

   Write `AFFINITIES` once per batch with any scored signal, conditional on `version`, then bump `version`. There is no once-a-minute throttle: views finish only a few times per session.

   **Known limit:** if two views of the same title finish in one batch (e.g. a timed-out view followed by a restart), L1 merges them into one `recent` entry and only the later one is scored.
7. **Write curated events** (`EVT#…`): **Deferred** (Section 5.3). Only the `TITLE#` upsert from this step is built (step 6).
8. **Forward to Personalize** (Section 9.3) unless `booth = true`: **Not built.**
9. **Invalidate** `recommendation-cache` rows: **Not built.** Home rows are left to Personalize (Section 5.4).

**Nightly rebuild**: **Deferred.** The live update keeps L2 current on its own. A rebuild would apply scoring changes to past views, fill lost updates, close views that never got a `play_end`, and refresh fading (readers already fade scores at read time, Section 6.1). Without `EVT#` items, it would replay the raw S3 event log (Section 7.3) through the same `l1.fold` and `l2` functions. If built, it should start as an on-demand Lambda rather than a schedule.

### 7.5 Session close

**Not built.** Today an open view is closed only by the member's next event, after 10 minutes without a heartbeat (step 2). A member who leaves mid-play and never returns leaves that view uncounted until `SESSION#behavior` expires (4 h after the last activity). The intended design: after 30 minutes without events, the next processor invocation for that profile (or a rebuild) finalizes the session's views. No separate timer is required.

---

## 8. Content catalog requirements

Every item that can generate an event or be recommended needs an `item_id` and metadata in the catalog (OpenSearch) and in the Personalize Items dataset.

| Content | `item_id` format | Source today | Required fields |
|---|---|---|---|
| Movies/series | `tt…` | `catalog` | genres, talent, duration, Nova vector (exists) |
| Micro dramas | `md-<n>` | `frontend /clips/microdramas.json`, BDA descriptions | genres, themes, duration, Nova vector (to add) |
| Soccer highlights | `hl-<game_id>-<n>` | match-state `/highlights` | players, team, event type, parent `game_id`, duration (to add) |
| Live events | `game:<game_id>` | match catalog | teams, competition |

Action: extend the catalog indexer to upsert micro dramas, highlights, and key moments into the same OpenSearch collection (a separate index `media-items-*` is acceptable as long as the processor and Personalize export read both), with Nova embeddings for each.

For live production, highlights and their vertical renditions can be produced in real time by AWS Elemental Inference (event clipping and smart crop) attached to the AWS Elemental MediaLive channel; the clip metadata is matched to feed events by video time. The sample uses pre-cut clips.

---

## 9. Amazon Personalize

### 9.1 Dataset group and schemas

One dataset group `unified-profile` (custom). All formats in one group so cross-format behavior is learned jointly.

**Item interactions** schema:

```json
{
  "type": "record", "name": "Interactions", "namespace": "com.amazonaws.personalize.schema",
  "fields": [
    {"name": "USER_ID", "type": "string"},
    {"name": "ITEM_ID", "type": "string"},
    {"name": "TIMESTAMP", "type": "long"},
    {"name": "EVENT_TYPE", "type": "string"},
    {"name": "EVENT_VALUE", "type": "float"},
    {"name": "DEVICE", "type": "string", "categorical": true}
  ],
  "version": "1.0"
}
```

**Items** schema: `ITEM_ID`, `CONTENT_TYPE` (categorical), `GENRES` (categorical, `|`-separated), `TALENT` (categorical), `TEAMS` (categorical), `PLAYERS` (categorical), `MATURITY` (categorical), `LANGUAGE` (categorical), `DURATION_S` (int), `CREATION_TIMESTAMP` (long), `DESCRIPTION` (textual).

**Users** schema: `USER_ID`, `PERSONA` (categorical), `LANGUAGE` (categorical).

### 9.2 Solutions, campaigns, filters

| Resource | Recipe | Use |
|---|---|---|
| `up-v2` solution + campaign | `aws-user-personalization-v2` | Standard rows, short-form feed, post-play |
| `rank-v2` solution + campaign | `aws-personalized-ranking-v2` | Agent rerank |

Automatic training on (UP-v2 refreshes the model with new items every 2 hours). Set minimum provisioned TPS low (1) for a demo.

Filters (parameterized; values passed at request time from L3 declared and L1):

| Filter | Expression |
|---|---|
| `declared-limits` | `EXCLUDE ItemID WHERE Items.GENRES IN ($EXCLUDED_GENRES) \| EXCLUDE ItemID WHERE Items.MATURITY IN ($BLOCKED_MATURITY)` |
| `content-type` | `INCLUDE ItemID WHERE Items.CONTENT_TYPE IN ($CONTENT_TYPES)` |
| `not-completed` | `EXCLUDE ItemID WHERE Interactions.EVENT_TYPE IN ("complete")` |

Personalize filters consider at most 100 interactions per user per event type, so "already watched" is also enforced from `TITLE#` watch state and the agent's session exclusions.

### 9.3 Event forwarding

- Event tracker on the dataset group; the stream processor calls `PutEvents` for curated events only: derived `view` (`EVENT_TYPE` = `view` or `complete`, `EVENT_VALUE` = `e`), `list_add`, `agent_accept`, `highlight_open`. Not heartbeats, not impressions.
- Quotas: 1,000 `PutEvents` requests/s per dataset group, 10 events per call. Batch per profile.
- Lower-signal history (impressions, rejects) can be loaded by periodic incremental bulk import from the event log.
- Skip forwarding when `booth = true`.

### 9.4 Bootstrap data (required in a new account)

Personalize custom solutions need a minimum volume of interactions (at least 1,000 item interactions, with minimum unique-user requirements; check the current Personalize docs). Three personas are not enough. Generate a synthetic bootstrap set:

- 300 synthetic users per persona archetype (action fan, rom-com lover, sports fan, plus 2 mixed archetypes), `USER_ID = synthetic-<archetype>-<n>`.
- 20–60 interactions each, sampled from catalog items weighted by the archetype's genre/team affinities, across all content types, with realistic `EVENT_VALUE`s from the Section 7.4 scoring table.
- The real demo personas get interactions derived from their seeded `WATCH_HISTORY` and affinities.

The bootstrap generator is a one-off script (`scripts/personalize_bootstrap.py`) that writes CSVs to S3 for a FULL import.

---

## 10. Agentic path changes (movie agent v2)

### 10.1 Profile Context module

New package `backend/movie-ai-agent-v2/profile_context/`:

- `store.py`: reads `AFFINITIES`, `L3#DECLARED`, `SESSION#*` (one `Query` on each table's partition), catalog vocabulary (cached).
- `compose.py`: Section 6.1 and 6.2 logic (pure functions, unit-tested).
- `context.py`: `get_profile_context(profile_id, session_id) -> ProfileContext` with caching (Section 6.3).

Integration points:

| Current code | Change |
|---|---|
| `memory.get_user_profile_context()` → `user_profile.get_profile_block()` | Replace with `ProfileContext.text_block` (the module also reads LTM through the existing refresher) |
| `agent_v2._combined_preferences()` | Return `ProfileContext.text_block` |
| `memory.get_excluded_ids()` | Union of `_recommended_ids` and `ProfileContext.exclusions` |
| `memory.add_turn()` | Emit `impression` events (built, written in the background). The `SESSION#chat` write is **not built**: shown titles stay in the runtime session (Section 5.3) |
| `rewrite.py` | Superseded: a single understanding step replaced rewrite and router. `SESSION#chat` constraints are **not built** |
| `memory._fetch_ltm()` | Namespace becomes `/actor/{actorId}/preferences/` (Section 11) |

### 10.2 Retrieval tool: centroids, declared filters, rerank, fusion

1. **Declared filters** go into every OpenSearch query as `must_not` (genres, maturity) and `filter` (language) clauses.
2. **Centroid boost** (catch-all path): run one additional kNN query per centroid (k = 20) and add it as an extra RRF list with weight 0.3. Skip when the request has a person/title/year constraint.
3. **Personalize rerank**: after fan-out/catch-all produces up to 35 candidates (`CATCHALL_SIZE`), call `GetPersonalizedRanking` (`rank-v2` campaign, `userId = profile_id`, `context = {"DEVICE": …}`, `filterArn = declared-limits`). Timeout 300 ms; on timeout or error, continue without it.
4. **Fusion** (weighted RRF, `k = RRF_K`):

   ```text
   score(i) = w_sem / (k + rank_sem(i)) + w_p / (k + rank_p(i))
   ```

   - Specific requests (person, title, year range, or `genre_authoritative`): `w_sem = 1.0`, `w_p = 0.25`
   - Open-ended mood/genre requests: `w_sem = 1.0`, `w_p = 0.6`
   - Vague or "surprise me": `w_sem = 1.0`, `w_p = 1.0`
   - Items unknown to Personalize (recent clips) keep only their semantic term.
5. `reduce` then runs unchanged on the fused order with the new `text_block`.

### 10.3 Declared-change confirmation flow

- New agent tool `propose_declared_change(change)`: the model proposes, for example, `{"op": "add", "field": "excluded_genres", "value": "horror"}`. The tool does not write; it returns a confirmation question.
- On the next turn, if the viewer confirms, the agent calls `apply_declared_change(change_id)`, which invokes the Settings Lambda with `profile_id` taken from the runtime's authenticated request context (never from model output).
- In booth mode, confirmed changes are kept for the session only instead of being written to `L3#DECLARED` (`SESSION#chat` is not built; session-only overrides live in the runtime session).

### 10.4 Other consumers

- **Live sports agent**: reads `ProfileContext.followed` and `SESSION#behavior.live` to personalize catch-up ("how is Kofi Asante-Brook doing") and to order highlights.
- **Frontend**: `FAVORITE_PLAYER` and `MICRODRAMA_ORDER` are replaced by a new `GET /profile/summary` (Settings Lambda, read-only) returning `followed` and the top L2 genres, and by `ROWS#short_form_feed` from the cache.

---

## 11. AgentCore Memory configuration (new account)

| Setting | Value |
|---|---|
| Memory | `unified_profile_mem`, event expiry 30 days |
| Strategy 1 | Custom User Preference `TastePreferences`, namespace `/actor/{actorId}/preferences/`, extraction and consolidation prompts and models as in `setup_ltm_strategy.py` (Claude Haiku 4.5, single-subject records) |
| Strategy 2 | Summary, namespace `/actor/{actorId}/session/{sessionId}/summary/` (optional; used for "last time we talked about…") |
| Strategy 3 | Semantic facts, namespace `/actor/{actorId}/facts/` (optional; "watches with kids on weekends") |
| Retrieval | `retrieve_memories(namespace=f"/actor/{actor_id}/preferences/", top_k=10)` in the background refresher (`LTM_CACHE_TTL_S = 30`) |

Actor-first namespaces give one subtree per viewer for retrieval, deletion, and per-user IAM conditions. Parametrize `setup_ltm_strategy.py` (memory ID, Region, execution role ARN, namespace) through arguments or environment variables instead of constants.

Memory record streaming to Kinesis is not required by this design (the inferred view reads LTM directly). Add it later if a stored inferred profile or cross-system propagation of deletions is needed.

---

## 12. Settings API

Lambda `profile_settings` behind the existing API Gateway REST API (Cognito authorizer) and invocable directly by the agent runtime role.

| Route | Method | Behavior |
|---|---|---|
| `/profile/declared?profile=<slug>` | GET | Returns `L3#DECLARED` |
| `/profile/declared?profile=<slug>` | PUT | Full replace; body includes `version`; conditional write `version = :expected`; returns 409 on conflict |
| `/profile/declared/changes?profile=<slug>` | POST | Single operation `{op, field, value}` (used by the agent flow) |
| `/profile/summary?profile=<slug>` | GET | Read-only: `followed`, top L2 entities (labels only) |

Rules:

- `profile_id = build_pk(jwt.username, profile)`; the caller can only touch profiles of its own account. Child-profile maturity changes require the account-owner role (custom claim) if enabled.
- Validate values against vocabularies (genres from the catalog, maturity enum, ISO 639-1 languages, entity IDs that exist).
- On success: increment `version`, set `updated_by`, delete `recommendation-cache` items for the profile, and emit a `settings_changed` event to Kinesis (so the agent's context cache rebuilds).
- Audit: DynamoDB Streams on the profile table → Lambda filter (`data_type = L3#DECLARED`) → Firehose → S3 `audit/` prefix.

---

## 13. Standard ML path

- **Row builder** Lambda `build_rows`, triggered by EventBridge Scheduler (every 60 min for active profiles) and on cache invalidation (SQS message from the processor/Settings API):
  1. `GetRecommendations` (`up-v2`) for `home` (`numResults = 50`, filters `declared-limits` + `not-completed`).
  2. Theme rows: for each top-3 L2 entity with `s ≥ 0.5`, request with an extra filter (`INCLUDE … WHERE Items.GENRES IN (…)` or `Items.PLAYERS IN (…)`) and title from a template ("Because you follow {label}", "More {label}").
  3. `short_form_feed`: `content-type` filter with `["micro_drama", "highlight"]`; followed players' highlights first.
  4. Write `ROWS#<surface>` with TTL.
- **Existing Lambdas**: `get_carousels` and `get_top_picks` read `recommendation-cache` first and fall back to legacy `carousel_*` / `top_picks` items. Hydration through OpenSearch is unchanged.

---

## 14. Migration and seeding

1. **Create** the new tables, Kinesis stream, Firehose, DLQ, Personalize dataset group (in the phase order of Section 18).
2. **Seed per persona** (`scripts/seed_unified_profile.py`), from today's `PREFERENCES` and `WATCH_HISTORY`:

   | Today | New |
   |---|---|
   | `favorite_genres` | `AFFINITIES.entities["genre:*"]` with `s = 0.7, n = 10` |
   | `favorite_directors` | `AFFINITIES.entities["person:*"]` with `s = 0.6, n = 6` |
   | `disliked_genres`, `avoid_themes`, `language_preferences` | `L3#DECLARED.excluded_genres`, `avoid_themes`, `languages` |
   | `max_movie_length_minutes` | `L3#DECLARED.max_length_minutes` |
   | `mood_preferences`, `preferred_era` | Rendered by the template as soft lines (store under `AFFINITIES.formats` / a `notes` list) |
   | `WATCH_HISTORY.movies[]` | `TITLE#<id>` with `completed = true` + centroids computed from their Nova vectors |
   | `FAVORITE_PLAYER` (frontend) | `L3#DECLARED.followed.players` for `sports_fan` |
   | `tappable_options` | Stays as UI config (not profile) |

3. **Switch the agent** to the Profile Context module behind a flag (`PROFILE_CONTEXT_ENABLED`); compare outputs against the legacy block for the three personas.
4. **Remove** `PREFERENCES`/`WATCH_HISTORY` reads once parity is confirmed.

---

## 15. Booth mode

- A session is in booth mode when the Cognito user is in the `booth` group (or the request carries `mode=booth` from a pinned kiosk). The ingestion Lambda sets `booth = true` on events.
- Effects: L1 updates normally (sessions visibly adapt); L2 updates, Personalize `PutEvents`, and `TITLE#` writes are skipped (`EVT#` is not written at all); declared changes stay session-only; AgentCore Memory still learns (preferences appear mid-session).
- Reset script `scripts/reset_persona.py <profile_id>`: deletes `SESSION#*` and the persona's LTM records, restores seeded items.

---

## 16. IAM summary

| Principal | Allow |
|---|---|
| Ingestion Lambda | `kinesis:PutRecords` on `profile-events`; `dynamodb:GetItem` on `L3#DECLARED` (consent) |
| Stream processor Lambda | Kinesis read (event source mapping); `dynamodb:GetItem/PutItem/UpdateItem/Query` on `user-interactions`; `GetItem/PutItem` on `movie-agent-user-profiles` (code path writes `AFFINITIES` only; consent is enforced at ingestion); `aoss:APIAccessAll` (read) on the catalog collection; `sqs:SendMessage` on the DLQ. Not granted until built: `DeleteItem` on `recommendation-cache`, `personalize:PutEvents` |
| Agent runtime role | `dynamodb:GetItem/Query` on profile and interaction tables; `kinesis:PutRecords`; `personalize:GetPersonalizedRanking`; `lambda:InvokeFunction` on `profile_settings`; existing Bedrock, AOSS, AgentCore Memory permissions (no `SESSION#chat` writes) |
| Settings Lambda | `GetItem/PutItem/UpdateItem` on `movie-agent-user-profiles` (code path: `L3#DECLARED` only); `GetItem` on `AFFINITIES`; `DeleteItem` on `recommendation-cache`; `kinesis:PutRecords` |
| Row builder Lambda | `personalize:GetRecommendations`; `GetItem` on profile table; `PutItem` on `recommendation-cache` |
| Personalize service role | `s3:GetObject/ListBucket` on the bootstrap/import prefix |

IAM cannot restrict writes by sort key. The "only writer" rules for `AFFINITIES` and `L3#DECLARED` are enforced by separate roles and code paths. If stricter isolation is required, use `dynamodb:Attributes` conditions on `UpdateItem` or move `L3#DECLARED` to its own table.

---

## 17. Configuration

New environment variables (agent unless noted):

| Variable | Default | Purpose |
|---|---|---|
| `PROFILE_CONTEXT_ENABLED` | `true` | Use the Profile Context module |
| `INTERACTIONS_TABLE` | `user-interactions` | |
| `REC_CACHE_TABLE` | `recommendation-cache` | Agent, processor, row builder, Settings |
| `PROFILE_EVENTS_STREAM` | `profile-events` | Agent, ingestion, Settings |
| `CONTEXT_TTL_S` | `60` | Context cache refresh |
| `PERSONALIZE_RANK_CAMPAIGN_ARN` | — | Rerank |
| `PERSONALIZE_UP_CAMPAIGN_ARN` | — | Row builder |
| `PERSONALIZE_TRACKING_ID` | — | Processor |
| `PERSONALIZE_FILTER_DECLARED_ARN` / `_CONTENT_TYPE_ARN` / `_NOT_COMPLETED_ARN` | — | |
| `RERANK_TIMEOUT_MS` | `300` | |
| `FUSION_WEIGHTS` | `specific=0.25,open=0.6,vague=1.0` | |
| `CENTROID_RRF_WEIGHT` | `0.3` | |
| `SETTINGS_FUNCTION_NAME` | `profile_settings` | |
| `LTM_NAMESPACE_TEMPLATE` | `/actor/{actorId}/preferences/` | |

Processor tuning (constants in the shared scoring module): half-lives, `α`, `η`, centroid threshold, engagement table.

---

## 18. Build plan

| Phase | Scope | Acceptance criteria |
|---|---|---|
| 0. Baseline | Deploy the existing solution in the new account: CDK stack, Cognito, frontend, OpenSearch catalog index, AgentCore runtimes, Memory (actor-first namespace), model access (Nova, Claude Haiku/Sonnet), shard warmer | Existing demo works end to end for all three personas |
| 1. Profile store + context | New tables; `seed_unified_profile.py`; Profile Context module; agent integration behind flag | For each persona, `text_block` contains rules, taste, and follows; recommendations match or improve on legacy for a fixed prompt set; declared exclusions never appear in results |
| 2. Events + L1/L2 | `/events` endpoint, ingestion Lambda, Kinesis, Firehose, processor, frontend emitters (VOD player, clips feed, sports), agent impressions | Playing a highlight to the end updates `SESSION#behavior` within 5 s and `AFFINITIES` within 2 min; event log objects land in S3; replaying a fixture through the processor yields the expected L2 (unit test) |
| 3. Settings API | Lambda + routes + agent confirm flow + audit stream | Saying "never show me horror" then confirming writes `L3#DECLARED`; horror disappears from agent results and rows on the next request; audit record in S3 |
| 4. Personalize | Catalog extension (micro dramas, highlights, key moments); bootstrap data; dataset group, solutions, campaigns, filters, tracker; `PutEvents`; row builder; agent rerank + fusion | Rows served from `recommendation-cache`; rerank adds < 300 ms p95 and falls back cleanly; specific requests keep on-topic results (regression set) |
| 5. Frontend personalization | `/profile/summary`; replace `FAVORITE_PLAYER` and `MICRODRAMA_ORDER` | Sports fan's followed player comes from the profile; short-form feed order comes from `ROWS#short_form_feed` |
| 6. Booth mode | Group/flag, processor gating, reset script | After a day of booth use, persona `AFFINITIES.version` is unchanged; sessions still adapt |

---

## 19. Testing

- **Unit**: engagement scoring, decay, centroid updates, the L1 fold and L1-to-L2 signals, time-of-day buckets, template rendering, LTM tagging, fusion, declared-filter construction. Pure functions with table-driven tests.
- **Processor integration**: fixture of ~200 events per persona replayed against DynamoDB Local or a test stack; snapshot-compare `SESSION#behavior` and `AFFINITIES`.
- **Agent regression**: fixed prompt set per persona (existing test harness `test.py`), comparing top-5 results and checking that no declared-excluded item appears.
- **Load (optional)**: replay 10× booth traffic through `/events`; check iterator age < 5 s and no throttling.

---

## 20. Observability

| Signal | Source | Alarm |
|---|---|---|
| Kinesis iterator age | CloudWatch `GetRecords.IteratorAgeMilliseconds` | > 60 s for 5 min |
| Processor errors / DLQ depth | Lambda errors, SQS `ApproximateNumberOfMessagesVisible` | DLQ > 0 |
| `PutEvents` throttles | Processor custom metric | > 0 |
| Context build latency, cache hit rate | Agent EMF metrics | p95 > 50 ms |
| Rerank latency, fallback rate | Agent EMF metrics | fallback > 10 % |
| Events per surface per minute | Processor EMF metric | drop > 80 % vs previous hour |
| Declared changes | Audit stream count | informational |

Log the `ProfileContext.version` hash with each agent response so a recommendation can be traced to the exact profile inputs.

---

## 21. Security and privacy

- All new endpoints require Cognito authentication; no unauthenticated routes. `profile_id` is always derived from the token.
- No PII or free text in behavioral events; conversation text stays in AgentCore Memory.
- Consent flags are enforced at ingestion and in the Context module.
- Deletion of a profile: delete partition items in all three tables, delete AgentCore Memory records under `/actor/{actorId}/`, delete the user from Personalize (`users` dataset deletion job / `DeleteUsers` equivalent per current API), and purge event-log objects by `profile_id` (Athena-driven rewrite, or Iceberg row deletes if adopted).
- Encryption at rest on all stores; TLS in transit.

---

## 22. Extensions (not in scope)

- **Stored inferred profile** (`L3#INFERRED`): LLM theme synthesis and lifecycle (reinforce, weaken, merge, forget), fed by AgentCore Memory record streaming to Kinesis.
- **Amazon Managed Service for Apache Flink** in place of the Lambda processor at high volume; **ElastiCache (Valkey)** for L1.
- **Custom sequence model** with exported user embeddings, if Personalize limits are reached.

---

## 23. Open questions

1. Item ID scheme for highlights and key moments across matches: confirm `hl-<game_id>-<n>` and how they map to clip files.
2. Whether micro dramas and highlights join the existing title index or a separate `media-items` index.
3. Maturity ratings and languages for micro dramas and highlights (needed for declared filters).
4. Personalize minimum data requirements and quotas in the target Region at build time (verify against current documentation).
5. Child-profile and account-owner semantics for maturity changes (Cognito custom claim or out of scope for the demo).
