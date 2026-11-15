"""Shard prompt-cache warmer Lambda.

Keeps the Bedrock prompt cache for the movie-ai-agent-v2 fan-out shards warm.
Each shard's ~28k-token system prompt is cached at Bedrock (ttl 1h, server-side,
cross-container, content-keyed). This function fires one tiny converse per shard
with the IDENTICAL system blocks + cachePoint + model that the agent's fan-out
uses, so it refreshes the same cache entries. Scheduled by EventBridge to run
under the 1h TTL.

Env:
  SHARD_MODEL_ID   default us.anthropic.claude-haiku-4-5-20251001-v1:0
  SHARD_CACHE_TTL  default "1h"
"""

import concurrent.futures
import json
import os
import time
from pathlib import Path

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-1")
# Must match backend/movie-ai-agent-v2/config.py SHARD_MODEL_ID + SHARD_CACHE_TTL
# exactly, or the cache key won't match the agent's shard calls.
MODEL_ID = os.environ.get("SHARD_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
CACHE_TTL = os.environ.get("SHARD_CACHE_TTL", "1h")
# Haiku 5.x rejects temperature and thinks by default; match the agent (llm.py).
if "claude-haiku-5" in MODEL_ID:
    _REQUEST_SETTINGS = {"inferenceConfig": {"maxTokens": 8},
                         "additionalModelRequestFields": {"thinking": {"type": "disabled"}}}
else:
    _REQUEST_SETTINGS = {"inferenceConfig": {"maxTokens": 8, "temperature": 0}}

SHARDS_DIR = Path(__file__).parent / "shards"
_manifest = json.loads((SHARDS_DIR / "manifest.json").read_text())
SHARD_TEXT = {
    s["shard"]: (SHARDS_DIR / Path(s["system_txt"]).name).read_text(encoding="utf-8")
    for s in _manifest["shards"]
}

_bedrock = boto3.client(
    "bedrock-runtime", region_name=REGION,
    config=Config(retries={"max_attempts": 4, "mode": "adaptive"}, max_pool_connections=50),
)


def _warm_one(shard_text: str) -> tuple[int, int]:
    """Fire one converse to (re)warm a shard's cached system prefix.

    System blocks + model MUST match the agent's fan-out call so this hits the
    same cache entry. The user message is irrelevant (cache is on the prefix
    up to the cachePoint)."""
    resp = _bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": shard_text}, {"cachePoint": {"type": "default", "ttl": CACHE_TTL}}],
        messages=[{"role": "user", "content": [{"text": "Recommend one title."}]}],
        **_REQUEST_SETTINGS,
    )
    u = resp.get("usage", {})
    return u.get("cacheReadInputTokens", 0), u.get("cacheWriteInputTokens", 0)


def handler(event=None, context=None):
    t0 = time.time()
    reads = writes = failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SHARD_TEXT)) as ex:
        futs = {ex.submit(_warm_one, txt): sid for sid, txt in SHARD_TEXT.items()}
        for fut in concurrent.futures.as_completed(futs):
            try:
                r, w = fut.result()
                reads += r
                writes += w
            except Exception as e:
                failed += 1
                print(f"[warmer] shard {futs[fut]} failed: {e!r}")
    result = {
        "shards": len(SHARD_TEXT),
        "warmed": len(SHARD_TEXT) - failed,
        "failed": failed,
        "cache_read": reads,
        "cache_write": writes,
        "elapsed_s": round(time.time() - t0, 1),
    }
    print(f"[warmer] {json.dumps(result)}")
    return result


if __name__ == "__main__":
    print(handler())
