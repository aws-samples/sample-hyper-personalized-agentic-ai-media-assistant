"""Catch-all retrieval path: OpenSearch hard filters + hybrid KNN.

Used for filter-bearing queries (actor, era, rating, kids, awards, exact
title, genre-exclude). Hard filters narrow the candidate set; the soft
signal text drives KNN ranking within it. Mirrors build_filters() + catch_all()
from the validated pipeline.
"""

from __future__ import annotations

import time

from config import CATCHALL_SIZE, SURPRISE_POOL
from router import KIDS_MPAA_RATINGS, TV_TYPES
from retrieval import knn_search
from catalog import candidate_line


def build_filters(d: dict) -> list[dict]:
    """Translate the decomposition's HARD filters into OpenSearch clauses.

    genres_include and keywords are intentionally excluded here — they are soft
    signals folded into the KNN query text, not zero-match hard clauses.
    """
    f: list[dict] = []
    exclude: list[dict] = []

    persons = d.get("person")
    if persons:
        # `person` may be one name (str) or several (list) — the router returns a
        # list when a query names multiple people ("films by X, Y, or Z"). Build
        # an OR of match_phrase clauses per name across cast + crew. A raw list
        # would otherwise be injected into match_phrase and 400 OpenSearch.
        if isinstance(persons, str):
            persons = [persons]
        should = []
        for name in persons:
            if not name:
                continue
            should.append({"match_phrase": {"principalCastMembers": name}})
            should.append({"match_phrase": {"principalCrewMembers": name}})
        if should:
            f.append({"bool": {"should": should, "minimum_should_match": 1}})

    for g in d.get("genres_exclude") or []:
        exclude.append({"match": {"genres": g}})

    yr: dict = {}
    if d.get("min_year"):
        yr["gte"] = int(d["min_year"])
    if d.get("max_year"):
        yr["lte"] = int(d["max_year"])
    if yr:
        f.append({"range": {"premiere_year": yr}})

    if d.get("min_rating"):
        f.append({"range": {"imdb_rating": {"gte": float(d["min_rating"])}}})

    if d.get("kids"):
        f.append({"terms": {"mpaa_ratings": list(KIDS_MPAA_RATINGS)}})

    if d.get("awards"):
        f.append({"exists": {"field": "awards"}})

    if d.get("category") == "micro-drama":
        f.append({"term": {"title_type": "micro-drama"}})
    elif d.get("category") == "tv":
        f.append({"terms": {"title_type": TV_TYPES}})

    if exclude:
        f.append({"bool": {"must_not": exclude}})

    return f


def taste_applies(d: dict) -> bool:
    """Taste boost only for open requests: never when the request names a
    person, a title or a year range, where it could pull results off-topic."""
    return not (d.get("person") or d.get("exact_title") or d.get("min_year") or d.get("max_year"))


def _candidates(hits: list[dict]) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for h in hits:
        tid = h.get("title_id")
        if tid:
            candidates[tid] = candidate_line(
                tid, h.get("originalTitle", tid),
                str(h.get("premiere_date", ""))[:4], h.get("imdb_rating", ""),
                h.get("genres"), h.get("plot"),
            )
    return candidates


def catch_all(d: dict, qtext: str, taste=None, exclude_ids=None) -> dict:
    """Run the filter+KNN path. Returns {"candidates": {tid: line}, "path_s": s}.

    `taste` (member centroids) boosts open requests (see taste_applies);
    `exclude_ids` (watched titles) are filtered out, except on an exact-title
    request, where the member asked for that title by name.
    """
    t0 = time.perf_counter()
    if d.get("exact_title"):
        hits = knn_search(
            query_text=d["exact_title"],
            filters=[{"match_phrase": {"title_text": d["exact_title"]}}],
            size=CATCHALL_SIZE, movie_only=False,
        )
    else:
        boost = taste if taste and taste_applies(d) else None
        if boost:
            print(f"[v2] taste boost: {len(boost)} centroid(s)")
        hits = knn_search(
            query_text=qtext or "popular acclaimed title",
            filters=build_filters(d),
            size=CATCHALL_SIZE, movie_only=(d.get("category") == "movie"),
            taste=boost, exclude_ids=exclude_ids,
        )
    elapsed = time.perf_counter() - t0
    # Retrieval is pure OpenSearch here (no LLM tokens); blank usage keeps the
    # orchestrator's accumulation uniform across paths.
    from llm import blank_usage
    return {"candidates": _candidates(hits), "path_s": round(elapsed, 3), "usage": blank_usage()}


def catch_all_taste(taste, exclude_ids=None, movie_only: bool = True, filters=None) -> dict:
    """Open-request retrieval (path "taste"): near the member's taste centroids,
    or the highest-rated titles for a member with no taste yet. `filters` carries
    hard exclusions (the request's, active L3 genre rules, limits). No LLM call."""
    from llm import blank_usage
    from retrieval import taste_search, top_rated
    t0 = time.perf_counter()
    if taste:
        print(f"[v2] taste retrieval: {len(taste)} centroid(s)")
        hits = taste_search(taste, SURPRISE_POOL, movie_only=movie_only, exclude_ids=exclude_ids,
                            filters=filters)
    else:
        print("[v2] taste retrieval: no centroids yet -> top rated")
        hits = top_rated(SURPRISE_POOL, movie_only=movie_only, exclude_ids=exclude_ids, filters=filters)
    return {"candidates": _candidates(hits), "path_s": round(time.perf_counter() - t0, 3),
            "usage": blank_usage()}


def drop_excluded_genres(candidates: dict[str, str], genres: list[str]) -> dict[str, str]:
    """Fan-out has no query-side filter: drop candidates carrying an excluded genre
    (exact catalog genre tokens from the candidate line)."""
    if not genres:
        return candidates
    bad = set(genres)
    out = {}
    for tid, line in candidates.items():
        m = line.split("genres:", 1)
        gs = {g.strip().lower() for g in m[1].split("|", 1)[0].split(",")} if len(m) == 2 else set()
        if not gs & bad:
            out[tid] = line
    return out


def catch_all_short(qtext: str, kids: bool = False) -> dict:
    """Micro-drama fallback: hard-filter genre 'short' when no micro-drama
    title_type matches. Keeps results short-form instead of leaking full-length
    titles via the generic (filter-free) semantic fallback. kids keeps the
    kids ratings.
    """
    from llm import blank_usage
    t0 = time.perf_counter()
    hits = knn_search(query_text=qtext or "short drama",
                      filters=[{"match": {"genres": "short"}}] + (build_filters({"kids": True}) if kids else []),
                      size=CATCHALL_SIZE, movie_only=False)
    elapsed = time.perf_counter() - t0
    candidates: dict[str, str] = {}
    for h in hits:
        tid = h.get("title_id")
        if tid:
            candidates[tid] = candidate_line(
                tid, h.get("originalTitle", tid),
                str(h.get("premiere_date", ""))[:4], h.get("imdb_rating", ""),
                h.get("genres"), h.get("plot"),
            )
    return {"candidates": candidates, "path_s": round(elapsed, 3), "usage": blank_usage()}


def catch_all_semantic(qtext: str, movie_only: bool = True, exclude_ids=None, filters=None) -> dict:
    """Semantic KNN fallback without the request's filters.

    Used when the primary retrieval leaves nothing to recommend -- an empty
    result, over-constrained filters, a throttled fan-out, or every candidate
    already watched / recommended (`exclude_ids` keeps those out here too).
    Guarantees the request still returns semantically similar titles.
    `filters` carries only the guardrails that must survive (the kids ratings).
    """
    from llm import blank_usage
    t0 = time.perf_counter()
    hits = knn_search(query_text=qtext or "popular acclaimed title",
                      filters=filters or None, size=CATCHALL_SIZE, movie_only=movie_only,
                      exclude_ids=exclude_ids)
    elapsed = time.perf_counter() - t0
    candidates: dict[str, str] = {}
    for h in hits:
        tid = h.get("title_id")
        if tid:
            candidates[tid] = candidate_line(
                tid, h.get("originalTitle", tid),
                str(h.get("premiere_date", ""))[:4], h.get("imdb_rating", ""),
                h.get("genres"), h.get("plot"),
            )
    return {"candidates": candidates, "path_s": round(elapsed, 3), "usage": blank_usage()}
