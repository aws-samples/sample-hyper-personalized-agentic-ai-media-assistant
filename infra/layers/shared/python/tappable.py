"""Idle-search starter options ("tappable options"), chosen from a pre-built pool.

Explained with diagrams in docs/how-personalization-works.md §5.3.

The pool (tappable_pool.json, built offline by scripts/build_tappable_pool.py)
holds short request texts, each tagged with catalog genre ids and embedded in
the catalog's vector space. Nothing here calls a model, so the cost per viewer
is a few hundred dot products.

  * Cold start (no taste yet): `starter()` draws COUNT generic options at random.
  * Taste profile: `from_taste()` picks the options closest to the viewer's
    taste centroids (one per centroid first, heaviest first), or, before any
    centroid exists, options tagged with the genres loved in onboarding. The
    events processor stores the result on AFFINITIES as `tappable_options`,
    so it changes only when the taste profile does.
  * Read time: `visible()` drops stored options that a later L3 change rules
    out (a never-show genre, a kids profile) and tops up from `starter()`.
"""

import base64
import json
import math
import random
import struct
from pathlib import Path

COUNT = 3
STORED = 6            # stored per profile, so read-time filtering can still show COUNT
POOL_PATH = Path(__file__).with_name("tappable_pool.json")

_pool = None


def _decode(v):
    raw = base64.b64decode(v)
    vec = struct.unpack(f"<{len(raw) // 4}f", raw)
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def pool(path=None):
    """[{id, text, genres, kids, generic, vector}] -- loaded once per container."""
    global _pool
    if _pool is None or path:
        data = json.loads(Path(path or POOL_PATH).read_text())
        loaded = [{**{k: o[k] for k in ("id", "text", "genres", "kids", "generic")}, "vector": _decode(o["v"])}
                  for o in data["options"]]
        if path:
            return loaded
        _pool = loaded
    return _pool


def _allowed(option, declared):
    declared = declared or {}
    banned = {str(g).lower() for g in declared.get("excluded_genres") or []}
    if banned & set(option.get("genres") or []):
        return False
    return bool(option.get("kids")) or not declared.get("kids_profile")


def _out(option):
    return {"id": option["id"], "text": option["text"], "genres": list(option.get("genres") or []),
            "kids": bool(option.get("kids"))}


def starter(declared=None, count=COUNT, rng=random, options=None):
    """`count` generic options at random, within the profile's limits."""
    candidates = [o for o in (options or pool()) if o.get("generic") and _allowed(o, declared)]
    return [_out(o) for o in rng.sample(candidates, min(count, len(candidates)))]


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def from_taste(centroids, loved_genres=(), declared=None, count=STORED, options=None):
    """Options for a profile with taste, best first; [] when there is no taste yet.

    `centroids` = decoded AFFINITIES centroids [{"vector", "weight"}];
    `loved_genres` = genre ids, used only while there are no centroids.
    """
    candidates = [o for o in (options or pool()) if _allowed(o, declared)]
    picked = []
    cents = [c for c in centroids or [] if c.get("vector")]
    if cents:
        cents = sorted(cents, key=lambda c: -float(c.get("weight") or 0))
        units = [(_unit(c["vector"]), float(c.get("weight") or 0)) for c in cents]
        sims = {o["id"]: [sum(a * b for a, b in zip(u, o["vector"])) for u, _w in units] for o in candidates}
        # One option per centroid first, so each side of the viewer's taste shows up.
        for i in range(len(units)):
            best = max((o for o in candidates if o not in picked), key=lambda o: sims[o["id"]][i], default=None)
            if best is not None:
                picked.append(best)
        # Then the rest by similarity to any centroid, nudged toward heavier ones.
        rest = sorted((o for o in candidates if o not in picked),
                      key=lambda o: -max(s * (0.85 + 0.15 * w) for s, (_u, w) in zip(sims[o["id"]], units)))
        picked += rest[:max(0, count - len(picked))]
    else:
        loved = [str(g).lower() for g in loved_genres or []]
        if not loved:
            return []
        # Round-robin over loved genres; specific (non-generic) options first.
        by_genre = {g: sorted((o for o in candidates if g in o.get("genres", [])),
                              key=lambda o: (o.get("generic", False), len(o.get("genres", [])))) for g in loved}
        while len(picked) < count and any(by_genre.values()):
            for g in loved:
                while by_genre[g] and by_genre[g][0] in picked:
                    by_genre[g].pop(0)
                if by_genre[g] and len(picked) < count:
                    picked.append(by_genre[g].pop(0))
    return [_out(o) for o in picked[:count]]


def visible(stored, declared=None, count=COUNT, rng=random, options=None):
    """Option texts to show now: stored ones still within the limits, topped up at random."""
    keep = [o for o in stored or [] if isinstance(o, dict) and o.get("text") and _allowed(o, declared)][:count]
    if len(keep) < count:
        texts = {o["text"] for o in keep}
        extra = [o for o in starter(declared, count + len(keep), rng, options) if o["text"] not in texts]
        keep += extra[:count - len(keep)]
    return [o["text"] for o in keep]
