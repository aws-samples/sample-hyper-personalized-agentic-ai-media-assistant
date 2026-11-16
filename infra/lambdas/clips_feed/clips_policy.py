"""Clips feed ranking: session interests, long-term taste, greedy diversity.

Explained with diagrams in docs/how-personalization-works.md §5.4.

A port of the Personalized-Feed-POC selection policy (backend/feedpoc/policy.py
on branch codex/personalized-feed-poc) onto this project's catalog:

- signal: per card, dwell from 2 s to 10 s ramps to +0.3; like +1, more like
  this +1.5, less like this -1. A quick swipe is neutral, not a dislike.
- rebuild: session interests are rebuilt from the session's impressions on
  every request (at most 64, 10-minute half-life), up to 4 positive and 4
  negative vectors; a card within cosine MERGE_COSINE of a vector joins it
  (moving it by at most 0.25), otherwise it starts a new one. Nothing derived
  is stored: the session item keeps only the impressions (facts).
- priors: the member's L2 taste centroids (AFFINITIES.centroids) replace the
  POC's Bedrock preference reasoning. Without session evidence they drive all
  queries; alongside it they get one query at weight <= 0.2, but they still
  count toward how many cards are personalized.
- discovery: sampled from the neighbourhood of the member's taste centroids
  (award-listed titles only, since the catalog has no vote counts), not the
  global top-rated list, which is the fallback for a member with no taste.
- select: relevance = interest weight x similarity, minus a penalty for being
  close to a negative vector; round(batch x confidence) personalized slots,
  always one discovery slot; a greedy pass subtracts 0.2 x similarity to the
  cards already chosen and 0.08 for a repeated genre.
- microdramas (catalog title_type `micro-drama`) are always candidates and
  their score is multiplied by MD_BOOST, so they lead the feed; the other
  cards are catalog titles shown as posters.

Pure functions only: catalog search is passed in, so the policy is unit-tested
without AWS.
"""

import base64
import math
import random
import struct

BATCH = 6
IMPRESSIONS_MAX = 64
SESSION_HALF_LIFE_S = 600
# The POC merged at cosine 0.7. In this catalog's Nova space most titles are
# 0.66-0.77 apart (measured), so 0.7 folds unrelated cards into one interest;
# 0.8 merges only titles that are genuinely close.
MERGE_COSINE = 0.8
MAX_MERGE_ALPHA = 0.25
MAX_VECTORS = 4
PRIOR_MAX_W = 0.6
PRIOR_SHARE_W = 0.2
MIN_INTEREST_W = 0.02
SESSION_SEARCH_K = 40
PRIOR_SEARCH_K = 20
DISCOVERY_POOL = 80
SEMANTIC_POOL = 120
MD_BOOST = 1.5
NEGATIVE_PENALTY = 0.3
REDUNDANCY = 0.2
GENRE_REPEAT = 0.08
FEEDBACK_W = {"none": 0.0, "like": 1.0, "more": 1.5}
FEEDBACK_VALUES = {"none", "like", "more", "dislike"}
STRONG_FEEDBACK = {"more", "dislike"}


# --- vectors ----------------------------------------------------------------

def unit(v):
    n = math.sqrt(_dot(v, v))
    return [x / n for x in v] if n else list(v)


def _dot(a, b):
    return math.sumprod(a, b) if hasattr(math, "sumprod") else sum(x * y for x, y in zip(a, b))


def cosine(a, b):
    return _dot(unit(a), unit(b))


def sim01(a, b):
    """Cosine mapped to [0, 1]."""
    return (cosine(a, b) + 1) / 2


def usim01(a, b):
    """sim01 for vectors that are already unit length (no re-normalizing)."""
    return (_dot(a, b) + 1) / 2


def decay(at, now, half_life=SESSION_HALF_LIFE_S):
    return math.pow(2.0, -max(0.0, now - at) / half_life)


def decode_centroids(stored):
    """AFFINITIES.centroids ([{"v": base64 float32, "w", "n"}]) -> [(weight, vector)]."""
    out = []
    for c in stored or []:
        try:
            raw = base64.b64decode(c["v"])
            out.append((float(c.get("w") or 0), list(struct.unpack(f"<{len(raw) // 4}f", raw))))
        except Exception:  # noqa: BLE001 -- a malformed centroid is skipped
            continue
    return out


# --- impressions --------------------------------------------------------------

def signal(imp):
    """POC policy.signal: dwell 2-10 s ramps to 0.3; feedback dominates."""
    if imp.get("feedback") == "dislike":
        return -1.0
    dwell = min(0.3, max(0.0, (float(imp.get("dwell_ms") or 0) - 2000) / 8000 * 0.3))
    return dwell + FEEDBACK_W.get(imp.get("feedback") or "none", 0.0)


def merge_impressions(stored, updates, now):
    """Fold client card reports into the session's impressions (dict id -> imp).

    Dwell is cumulative per card, so the larger value wins (a retried report
    cannot inflate it); the latest feedback wins, and "none" clears it (an
    un-like). Keeps the newest IMPRESSIONS_MAX by order.
    """
    imps = {k: dict(v) for k, v in (stored or {}).items()}
    order = max((int(v.get("order") or 0) for v in imps.values()), default=0)
    for u in updates or []:
        item = u.get("item_id")
        if not item:
            continue
        imp = imps.get(item)
        if imp is None:
            order += 1
            imp = {"dwell_ms": 0, "feedback": "none", "order": order}
        imp["dwell_ms"] = max(int(imp.get("dwell_ms") or 0), int(u.get("dwell_ms") or 0))
        if u.get("feedback") in FEEDBACK_VALUES:
            imp["feedback"] = u["feedback"]
        imp["at"] = now
        imps[item] = imp
    newest = sorted(imps.items(), key=lambda kv: -int(kv[1].get("order") or 0))[:IMPRESSIONS_MAX]
    return dict(newest)


def rebuild(impressions, titles, now):
    """Session interests from impressions: (positives, negatives), each a list of
    {"v", "w", "at", "sources"}. `titles` = {id: title dict with "vector"}."""
    positives, negatives = [], []
    for item, imp in sorted(impressions.items(), key=lambda kv: int(kv[1].get("order") or 0)):
        title = titles.get(item)
        w = signal(imp)
        if not title or not title.get("vector") or not w:
            continue
        target = negatives if w < 0 else positives
        w = abs(w) * decay(float(imp.get("at", now)), now)
        v = unit(title["vector"])
        match = max(target, key=lambda x: cosine(x["v"], v), default=None)
        if match and cosine(match["v"], v) >= MERGE_COSINE:
            alpha = min(MAX_MERGE_ALPHA, w / (match["w"] + w))
            match["v"] = unit([(1 - alpha) * a + alpha * b for a, b in zip(match["v"], v)])
            name = title.get("title")
            match["sources"] = ([s for s in match["sources"] if s != name] + [name])[-6:]
            match["contrib"][name] = match["contrib"].get(name, 0.0) + w
            match["w"] += w
            match["at"] = now
        else:
            if len(target) == MAX_VECTORS:
                target.remove(min(target, key=lambda x: x["w"]))
            target.append({"v": v, "w": w, "at": now, "sources": [title.get("title")],
                           "contrib": {title.get("title"): w}})
    return positives, negatives


def strongest_source(interest):
    """The title that contributed most weight to a session interest."""
    contrib = interest.get("contrib") or {}
    if contrib:
        return max(contrib.items(), key=lambda kv: kv[1])[0]
    return (interest.get("sources") or [None])[-1]


def interests(positives, priors):
    """Retrieval interests, strongest first. `priors` = [(weight, vector)] from L2.

    Each interest's `strength` is what it says about the member regardless of
    how retrieval budget is shared: the long-term prior keeps its full strength
    there even when it gets only one bounded query beside session interests,
    so a member with known taste gets a mostly personalized batch from the
    first page."""
    current = [{"kind": "session", "v": p["v"], "w": p["w"] / (1 + p["w"]), "sources": p["sources"],
                "contrib": p.get("contrib") or {}} for p in positives]
    current.sort(key=lambda x: -x["w"])
    prior = [{"kind": "taste", "v": unit(v), "w": min(PRIOR_MAX_W, PRIOR_MAX_W * w), "sources": []}
             for w, v in priors or []]
    prior = sorted([p for p in prior if p["w"] >= MIN_INTEREST_W], key=lambda x: -x["w"])
    for x in current + prior:
        x["strength"] = x["w"]
    if not current:
        return prior[:MAX_VECTORS]
    if prior:
        return current[:MAX_VECTORS - 1] + [{**prior[0], "w": min(PRIOR_SHARE_W, prior[0]["w"]), "prior": True}]
    return current[:MAX_VECTORS]


# --- selection ----------------------------------------------------------------

def is_microdrama(t):
    return t.get("kind") == "microdrama"


def _explain(kind, winner=None, md=False):
    if kind == "session" and winner and strongest_source(winner):
        summary = f"Because you watched {strongest_source(winner)}"
    elif kind == "session":
        summary = "Matches what you're watching now"
    elif kind == "taste":
        summary = "Matches your taste"
    else:
        summary = "Something different to explore"
    return {"kind": kind, "summary": summary, "microdrama_boost": md}


def select(ints, negatives, hits, microdramas, discovery, count, exclude, anchors=(), seed=""):
    """Choose `count` cards. Returns [(title, reason, explanation)].

    `hits[i]` = catalog titles near `ints[i]` (searched with the member's
    filters, SESSION_SEARCH_K or PRIOR_SEARCH_K of them); `microdramas` and
    `discovery` are candidate lists; `exclude` ids are never chosen; `anchors`
    (cards already queued) count for diversity.
    """
    if count <= 0:
        return []
    exclude = set(exclude)
    pool = {}
    for found in hits:
        for t in found:
            if t["id"] not in exclude and t.get("vector"):
                pool.setdefault(t["id"], t)
    mds = [t for t in microdramas if t["id"] not in exclude and t.get("vector")]
    for t in mds:
        pool.setdefault(t["id"], t)

    neg = [(unit(n["v"]), min(1.0, n["w"])) for n in negatives]
    ints = [{**x, "v": unit(x["v"])} for x in ints]
    # Every score below compares the same few hundred titles against the same
    # interests many times over; normalize each vector once and memoize the
    # per-title scores so the loop is plain dot products.
    _uv, _rel, _disc = {}, {}, {}

    def vec(t):
        if t["id"] not in _uv:
            _uv[t["id"]] = unit(t["vector"])
        return _uv[t["id"]]

    def penalty(t):
        return NEGATIVE_PENALTY * max((w * usim01(v, vec(t)) for v, w in neg), default=0.0)

    def boost(t, score):
        return score * MD_BOOST if is_microdrama(t) and score > 0 else score

    def relevance(t):
        if not ints:
            return discovery_score(t)
        if t["id"] not in _rel:
            _rel[t["id"]] = boost(t, max(0.0, max(x["w"] * usim01(x["v"], vec(t)) for x in ints) - penalty(t)))
        return _rel[t["id"]]

    def discovery_score(t):
        if t["id"] not in _disc:
            _disc[t["id"]] = boost(t, min(1.0, max(0.0, float(t.get("rating") or 0) / 10)) * 0.1 - penalty(t))
        return _disc[t["id"]]

    confidence = max((x.get("strength", x["w"]) for x in ints), default=0.0)
    personalized = min(count - 1 if count > 1 else count, int(count * confidence + 0.5))
    explore = [t for t in discovery if t["id"] not in exclude and t.get("vector")]
    random.Random(seed).shuffle(explore)
    discovery_pool = sorted(explore[:DISCOVERY_POOL] + mds, key=lambda t: (-discovery_score(t), t["id"]))
    discovery_pool = list({t["id"]: t for t in discovery_pool}.values())
    semantic_pool = sorted(pool.values(), key=lambda t: (-relevance(t), t["id"]))[:SEMANTIC_POOL]

    chosen, used, picked = [], [a for a in anchors if a.get("vector")], set()
    genres = {g for a in anchors for g in a.get("genres") or []}
    for slot in range(count):
        personalizing = slot < personalized
        eligible = [t for t in (semantic_pool if personalizing else discovery_pool) if t["id"] not in picked]
        if not eligible:
            personalizing = not personalizing
            eligible = [t for t in (semantic_pool if personalizing else discovery_pool) if t["id"] not in picked]
        if not eligible:
            break

        def score(t):
            redundancy = max((usim01(vec(t), vec(a)) for a in used), default=0.0)
            repeat = GENRE_REPEAT if set(t.get("genres") or []) & genres else 0.0
            return (relevance(t) if personalizing else discovery_score(t)) - REDUNDANCY * redundancy - repeat

        best = min(eligible, key=lambda t: (-score(t), t["id"]))
        picked.add(best["id"])
        if personalizing and ints:
            winner = max(ints, key=lambda x: x["w"] * usim01(x["v"], vec(best)))
            explanation = _explain(winner["kind"], winner, is_microdrama(best))
            reason = "for-you"
        else:
            explanation = _explain("discovery", md=is_microdrama(best))
            reason = "explore" if ints else "discover"
        explanation["relevance"] = round(relevance(best), 4)
        chosen.append((best, reason, explanation))
        used.append(best)
        genres.update(best.get("genres") or [])
    return chosen


def keep_count(queued, updates):
    """Queued cards the client keeps after a refill: all of them, or only the
    next one after strong feedback (more like this / less like this), so the
    rest of the queue is replaced and the reaction shows up on the next swipe."""
    strong = any((u.get("feedback") in STRONG_FEEDBACK) for u in updates or [])
    return min(1, len(queued)) if strong else len(queued)
