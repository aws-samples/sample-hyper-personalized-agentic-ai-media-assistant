"""Fan-out retrieval path: parallel LLM reads across cached catalog shards.

Used for pure mood/genre queries (no hard filter). Each shard's ~28k-token
system prompt is cached at Bedrock (ttl 1h). Keeping that cache warm is handled
externally by a scheduled Lambda (see backend/shard-warmer/), NOT by this code —
so fan_out just fires the parallel shard calls and relies on the warm cache.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, wait

from config import SHARD_MODEL_ID, SHARD_CACHE_TTL, FANOUT_K, FANOUT_TIMEOUT_S
from llm import call, cached_system
from catalog import (SHARD_TEXT, SHARD_TITLES, MOVIE_TYPES, TV_TYPES,
                     select_shards, candidate_line)

# Shard output lines look like: "Quantum Storm [tt1001001] | 2001 | ...".
TITLE_RE = re.compile(r"^(.*?)\s*\[(tt[0-9a-zA-Z]+)\]")


def parse_titles(answer: str) -> list[tuple[str, str]]:
    """Extract (title, title_id) pairs from a shard's answer."""
    out = []
    for ln in answer.splitlines():
        m = TITLE_RE.match(ln.strip())
        if m:
            out.append((m.group(1).strip(), m.group(2)))
    return out


def _shard_system(sid: str):
    return cached_system(SHARD_TEXT[sid], ttl=SHARD_CACHE_TTL)


def fan_out(query: str, category: str, k: int = FANOUT_K, kids: bool = False) -> dict:
    """Fan out the query across a category's shards and merge their picks.

    kids=True reads only the kids shard and keeps the picks in the requested
    format (the kids shard mixes movies, series and shorts). Any id a shard
    returns that is not in the shards asked is dropped, so a made-up id can
    never bring in a title outside them.

    Returns {"candidates": {tid: line}, "path_s": s, "n_shards": n, "usage": dict}.
    The shard prompt caches are kept warm by the external shard-warmer Lambda,
    so this fires the real shard calls directly (they hit the warm cache).
    """
    from llm import blank_usage, accumulate

    shards = select_shards(category, kids=kids)
    allowed: dict[str, str] = {}
    for sid in shards:
        allowed.update(SHARD_TITLES.get(sid, {}))
    types = (MOVIE_TYPES if category == "movie" else TV_TYPES if category == "tv" else None) if kids else None
    prompt = f"{query} Return up to {k} titles."
    t0 = time.perf_counter()
    # NB: do NOT use `with ThreadPoolExecutor(...)` here. Its __exit__ calls
    # shutdown(wait=True), which blocks until EVERY submitted task finishes —
    # so a single hung shard call (e.g. a Bedrock stream stalled under
    # throttling) would make the whole request hang forever, defeating the
    # FANOUT_TIMEOUT_S wait() below. Manage the pool manually and, after the
    # timed wait, tear it down without blocking on stragglers.
    ex = ThreadPoolExecutor(max_workers=max(len(shards), 1))
    try:
        futs = {ex.submit(call, _shard_system(sid), prompt,
                          SHARD_MODEL_ID): sid for sid in shards}
        done, _ = wait(futs, timeout=FANOUT_TIMEOUT_S)
    finally:
        # cancel_futures drops not-yet-started tasks; wait=False means we never
        # block on a straggler. A still-running thread will finish and be GC'd
        # harmlessly (its result is simply ignored).
        ex.shutdown(wait=False, cancel_futures=True)
    elapsed = time.perf_counter() - t0

    candidates: dict[str, str] = {}
    usage = blank_usage()
    n_failed = len(futs) - len(done)  # timed out
    first_err = None
    for fut in done:
        try:
            answer, call_usage, _ = fut.result()
        except Exception as e:
            n_failed += 1
            first_err = first_err or repr(e)
            continue
        accumulate(usage, call_usage)
        for title, tid in parse_titles(answer):
            if allowed and tid not in allowed:
                continue  # not in the shards asked: an invented or foreign id
            if types is not None and allowed.get(tid) not in types:
                continue  # kids shard pick in the wrong format
            if tid not in candidates:
                candidates[tid] = candidate_line(tid, title)

    # Enrich with genres + plot so the reranker can judge relevance (the shard
    # output only carries title/id). One batch OpenSearch fetch for all picks;
    # on any failure we keep the acclaim-only lines above (graceful degrade).
    if candidates:
        try:
            from retrieval import fetch_records_by_ids
            recs = fetch_records_by_ids(list(candidates.keys()))
            for tid, r in recs.items():
                candidates[tid] = candidate_line(
                    tid, r.get("originalTitle", tid),
                    str(r.get("premiere_date", ""))[:4], r.get("imdb_rating", ""),
                    r.get("genres"), r.get("plot"),
                )
        except Exception as e:
            print(f"[fanout] candidate genre/plot enrich failed: {e}")

    if n_failed:
        # Surface shard failures (e.g. Bedrock throttling) instead of silently
        # returning fewer/zero candidates.
        print(f"[fanout] {n_failed}/{len(shards)} shard calls failed"
              f"{f' (e.g. {first_err})' if first_err else ' (timed out)'}")
    return {"candidates": candidates, "path_s": round(elapsed, 3),
            "n_shards": len(shards), "n_failed": n_failed, "usage": usage}
