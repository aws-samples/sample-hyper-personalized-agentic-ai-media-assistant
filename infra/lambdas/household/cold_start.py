"""Cold-start Home rows for a member with nothing in the recommendation cache.

Explained with diagrams in docs/how-personalization-works.md §5.2.

A member added through "Add profile" has no seeded rows, and nothing else
builds rows until the offline Personalize job runs. GET /home then builds
them here, from what the member told us:

  * one rail per liked genre (L2 `genre:*` entities, strongest first, up to
    MAX_GENRE_RAILS), so the genres loved in onboarding shape Home at once;
  * "Top Rated Movies" and "Top Rated Series", for everyone.

Every query applies the member's L3 limits: never-show genres and the kids
limit. The catalog records no popularity, so award-listed
titles ranked by rating stand in for it (the same quality floor as the clips
feed). A title appears on one rail only.

Rows are written in the shape the Personalize job writes, tagged
`source=cold_start`, with a `basis` fingerprint of the inputs on the hero
row and an `expires_at`. GET /home rebuilds them when the basis changes
(new rules, a different top genre) or they expire. Rows from any other
source are never touched, and once they exist the cold-start rows are
ignored.

Pure logic: the catalog search is passed in, so this is testable offline.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

SOURCE = "cold_start"
HERO_SK = "HERO#home"
ROWS_PREFIX = "ROWS#home#"
TTL = timedelta(days=7)

MAX_GENRE_RAILS = 3
GENRE_RAIL_SIZE = 12
TOP_RAIL_SIZE = 20
FETCH_EXTRA = 20             # over-fetch so de-duplication across rails still fills each rail
MIN_EVIDENCE = 3             # same floor the agent uses before it trusts an entity
GENRE_HALF_LIFE_DAYS = 30    # keep in sync with profile_events/l2.py HALF_LIFE_DAYS["genre"]

MOVIE_TYPES = ["movie", "tvMovie"]
SERIES_TYPES = ["tvSeries", "tvMiniSeries"]
# Kids limit: same ratings as the clips feed.
KIDS_RATINGS = ["g", "tv-y", "tv-y7", "tv-g"]  # TV-PG is parental guidance, not kids


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def liked_genres(affinities: dict, excluded, now: datetime) -> list:
    """[(genre_id, label)] the member likes, strongest first, faded by age like L2."""
    excluded = {str(g).lower() for g in excluded or []}
    scored = []
    for key, e in ((affinities or {}).get("entities") or {}).items():
        if not key.startswith("genre:") or not isinstance(e, dict):
            continue
        gid = key[len("genre:"):]
        s, n = float(e.get("s") or 0), float(e.get("n") or 0)
        if gid in excluded or s <= 0 or n < MIN_EVIDENCE:
            continue
        last = _parse_ts(e.get("last"))
        age_days = max(0.0, (now - last).total_seconds() / 86400) if last else 0.0
        scored.append((s * 0.5 ** (age_days / GENRE_HALF_LIFE_DAYS), gid, e.get("label") or gid.title()))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [(gid, label) for _, gid, label in scored[:MAX_GENRE_RAILS]]


def limits(declared: dict) -> list:
    """OpenSearch filters every cold-start query shares: quality floor + L3 limits."""
    declared = declared or {}
    fc = [{"exists": {"field": "image"}}, {"exists": {"field": "awards"}}, {"exists": {"field": "imdb_rating"}}]
    banned = [{"match_phrase": {"genres": g}} for g in declared.get("excluded_genres") or [] if g]
    if banned:
        fc.append({"bool": {"must_not": banned}})
    if declared.get("kids_profile"):
        fc.append({"terms": {"mpaa_ratings": KIDS_RATINGS}})
    return fc


def plan(declared: dict, affinities: dict, now: datetime):
    """(basis, rail specs). The basis changes exactly when the rails would."""
    declared = declared or {}
    fc = limits(declared)
    specs = [{"key": f"cs_genre_{gid}", "title": f"Because You Like {label}", "size": GENRE_RAIL_SIZE,
              "filter": fc + [{"terms": {"title_type": MOVIE_TYPES + SERIES_TYPES}},
                              {"match_phrase": {"genres": gid}}]}
             for gid, label in liked_genres(affinities, declared.get("excluded_genres"), now)]
    specs.append({"key": "cs_top_movies", "title": "Top Rated Movies", "size": TOP_RAIL_SIZE,
                  "filter": fc + [{"terms": {"title_type": MOVIE_TYPES}}]})
    specs.append({"key": "cs_top_series", "title": "Top Rated Series", "size": TOP_RAIL_SIZE,
                  "filter": fc + [{"terms": {"title_type": SERIES_TYPES}}]})
    basis = hashlib.sha256(json.dumps([[s["key"], s["filter"]] for s in specs], sort_keys=True).encode()).hexdigest()[:16]
    return basis, specs


def query(spec: dict) -> dict:
    """The catalog query for one rail: top rated within its filters."""
    return {"_source": ["title_id"], "size": spec["size"] + FETCH_EXTRA,
            "query": {"bool": {"filter": spec["filter"]}},
            "sort": [{"imdb_rating": {"order": "desc"}}, {"title_id": {"order": "asc"}}]}


def build_rows(profile_id: str, basis: str, specs: list, search, now: datetime) -> list:
    """recommendation-cache items for one member. `search(body)` returns OpenSearch hits."""
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = int((now + TTL).timestamp())
    used, rails = set(), []
    for spec in specs:
        hits = search(query(spec)).get("hits", {}).get("hits", [])
        picks = []
        for h in hits:
            tid = (h.get("_source") or {}).get("title_id")
            if tid and tid not in used:
                picks.append(tid)
                used.add(tid)
            if len(picks) == spec["size"]:
                break
        if picks:
            rails.append((spec, picks))
    if not rails:
        return []
    common = {"profile_id": profile_id, "source": SOURCE, "generated_at": stamp, "expires_at": expires}
    rows = [{**common, "sk": HERO_SK, "title_id": rails[0][1][0], "basis": basis}]
    for rank, (spec, picks) in enumerate(rails, 1):
        rows.append({**common, "sk": ROWS_PREFIX + spec["key"], "rank": rank, "display_title": spec["title"],
                     "items": [{"title_id": t} for t in picks]})
    return rows


def is_fresh(rows: list, basis: str, now: datetime) -> bool:
    """True when stored cold-start rows were built from these inputs and have not expired."""
    hero = next((r for r in rows if r.get("sk") == HERO_SK), None)
    return bool(hero) and hero.get("basis") == basis and int(hero.get("expires_at") or 0) > now.timestamp()
