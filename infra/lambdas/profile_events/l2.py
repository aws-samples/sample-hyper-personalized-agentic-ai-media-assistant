"""L2 long-term taste (`AFFINITIES`): pure functions, no I/O.

Explained for non-specialists, with diagrams and worked examples, in
docs/how-personalization-works.md §2-3.

docs/unified-profile-design.md §7.4 steps 4 and 6, fed from L1 rather than
from raw events: the processor compares the L1 state before and after a fold
(`signals`), and only what L1 newly recorded is scored into L2 -- a finished
view, an agent pick opened, a My List add, explicit feedback (like / more /
dislike). Heartbeats and impressions never reach L2. An opened agent pick is
interest only (a small favorites step, no evidence, no centroid move), and a
title's other signals combine into one contribution (`fold_contribution`).

Deliberate changes from the design:
- a long-form view with less than MIN_SCORED_LONG_S watched is not scored at
  all: a few seconds of a two-hour film is sampling, not an abandon;
- there is no `agent_reject` (rejections reach long-term memory through the
  conversation);
- L2 is written on every scored signal (views finish a few times a session),
  not throttled to once a minute.
"""

import base64
import math
import struct

from contract import is_short_form

ALPHA = 0.15                     # learning rate
PARENT_FACTOR = 0.5              # entities reached through parent_item_id get alpha/2
HALF_LIFE_DAYS = {"genre": 30, "person": 60}
DEFAULT_HALF_LIFE_DAYS = 30
MIN_SCORED_LONG_S = 120          # long-form views shorter than this are not scored

# Short-form cards (clips, microdramas, poster title cards), after the POC feed
# policy (Personalized-Feed-POC policy.signal): a card watched under
# CARD_MIN_S is a swipe -- neutral, not a dislike; dwell from CARD_MIN_S to
# CARD_DWELL_FULL_S ramps up to CARD_DWELL_E; completion is measured against
# the card's length, capped at CARD_MAX_S, so a signal lands well before 2 min.
CARD_MIN_S = 2
CARD_DWELL_FULL_S = 10
CARD_DWELL_E = 0.3
CARD_MAX_S = 120
CARD_HALF_E, CARD_FULL_E, CARD_REPLAY_E = 0.4, 0.6, 0.8

# Opening a search result is interest, not taste: a small favorites step with
# no evidence (`n`) and no centroid move. If the viewer then plays the title,
# that view is scored like any other.
ACCEPT_E = 0.05
LIST_ADD_E = 0.7
# "Mark as watched" on Title Detail: the viewer says they have seen it, which
# counts like a finished long-form view. It is a view, so watching and marking
# the same title combine into one contribution (the larger of the two).
MARKED_E = 0.8
FEEDBACK_E = {"like": 0.5, "more": 0.7, "dislike": -0.4}

# One title, one contribution: a title's view, feedback and My List add combine
# into one `e` (the strongest positive, +BOTH_BONUS when two or more agree,
# capped at 1; a dislike replaces them), and only the change since the title's
# last contribution is applied. A contribution older than CONTRIB_TTL_DAYS
# counts as a fresh one, so a rewatch months later refreshes taste.
BOTH_BONUS = 0.2
CONTRIB_TTL_DAYS = 30
CONTRIB_MAX = 300

# Taste centroids. Keep TASTE_* and taste_centroids in sync with
# backend/movie-ai-agent-v2/taste.py, infra/lambdas/household/household_core.py
# and insights/api/profile.py; infra/deployment/test_taste.py checks they agree.
# Each centroid keeps its evidence E (sum of the `e` merged into it); a new
# title moves it by max(CENTROID_RATE * e, e / (E + e)) -- an e-weighted running
# mean while young -- and its weight is E / (E + TASTE_EVIDENCE_K).
TASTE_JOIN_COSINE = 0.55
TASTE_MAX_CENTROIDS = 3
TASTE_EVIDENCE_K = 2.0
TASTE_TITLE_E = 0.8              # a completed title, for centroids built from watch history
CENTROID_RATE = 0.1


# --- engagement -------------------------------------------------------------

def card_completion(view):
    """Share of a short-form card watched: watched / min(length, CARD_MAX_S).
    A card played to its end counts as complete."""
    if view.get("reason") == "ended":
        return 1.0
    watched = view.get("watched_s") or 0
    duration = view.get("duration_s")
    if duration:
        return min(1.0, watched / min(float(duration), CARD_MAX_S))
    c = view.get("completion")
    return c if c is not None else min(1.0, watched / CARD_MAX_S)


def view_engagement(view):
    """Score `e` in [-1, 1] for one finished L1 view (a `recent` entry), or None
    when the view says nothing about taste."""
    if view.get("reason") == "marked":
        return MARKED_E                       # "Mark as watched": counts as a finished title
    watched = view.get("watched_s") or 0
    c = view.get("completion")
    if is_short_form(view.get("type")):
        if watched < CARD_MIN_S:
            return None                       # a swipe past is not a dislike
        c = card_completion(view)
        if c >= 0.9:
            return CARD_REPLAY_E if (view.get("plays") or 1) > 1 else CARD_FULL_E
        if c >= 0.5:
            return CARD_HALF_E
        dwell = round(CARD_DWELL_E * min(1.0, (watched - CARD_MIN_S) / (CARD_DWELL_FULL_S - CARD_MIN_S)), 3)
        return dwell or None
    if watched < MIN_SCORED_LONG_S or c is None:
        return None
    if c < 0.10:
        return -0.4
    if c < 0.50:
        return 0.1
    if c < 0.90:
        return 0.5
    return 0.8


def signals(before, after):
    """What a fold newly recorded in L1: [(kind, item_id, e, view_or_None)].

    `before` is the L1 state read before the fold (None, or another session's
    state, counts as empty). Views are matched on (item, ts), so a rewatch,
    which replaces the item's `recent` entry, is a new signal.
    """
    if not after:
        return []
    if not before or before.get("session_id") != after.get("session_id"):
        before = {}
    old_views = {(r["item"], r.get("ts")) for r in before.get("recent") or []}
    old_accepts = set(before.get("agent_accepts") or [])
    old_adds = set(before.get("list_adds") or [])
    out = []
    for r in reversed(after.get("recent") or []):          # oldest first
        if (r["item"], r.get("ts")) not in old_views:
            out.append(("view", r["item"], view_engagement(r), r))
    for item in reversed(after.get("agent_accepts") or []):
        if item not in old_accepts:
            out.append(("agent_accept", item, ACCEPT_E, None))
    for item in reversed(after.get("list_adds") or []):
        if item not in old_adds:
            out.append(("list_add", item, LIST_ADD_E, None))
    old_feedback = {(f["item"], f.get("value"), f.get("ts")) for f in before.get("feedback") or []}
    for f in reversed(after.get("feedback") or []):
        if (f["item"], f.get("value"), f.get("ts")) not in old_feedback and f.get("value") in FEEDBACK_E:
            out.append(("feedback", f["item"], FEEDBACK_E[f["value"]], f))
    # e is None for a view that is not scored (still a watch-state update).
    return out


# --- entities ---------------------------------------------------------------

def half_life_s(key):
    return HALF_LIFE_DAYS.get(key.split(":", 1)[0], DEFAULT_HALF_LIFE_DAYS) * 86400


def update_entities(entities, labels, e, ts, alpha=ALPHA, count=True):
    """s <- clamp(s * 2^(-dt/H) + alpha * e, -1, 1); n += 1 when `count`; last = ts.

    `entities` = AFFINITIES.entities (mutated, returned); `labels` = {key: label}
    for the title. A stored entry with no `last` (seeded) is not decayed.
    `count=False` is for a step that is not new evidence: a search-result open,
    or a later signal on a title that already counted.
    """
    for key, label in labels.items():
        cur = dict(entities.get(key) or {"s": 0.0, "n": 0, "label": label})
        s = float(cur.get("s") or 0.0)
        last = cur.get("last")
        if last is not None and ts > float(last):
            s *= math.pow(2.0, -(ts - float(last)) / half_life_s(key))
        cur["s"] = round(max(-1.0, min(1.0, s + alpha * e)), 4)
        cur["n"] = int(cur.get("n") or 0) + (1 if count else 0)
        cur["last"] = int(ts)
        cur.setdefault("label", label)
        entities[key] = cur
    return entities


# --- per-title contributions -------------------------------------------------------

_SLOTS = {"view": "v", "feedback": "f", "list_add": "l"}


def contribution_key(item, view):
    """What one contribution is keyed on: the item reacted to. A poster card is a
    stand-in clip of its title, so it is kept apart from viewing the title itself."""
    return ("card:" + item) if (view or {}).get("type") == "title_card" else item


def combine(parts):
    """One `e` for a title from its stored parts {v: view, f: feedback, l: list add}."""
    f = parts.get("f")
    if f is not None and f < 0:
        return f
    pos = [x for x in parts.values() if x is not None and x > 0]
    if not pos:
        return parts.get("v")
    return round(min(1.0, max(pos) + (BOTH_BONUS if len(pos) > 1 else 0.0)), 4)


def fold_contribution(contribs, key, kind, e, ts):
    """Fold one scored signal into AFFINITIES.taste_contrib[key] (mutated).

    Returns {"entities": de, "centroid": dc, "first": bool, "first_centroid": bool}:
    de = change in the title's combined e (favorites step), dc = the part of
    it the centroids have not yet seen (never negative: centroids only move
    toward liked titles). `first` means the title adds evidence (`n`) now.
    """
    cur = contribs.get(key)
    if cur and ts - float(cur.get("t") or 0) > CONTRIB_TTL_DAYS * 86400:
        cur = None
    first = cur is None
    cur = dict(cur or {"p": {}, "e": 0.0, "c": 0.0})
    parts = dict(cur.get("p") or {})
    slot = _SLOTS[kind]
    parts[slot] = e if slot != "v" or parts.get("v") is None else max(float(parts["v"]), e)
    combined = combine(parts)
    applied, seen = float(cur.get("e") or 0.0), float(cur.get("c") or 0.0)
    dc = max(0.0, combined - seen)
    contribs[key] = {"p": parts, "e": combined, "c": round(max(seen, combined), 4), "t": int(ts)}
    return {"entities": round(combined - applied, 4), "centroid": round(dc, 4),
            "first": first, "first_centroid": seen <= 0 and dc > 0}


def prune_contributions(contribs, now):
    """Drop expired contributions and keep the newest CONTRIB_MAX."""
    live = {k: c for k, c in contribs.items() if now - float(c.get("t") or 0) <= CONTRIB_TTL_DAYS * 86400}
    return dict(sorted(live.items(), key=lambda kv: -float(kv[1].get("t") or 0))[:CONTRIB_MAX])


# --- declared likes (onboarding) -------------------------------------------------

LOVED_S = 0.5      # a stated like: weaker than the seed's 0.7, so viewing can overrule it
LOVED_N = 3        # just reaches the agent's evidence threshold (n >= 3)


def seed_loved(entities, genres, ts):
    """Raise each loved genre to at least (LOVED_S, LOVED_N) and stamp `last`,
    so it fades like learned taste. Never lowers a stronger learned score.
    `genres` = [{id, label}]. Returns the keys it changed (entities mutated)."""
    changed = []
    for g in genres:
        key = "genre:" + "_".join(str(g["id"]).lower().split())
        cur = dict(entities.get(key) or {"s": 0.0, "n": 0, "label": g.get("label") or g["id"]})
        s, n = float(cur.get("s") or 0.0), int(cur.get("n") or 0)
        last = cur.get("last")
        if last is not None and ts > float(last):
            s *= math.pow(2.0, -(ts - float(last)) / half_life_s(key))
        if s >= LOVED_S and n >= LOVED_N:
            continue
        cur["s"] = round(max(s, LOVED_S), 4)
        cur["n"] = max(n, LOVED_N)
        cur["last"] = int(ts)
        cur.setdefault("label", g.get("label") or g["id"])
        entities[key] = cur
        changed.append(key)
    return changed


# --- time-of-day contexts --------------------------------------------------------

def update_context(contexts, view, labels, e, alpha=ALPHA):
    """Fold one scored view into AFFINITIES.contexts[view.bucket] (mutated, returned).

    Per bucket: minutes watched short- vs long-form, a view count, and entity
    scores with the same formula and half-lives as the overall taste. A view
    without a bucket (no UTC offset sent) changes nothing here.
    """
    bucket = (view or {}).get("bucket")
    if not bucket:
        return contexts
    ctx = dict(contexts.get(bucket) or {"short_min": 0.0, "long_min": 0.0, "views": 0, "entities": {}})
    key = "short_min" if is_short_form(view.get("type")) else "long_min"
    ctx[key] = round(float(ctx.get(key) or 0) + (view.get("watched_s") or 0) / 60.0, 2)
    ctx["views"] = int(ctx.get("views") or 0) + 1
    ctx["entities"] = update_entities(dict(ctx.get("entities") or {}), labels, e, view.get("ts") or 0, alpha)
    contexts[bucket] = ctx
    return contexts


# --- centroids --------------------------------------------------------------

def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


def _cos(a, b):
    return sum(x * y for x, y in zip(_unit(a), _unit(b)))


def _weight(evidence):
    return round(evidence / (evidence + TASTE_EVIDENCE_K), 4)


def _blend(c, u, step, e, new_title):
    c["vector"] = _unit([(1 - step) * a + step * b for a, b in zip(c["vector"], u)])
    c["evidence"] = c["evidence"] + e
    c["count"] += 1 if new_title else 0


def taste_centroids(vectors):
    """Group liked-title embeddings (oldest first) into up to 3 unit-length centroids,
    each title counted at TASTE_TITLE_E. Same algorithm as the household seed and the agent.
    Returns [{"vector", "weight", "count", "evidence"}], most evidence first."""
    groups = []  # {"vector", "count", "evidence"}
    for v in vectors:
        if not v:
            continue
        u, e = _unit(v), TASTE_TITLE_E
        best, best_sim = None, -2.0
        for g in groups:
            sim = sum(a * b for a, b in zip(u, g["vector"]))
            if sim > best_sim:
                best, best_sim = g, sim
        if best is not None and best_sim >= TASTE_JOIN_COSINE:
            _blend(best, u, max(CENTROID_RATE * e, e / (best["evidence"] + e)), e, True)
        elif len(groups) < TASTE_MAX_CENTROIDS:
            groups.append({"vector": u, "count": 1, "evidence": e})
        else:
            _blend(best, u, CENTROID_RATE / 2 * e, e, True)
    out = [{"vector": g["vector"], "weight": _weight(g["evidence"]), "count": g["count"],
            "evidence": round(g["evidence"], 4)} for g in groups]
    return sorted(out, key=lambda c: -c["evidence"])


def update_centroids(centroids, v, e, new_title=True):
    """Move taste toward a liked title (design §7.4 step 6; only for e > 0).

    `centroids` = [{"vector", "count", "evidence"}] (decoded). The nearest centroid
    within TASTE_JOIN_COSINE moves by max(CENTROID_RATE * e, e / (E + e)); a
    dissimilar title starts a new centroid while there is room, else nudges the
    nearest by CENTROID_RATE / 2 * e. `new_title=False` adds evidence for a title
    the centroids already counted (e.g. a like after watching) without raising
    `count`. Returns the new list, weight = E / (E + TASTE_EVIDENCE_K), most evidence first.
    """
    cents = [dict(c) for c in centroids]
    for c in cents:
        c.setdefault("evidence", c.get("count", 1) * TASTE_TITLE_E)
    if e > 0 and v:
        u = _unit(v)
        best = max(cents, key=lambda c: _cos(c["vector"], u), default=None)
        sim = _cos(best["vector"], u) if best else -2.0
        if best is not None and sim >= TASTE_JOIN_COSINE:
            _blend(best, u, max(CENTROID_RATE * e, e / (best["evidence"] + e)), e, new_title)
        elif len(cents) < TASTE_MAX_CENTROIDS:
            cents.append({"vector": u, "count": 1, "evidence": e})
        else:
            _blend(best, u, CENTROID_RATE / 2 * e, e, new_title)
    for c in cents:
        c["weight"] = _weight(c["evidence"])
    return sorted(cents, key=lambda c: -c["evidence"])


def encode_centroids(cents):
    """Decoded centroids -> AFFINITIES.centroids storage shape [{"v", "w", "n", "e"}]
    (float32 little-endian, base64), as the household seed writes it."""
    return [{"v": base64.b64encode(struct.pack(f"<{len(c['vector'])}f", *c["vector"])).decode("ascii"),
             "w": c["weight"], "n": c["count"],
             "e": round(c.get("evidence", c["count"] * TASTE_TITLE_E), 4)} for c in cents]


def decode_centroids(stored):
    """AFFINITIES.centroids -> [{"vector", "weight", "count", "evidence"}], skipping malformed
    entries. A centroid stored before evidence was kept counts TASTE_TITLE_E per title."""
    out = []
    for c in stored or []:
        try:
            raw = base64.b64decode(c["v"])
            n = int(c.get("n") or 1)
            out.append({"vector": list(struct.unpack(f"<{len(raw) // 4}f", raw)),
                        "weight": float(c.get("w") or 0), "count": n,
                        "evidence": float(c["e"]) if c.get("e") is not None else n * TASTE_TITLE_E})
        except Exception:  # noqa: BLE001
            continue
    return out
