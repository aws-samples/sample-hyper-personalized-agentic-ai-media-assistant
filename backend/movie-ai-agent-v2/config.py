"""Configuration for the movie recommendation agent."""

import os
from pathlib import Path

# Load .env file if present (for deploy-time overrides)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# Model configuration
# Default model used as fallback
MODEL_ID = os.environ.get("MODEL_ID", "global.anthropic.claude-sonnet-4-6")
# DEFAULT_ROUTER_MODEL_ID = "us.amazon.nova-micro-v1:0"
DEFAULT_ROUTER_MODEL_ID = "global.amazon.nova-2-lite-v1:0"
# DEFAULT_PLANNER_MODEL_ID = "us.amazon.nova-lite-v1:0"
DEFAULT_PLANNER_MODEL_ID = "us.amazon.nova-2-lite-v1:0"
DEFAULT_EXECUTOR_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
DEFAULT_REFLECTOR_MODEL_ID = "global.amazon.nova-2-lite-v1:0"
DEFAULT_RESPONDER_MODEL_ID = "us.amazon.nova-2-lite-v1:0"
# DEFAULT_RESPONDER_MODEL_ID = "mistral.ministral-3-3b-instruct"
# Per-agent model overrides (fall back to MODEL_ID if not set)
# NOTE: Prompt caching requires REGIONAL model IDs (e.g. us.anthropic.claude-*)
# Cross-region IDs (global.*) route to different regions and prevent cache hits.
ROUTER_MODEL_ID = os.environ.get("ROUTER_MODEL_ID", DEFAULT_ROUTER_MODEL_ID)
PLANNER_MODEL_ID = os.environ.get("PLANNER_MODEL_ID", DEFAULT_PLANNER_MODEL_ID)
EXECUTOR_MODEL_ID = os.environ.get("EXECUTOR_MODEL_ID", DEFAULT_EXECUTOR_MODEL_ID)
REFLECTOR_MODEL_ID = os.environ.get("REFLECTOR_MODEL_ID", DEFAULT_REFLECTOR_MODEL_ID)
RESPONDER_MODEL_ID = os.environ.get("RESPONDER_MODEL_ID", DEFAULT_RESPONDER_MODEL_ID)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "8192"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3"))

# Embedding model configuration
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "amazon.nova-2-multimodal-embeddings-v1:0")
EMBEDDING_DIMENSION = 1024
EMBEDDING_REGION = os.environ.get("EMBEDDING_REGION", AWS_REGION)

# OpenSearch Serverless configuration
OPENSEARCH_HOST = os.environ.get("OPENSEARCH_HOST", "")
OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "catalog")

# Thinking budget for extended thinking (Claude models)
# Set to 0 or remove to disable extended thinking
THINKING_BUDGET_DEFAULT = int(os.environ.get("THINKING_BUDGET_DEFAULT", "0"))
THINKING_BUDGET_EXECUTOR = int(os.environ.get("THINKING_BUDGET_EXECUTOR", "0"))
THINKING_BUDGET_PLANNER = int(os.environ.get("THINKING_BUDGET_PLANNER", "0"))

# DynamoDB configuration
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "movie-agent-user-profiles")
# Maps a Cognito sub to its household members; used to verify a profile_id.
HOUSEHOLDS_TABLE = os.environ.get("HOUSEHOLDS_TABLE", "")

# AgentCore Memory configuration
AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")

# Long-term memory (custom User Preference strategy) retrieval cache TTL, seconds.
# The per-session object caches the retrieved preferences for this long, then
# refetches. On a long-lived / pinned booth session this is what lets newly
# consolidated preferences show up mid-demo instead of being frozen at the first
# fetch. Keep it >= typical consolidation lag so we don't refetch pointlessly.
LTM_CACHE_TTL_S = int(os.environ.get("LTM_CACHE_TTL_S", "30"))

# Session exclusion window. Titles shown this session are not recommended
# again: newest first, capped at this many ids so the oldest picks drop off and
# can resurface (about three batches). A title asked for by name is exempt.
MAX_EXCLUDE_IDS = int(os.environ.get("MAX_EXCLUDE_IDS", "15"))

# Multi-turn context window. The understanding step collapses recent turns +
# the latest message into one standalone query. Cap the history it sees to the
# most recent N turns so the prompt stays small and stale constraints from far
# earlier in a long (demo) session don't linger.
MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "5"))

# Watched-title exclusion. Titles the member completed (>= ~90%, set by the
# writer of TITLE# items) within this many days are not recommended back,
# unless the member names the title. Capped so OpenSearch queries stay small.
INTERACTIONS_TABLE = os.environ.get("INTERACTIONS_TABLE", "")
WATCHED_EXCLUDE_DAYS = int(os.environ.get("WATCHED_EXCLUDE_DAYS", "180"))
WATCHED_EXCLUDE_MAX = int(os.environ.get("WATCHED_EXCLUDE_MAX", "200"))
# L2 taste text, centroids and watch state are re-read this often in a session
# (design §6.3 CONTEXT_TTL_S), so viewing picked up by the stream processor shows.
PROFILE_TTL_S = int(os.environ.get("PROFILE_TTL_S", "60"))
# Behavioral pipeline (docs/unified-profile-design.md §7): the agent reads L1
# SESSION#behavior facts each turn and writes `impression` events here.
PROFILE_EVENTS_STREAM = os.environ.get("PROFILE_EVENTS_STREAM", "")

# L2 taste centroids in retrieval (design §10.2 step 2). Catch-all adds one KNN
# list per centroid (TASTE_K hits each), fused at TASTE_WEIGHT relative to the
# request's own lists; skipped when the request names a person, title or year.
# "Surprise me" retrieves from the centroids alone (SURPRISE_POOL candidates).
TASTE_K = int(os.environ.get("TASTE_K", "20"))
TASTE_WEIGHT = float(os.environ.get("TASTE_WEIGHT", "0.3"))
SURPRISE_POOL = int(os.environ.get("SURPRISE_POOL", "35"))

# Agent loop configuration
MAX_REFLECTION_RETRIES = int(os.environ.get("MAX_REFLECTION_RETRIES", "3"))

# Tracing (set to "true" to enable trace file output)
ENABLE_TRACING = os.environ.get("ENABLE_TRACING", "false").lower() in ("true", "1", "yes")


# ============================================================================
# movie-ai-agent-v2 pipeline configuration
# ============================================================================
# Pipeline: understand -> taste | fan-out | catch-all -> reduce.
#
# Prompt caching requires REGIONAL model IDs (us.anthropic.claude-*): caches are
# per-region, so global.* inference profiles (cross-region routing) drop the hit
# rate. The light stages (intent/reduce) are under Haiku's ~4k
# cache minimum and run uncached anyway, so they default to the GLOBAL id for
# cross-region latency resilience. The shard/fan-out executor stays REGIONAL
# (see SHARD_MODEL_ID) to keep its ~28k-token prompt cacheable.

# Default model for the light pipeline stages (env-overridable per stage).
DEFAULT_V2_MODEL_ID = os.environ.get(
    "V2_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0"
)
INTENT_MODEL_ID = os.environ.get("V2_INTENT_MODEL_ID", DEFAULT_V2_MODEL_ID)

# Skip the chitchat/intent gate (default on). When the agent is called as a tool
# by Nova Sonic, the caller has already decided a recommendation is needed, so
# the gate is redundant and just adds a round-trip (~0.8s). Set SKIP_INTENT=false
# to re-enable it for callers that pass raw user text (greetings, off-topic, etc.).
SKIP_INTENT = os.environ.get("SKIP_INTENT", "true").lower() in ("true", "1", "yes")
# Understanding step (understand.py): one call that turns the message + history
# into a standalone query and routing fields. Its ~5.4k-token
# system prompt is cached; keep the model id REGIONAL (us.*) for cache hits.
UNDERSTAND_MODEL_ID = os.environ.get("V2_UNDERSTAND_MODEL_ID", DEFAULT_V2_MODEL_ID)
# Cache TTL for that prompt: "1h" survives demo gaps (higher write price than the
# default 5 min, but writes are rare); set "" for the 5-minute default.
UNDERSTAND_CACHE_TTL = os.environ.get("V2_UNDERSTAND_CACHE_TTL", "1h")
# Shard/fan-out executor: kept REGIONAL so its ~28k-token system prompt stays
# cacheable (global.* would scatter across regions and drop the cache hit rate).
# Set V2_SHARD_MODEL_ID=global.anthropic.claude-haiku-4-5-20251001-v1:0 to force
# the shard stage onto global too (trades cache hits for cross-region routing).
SHARD_MODEL_ID = os.environ.get("V2_SHARD_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
REDUCE_MODEL_ID = os.environ.get("V2_REDUCE_MODEL_ID", DEFAULT_V2_MODEL_ID)

# Per-stage generation limits. Pipeline stages are terse (JSON / short lists),
# so cap output tokens low to keep latency down.
V2_TEMPERATURE = float(os.environ.get("V2_TEMPERATURE", "0.3"))
V2_MAX_TOKENS = int(os.environ.get("V2_MAX_TOKENS", "1024"))

# Shard fan-out.
SHARD_DIR = Path(__file__).parent / "shards"
FANOUT_K = int(os.environ.get("V2_FANOUT_K", "3"))          # titles requested per shard
FANOUT_TIMEOUT_S = float(os.environ.get("V2_FANOUT_TIMEOUT_S", "25"))
SHARD_CACHE_TTL = os.environ.get("V2_SHARD_CACHE_TTL", "1h")  # cachePoint ttl on shard prompts

# Catch-all retrieval.
CATCHALL_SIZE = int(os.environ.get("V2_CATCHALL_SIZE", "35"))  # candidates fetched before reduce
RRF_K = int(os.environ.get("V2_RRF_K", "60"))                  # reciprocal-rank-fusion constant

# Query-side embedding purpose for the catch-all KNN (validated choice).
# Independent of the doc-side index embeddings; TEXT_RETRIEVAL is the query pair.
EMBED_QUERY_PURPOSE = os.environ.get("V2_EMBED_QUERY_PURPOSE", "TEXT_RETRIEVAL")

# Final number of recommendations returned to the client.
FINAL_TOP_N = int(os.environ.get("V2_FINAL_TOP_N", "5"))


# ============================================================================
# Scene analysis (request_type="analysis") configuration
# ============================================================================
# analysis_agent.py reads these directly from os.environ with the same defaults;
# they're mirrored here as the single place to see/override them. The runtime's
# execution role (shared with v1) already grants the needed permissions:
# analysis AOSS collection, Bedrock KB Retrieve/RetrieveAndGenerate, cross-region
# rerank (us-west-2), Rekognition, and S3 GetObject on the scene bucket.
ANALYSIS_OSS_HOST = os.environ.get("ANALYSIS_OSS_HOST", "")
BEDROCK_KNOWLEDGE_BASE_ID = os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID", "")
BDA_VIDEO_CHAPTER_INDEX_NAME = os.environ.get("BDA_VIDEO_CHAPTER_INDEX_NAME", "chapters")
BDA_VIDEO_CHAPTER_SEGMENT_INDEX_NAME = os.environ.get("BDA_VIDEO_CHAPTER_SEGMENT_INDEX_NAME", "segments")
BDA_VIDEO_CHAPTER_AUDIO_SEGMENT_INDEX_NAME = os.environ.get("BDA_VIDEO_CHAPTER_AUDIO_SEGMENT_INDEX_NAME", "audio-segments")
MOVIE_SCENE_UNDERSTANDING_MODEL_ID = os.environ.get("MOVIE_SCENE_UNDERSTANDING_MODEL_ID", "global.amazon.nova-2-lite-v1:0")
MOVIE_SCENE_UNDERSTANDING_RERANKING_MODEL_ARN = os.environ.get(
    "MOVIE_SCENE_UNDERSTANDING_RERANKING_MODEL_ARN",
    "arn:aws:bedrock:us-west-2::foundation-model/cohere.rerank-v3-5:0",
)
SCENE_IMAGE_S3_BUCKET = os.environ.get("SCENE_IMAGE_S3_BUCKET", "")
