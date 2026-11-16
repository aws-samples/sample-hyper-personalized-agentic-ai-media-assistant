# movie-ai-agent-v2

A Strands-based Bedrock AgentCore recommendation agent. It streams status
events while it works and runs this pipeline:

```
understand  ->  answer | taste | fan-out | catch-all  ->  (relaxation ladder)  ->  reduce
```

- **understand** (`understand.py`): one cached LLM call reads the latest message
  plus the conversation and returns structured intent: answer or recommend,
  category, hard filters, soft signals and the search text. Fixed code
  (`router.py`) derives the path from those fields. The member's preferences are
  never shown to this step, so taste can't rewrite what was asked; only the L3
  rules are listed, so the same call can report which ones the request overrides.
- **profile** (`apply_profile`): applies the member's L3 rules and limits. The
  kids limit restricts every path to G, TV-Y, TV-Y7 and TV-G titles.
- **answer** (`answer.py`): a question about titles already shown is answered
  from catalog facts, with no new search.
- **taste**: an open request ("surprise me") searches with the member's L2 taste
  centroids (`taste.py`) as the query.
- **fan-out** (`fanout.py`): parallel LLM reads across token-balanced catalog
  shards for mood and genre requests. Kids requests read only the kids shard,
  and its picks are narrowed to the requested format. Ids the shards didn't
  contain are dropped. Shard system prompts are cached at Bedrock (1h TTL).
- **catch-all** (`catchall.py`): OpenSearch hard filters plus hybrid KNN/BM25 for
  requests with a filter (person, years, rating, awards, exact title).
- **relaxation ladder** (`_relax` in `agent_v2.py`): runs only when retrieval
  leaves no usable title. See [Empty results](#empty-results).
- **reduce** (`reduce.py`): relevance-first, popularity-aware rerank into the
  final reply, then rehydration of full records and content URLs.

Every path removes titles already shown in this session and titles the member
has watched or played (`session_behavior.py`), except a title asked for by name.

Multi-turn context (history and already-recommended ids) comes from AgentCore
Memory. Each response writes impression events to the profile-events stream
(`events_producer.py`).

## Empty results

When the main search leaves nothing usable, the ladder relaxes the request one
step at a time and stops at the first step that returns a usable title:

1. Drop the minimum rating and the awards filter.
2. Drop the year range.
3. Drop the person or exact title, and run a semantic search on the request text.
4. Widen movies to movies and series.
5. Let back titles already suggested in this session.

A step whose filter isn't in the request is skipped. Micro-drama requests use a
single short-form fallback instead. The kids ratings, limit genres, genre
excludes, genre rules and watched titles are never relaxed.

When the ladder runs, the reply opens with a fixed sentence ("I couldn't find an
exact match, so here are the closest picks."). The reranker gets a Search note
listing what the picks may not meet, so its own line doesn't claim the dropped
parts. Each step logs a line such as
`[v2] relax step 2/4 filtered dropped=[...] -> 12 usable`.

## Layout

```
main.py              AgentCore entrypoint: dispatch + SSE streaming + keepalive
agent_v2.py          orchestrator generator (status events, ladder, final response)
understand.py        understanding step (one cached LLM call) + apply_profile
fewshot.py           worked examples appended to the understanding prompt
router.py            filter keys, kids ratings and path rules used by understand/catch-all
answer.py            answer turns about titles already shown
catchall.py          build_filters + filtered, taste, semantic and short-form searches
fanout.py            parallel shard fan-out
catalog.py           shard prompts, shards by category, title metadata
retrieval.py         OpenSearch client, query embedder, hybrid search, fetch-by-ids
reduce.py            rerank + rehydrate full records/content URLs
taste.py             L2 taste centroid encoding/decoding
user_profile.py      member profile reader (L2 AFFINITIES, L3 rules and limits, watch state)
session_behavior.py  L1 session facts (played titles, format mix) for the agent
memory.py            AgentCore Memory (STM+LTM), local SessionMemory fallback
events_producer.py   impression events to the profile-events stream
intent.py            chitchat gate (skipped by default, see SKIP_INTENT)
analysis_agent.py    scene analysis requests (request_type="analysis")
llm.py               Strands helper: cache-pointed Agent, usage/cache extraction
config.py            models, endpoints, tunables (env-overridable)
shards/              token-balanced shard system prompts + manifest
evals/               offline eval suites
```

## Tests

The agent's unit tests live in `infra/deployment/test_taste.py` and run with the
rest of the repo's suite:

```bash
.venv/bin/python -m pytest infra/deployment -q
```

## Deploy

The agent is deployed by the repo's CDK runner, not from this directory. After
changing agent source, run the `build` phase (new agent image), then
`application` (the runtime and the shard warmer):

```bash
.venv/bin/python infra/deployment/deploy.py \
  --config infra/environments/<name>.json --phase build
.venv/bin/python infra/deployment/deploy.py \
  --config infra/environments/<name>.json --phase application
```

See `infra/deployment/README.md` for the full runner.

## Shard cache warming

Each shard's system prompt is cached at Bedrock (server-side, cross-container,
about 1h TTL). A scheduled Lambda in `backend/shard-warmer/` keeps that cache
warm, so a cold agent container doesn't pay the cache write on its first
fan-out. It fires one Bedrock `converse` per shard with the same system prompt,
`cachePoint` TTL and model the fan-out uses, every 50 minutes.

The Bedrock cache key is the prompt content, so the warmer must carry the same
prompts as the agent. `backend/shard-warmer/shards/` is a copy of this
package's `shards/` prompts and manifest.

**Catalog-change workflow**

1. Split the catalog: `python build_kids_split.py` reads `data/catalog/*.json`
   and writes `movie_index_kids.csv` and `movie_index_non_kids.csv`. The kids
   boundary is `KIDS_MPAA_RATINGS` (G, TV-Y, TV-Y7, TV-G). Micro-dramas are left
   out; the agent finds them through catch-all search.
2. Build the shards: `python build_shards.py` writes `shards/`. Kids titles go
   into one shard, and non-kids movies and TV into token-balanced shards (the
   bundled catalog has no TV titles, so `tv` requests use the movie shards).
   `build_semantic_shards.py` is the centroid-routing variant (`semantic_shards/`).
3. Copy the shard prompts and `manifest.json` to `backend/shard-warmer/shards/`.
4. Deploy the `build` and `application` phases.

A test fails if an adult movie or series is missing from the shards, or if the
kids shard contains a non-kids rating.

## Configuration

`config.py` reads env overrides (and a local `.env` if present). Notable knobs:

| Variable | Purpose | Default |
|----------|---------|---------|
| `SKIP_INTENT` | Skip the chitchat gate (the caller already decided a search is needed) | `true` |
| `V2_MODEL_ID` | Default model for every stage | `global.anthropic.claude-haiku-4-5-...` |
| `V2_UNDERSTAND_MODEL_ID` | Understanding step model (keep regional for cache hits) | inherits `V2_MODEL_ID` |
| `V2_UNDERSTAND_CACHE_TTL` | Cache TTL for the understanding prompt | `1h` |
| `V2_SHARD_MODEL_ID` | Fan-out model (regional, for cache hits) | `us.anthropic.claude-haiku-4-5-...` |
| `V2_REDUCE_MODEL_ID` | Rerank model | inherits `V2_MODEL_ID` |
| `V2_FANOUT_K` | Titles requested per shard | `3` |
| `V2_FANOUT_TIMEOUT_S` | Fan-out timeout | `25` |
| `V2_CATCHALL_SIZE` | Candidates fetched by the catch-all path | `35` |
| `V2_FINAL_TOP_N` | Titles returned to the client | `5` |
| `V2_SHARD_CACHE_TTL` | Shard prompt cache TTL (must match the warmer) | `1h` |
| `TASTE_K` / `TASTE_WEIGHT` | Taste-centroid search size and blend weight | `20` / `0.3` |
| `WATCHED_EXCLUDE_DAYS` / `WATCHED_EXCLUDE_MAX` | Watched-title exclusion window and cap | `180` / `200` |
| `AGENTCORE_MEMORY_ID` | Memory resource | set by the deployment |

Prompt caching requires **regional** model IDs; cross-region (`global.*`) IDs
route across regions and prevent cache hits.
