"""Strands LLM helper for the v2 pipeline.

Wraps Strands `Agent` + `BedrockModel` so every pipeline stage can make a
single-turn, stateless model call while preserving Bedrock prompt caching via
an explicit `cachePoint` in the system prompt.

Why Strands (not raw converse): it simplifies the call surface (build agent →
call → `str(result)`), and caching still works because we pass the system
prompt as content blocks with a `cachePoint`, exactly like the v1 agent does.

Usage extraction mirrors v1: `result.metrics.accumulated_usage` exposes
`inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens`.
"""

from __future__ import annotations

import time

from botocore.config import Config
from strands import Agent
from strands.models import BedrockModel

from config import AWS_REGION, V2_TEMPERATURE, V2_MAX_TOKENS

# Boto pool must be wide enough for the parallel shard fan-out (up to ~18
# concurrent Bedrock calls). Adaptive retries smooth over throttling.
# Hard connect/read timeouts are REQUIRED: without them a Bedrock stream that
# stalls (common under throttling when many shards fire large cache-write calls
# at once) never returns, and the fan-out executor would hang the whole request.
# read_timeout is kept below FANOUT_TIMEOUT_S (25s) so a stalled shard call
# raises inside the wait() window rather than leaking past it.
_BOTO_CFG = Config(
    retries={"max_attempts": 3, "mode": "adaptive"},
    max_pool_connections=50,
    connect_timeout=5,
    read_timeout=20,
)

# BedrockModel instances are safe to reuse and cheap to keep around; cache by
# (model_id, temperature, max_tokens) so repeated shard calls share one client.
_model_cache: dict[tuple, BedrockModel] = {}


def is_haiku_5(model_id: str) -> bool:
    """Claude Haiku 5.x needs different request settings (see _get_model)."""
    return "claude-haiku-5" in model_id


def _get_model(model_id: str, temperature: float, max_tokens: int) -> BedrockModel:
    key = (model_id, temperature, max_tokens)
    model = _model_cache.get(key)
    if model is None:
        kwargs: dict = {}
        if is_haiku_5(model_id):
            # Haiku 5.5 rejects `temperature` ("deprecated for this model") and
            # thinks adaptively by DEFAULT: hidden reasoning (~2.5x output tokens)
            # overruns the stages' small max_tokens caps. Thinking is turned off
            # explicitly so it behaves like Haiku 4.5 did (validated by evals/).
            kwargs["additional_request_fields"] = {"thinking": {"type": "disabled"}}
        else:
            kwargs["temperature"] = temperature
        model = BedrockModel(
            model_id=model_id,
            region_name=AWS_REGION,
            max_tokens=max_tokens,
            boto_client_config=_BOTO_CFG,
            **kwargs,
        )
        _model_cache[key] = model
    return model


def cached_system(text: str, ttl: str | None = None) -> list[dict]:
    """Build a system prompt as content blocks with a trailing cachePoint.

    The static text before the cachePoint is what Bedrock caches. Pass `ttl`
    (e.g. "1h") for long-lived caches like the shard system prompts; omit it
    for the default (5-minute) cache used by short-lived stages.
    """
    cache_point: dict = {"type": "default"}
    if ttl:
        cache_point["ttl"] = ttl
    return [{"text": text}, {"cachePoint": cache_point}]


def blank_usage() -> dict:
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheWriteInputTokens": 0,
    }


def extract_usage(result) -> dict:
    """Pull token + cache usage from a Strands AgentResult (best-effort)."""
    usage = blank_usage()
    try:
        acc = result.metrics.accumulated_usage
        for k in usage:
            usage[k] = acc.get(k, 0)
    except (AttributeError, TypeError):
        pass
    return usage


def accumulate(totals: dict, usage: dict) -> None:
    """Add one call's usage into running totals in place."""
    for k in ("inputTokens", "outputTokens", "totalTokens",
              "cacheReadInputTokens", "cacheWriteInputTokens"):
        totals[k] = totals.get(k, 0) + usage.get(k, 0)


def call(
    system,
    user_input: str,
    model_id: str,
    temperature: float = V2_TEMPERATURE,
    max_tokens: int = V2_MAX_TOKENS,
) -> tuple[str, dict, float]:
    """Run a single stateless model call and return (text, usage, elapsed_s).

    `system` may be a plain string (sent as-is, uncached) or a list of content
    blocks from `cached_system(...)` (cached). A fresh Agent is created per call
    so no conversation state leaks between pipeline stages.
    """
    model = _get_model(model_id, temperature, max_tokens)
    agent = Agent(model=model, system_prompt=system, callback_handler=None)
    t0 = time.perf_counter()
    result = agent(user_input)
    elapsed = time.perf_counter() - t0
    text = str(result).strip()
    return text, extract_usage(result), elapsed
