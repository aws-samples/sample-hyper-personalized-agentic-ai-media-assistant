"""Run the REAL clips-feed ranking (infra/lambdas/clips_feed, bundled unchanged)
on a sandbox profile and record each stage.

This is `lambda_function.build` with its three I/O points replaced: member
context comes from the sandbox, the session starts empty (plus the what-if
reactions as card reports), and nothing is saved. Search, filters, pools and
the selection policy are the feed's own functions.
"""

import time

import lambda_function as feed   # bundled copy of infra/lambdas/clips_feed/lambda_function.py
from lambda_function import policy

PAGES = 2   # first page + one refill, like the start of a real session


def _ids(titles):
    return [t["id"] for t in titles]


def run(sandbox, reactions, seed="whatif"):
    """reactions = [(title_id, feedback)] reported as cards in this session."""
    now = time.time()
    started = now
    priors = policy.decode_centroids(sandbox.affinities.get("centroids"))
    declared = sandbox.declared
    watched = sandbox.completed()
    fc = feed.filters(declared)

    updates = [{"item_id": t, "dwell_ms": 12000, "feedback": v} for t, v in reactions]
    impressions = policy.merge_impressions({}, updates, now)
    seen = feed.titles_by_id(list(impressions))
    positives, negatives = policy.rebuild(impressions, seen, now)
    ints = policy.interests(positives, priors)

    exclude = set(watched) | set(impressions)
    hits = [feed.knn(x["v"], fc, policy.PRIOR_SEARCH_K if x.get("prior") else policy.SESSION_SEARCH_K, exclude)
            for x in ints]
    mds = feed.microdramas(fc)
    top = feed.discovery(fc, priors, exclude)

    pages, issued = [], set()
    for page in range(PAGES):
        picks = policy.select(ints, negatives, hits, mds, top, policy.BATCH, exclude | issued,
                              seed=f"{seed}:{len(issued)}")
        pages.append([{"id": t["id"], "title": t["title"], "kind": t["kind"], "reason": reason,
                       "why": why.get("summary"), "relevance": why.get("relevance"),
                       "microdrama_boost": why.get("microdrama_boost")} for t, reason, why in picks])
        issued |= {t["id"] for t, _, _ in picks}

    confidence = max((x.get("strength", x["w"]) for x in ints), default=0.0)
    return {
        "interests": [{"kind": x["kind"], "weight": round(x["w"], 4), "strength": round(x.get("strength", x["w"]), 4),
                       "prior": bool(x.get("prior")), "sources": x.get("sources") or [],
                       "vector": x["v"], "candidates": _ids(h)} for x, h in zip(ints, hits)],
        "negatives": [{"weight": round(n["w"], 4), "sources": n.get("sources") or []} for n in negatives],
        "microdramas": _ids(mds), "discovery": _ids(top)[:policy.DISCOVERY_POOL],
        "excluded": sorted(exclude), "filters": fc, "confidence": round(confidence, 4),
        "personalized_slots": min(policy.BATCH - 1, int(policy.BATCH * confidence + 0.5)),
        "pages": pages, "seconds": round(time.time() - started, 2),
    }
