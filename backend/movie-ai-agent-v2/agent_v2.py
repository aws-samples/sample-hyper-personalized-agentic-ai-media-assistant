"""v2 orchestrator: intent -> understand -> taste|fan-out|catch-all -> reduce.

A generator that yields status events between phases (same shape as v1) and a
final response event. AgentCore Memory provides multi-turn context (history +
already-recommended ids) and records each turn.

The understanding step (understand.py) is ONE cached LLM call that turns the
conversation into structured intent and reports which of the member's L3
preference rules the request explicitly overrides. Precedence, highest first:
limits > the current conversation (incl. overrides) > L3 preferences > L2 taste.
"""

from __future__ import annotations

import time

from config import SKIP_INTENT, MAX_HISTORY_TURNS
from llm import blank_usage, accumulate
from intent import classify_intent
from understand import understand, apply_profile
from answer import answer_question, shown_titles
from catchall import (build_filters, catch_all, catch_all_semantic, catch_all_short,
                      catch_all_taste, drop_excluded_genres)
from fanout import fan_out
from reduce import reduce, build_recommendations
from events_producer import emit_impressions
import session_behavior

TOTAL_STEPS = 4


def _status(message: str, phase: str = "", step: int = 0) -> dict:
    return {
        "type": "status",
        "message": message,
        "phase": phase,
        "step": step,
        "total_steps": TOTAL_STEPS,
        "timestamp": time.time(),
    }


def _history_and_recs(session) -> tuple[list[str], list[str]]:
    """Prior user messages + titles recommended in the last turn (for understand)."""
    if not session:
        return [], []
    turns = getattr(session, "_turns", None)
    if turns is None:
        turns = getattr(session, "turns", None) or []
    history = [t.get("query", "") for t in turns if t.get("query")]
    # Demo-tuned: only the most recent N turns feed the understanding step, so old/stale
    # constraints from far earlier in a long same-session demo don't linger.
    history = history[-MAX_HISTORY_TURNS:]
    prev_recs = [r.get("originalTitle", "") for r in session.get_last_recommendations()]
    prev_recs = [t for t in prev_recs if t]
    return history, prev_recs


def _combined_preferences(session) -> str:
    """Soft-guidance block = L2 taste from viewing (AFFINITIES) + learned LTM.

    Both are read from the memory object's caches (re-read about once a
    minute, never blocking here). The L2 block reflects what the member
    watched; the LTM adds what they said. Either may be empty.
    """
    if not session:
        return ""
    blocks = [session.get_user_profile_context(), session.get_long_term_context()]
    return "\n\n".join(b for b in blocks if b)


def _log_totals(totals: dict, elapsed: float, timings: dict | None = None) -> None:
    breakdown = ""
    if timings:
        breakdown = " | " + " ".join(f"{k}={v:.1f}s" for k, v in timings.items())
    print(
        f"[v2] done in {elapsed:.1f}s | in={totals['inputTokens']:,} "
        f"out={totals['outputTokens']:,} cache_read={totals['cacheReadInputTokens']:,} "
        f"cache_write={totals['cacheWriteInputTokens']:,}{breakdown}"
    )


def _years_label(d: dict) -> str:
    lo, hi = d.get("min_year"), d.get("max_year")
    if lo and hi:
        return f"the {lo}-{hi} date range"
    return f"titles from {lo} on" if lo else f"titles up to {hi}"


def _relax(d: dict, df: dict, knn_text: str, taste, watched: list[str],
           exclude_ids: list[str], hard_excl: list[str]):
    """Empty-result ladder: relax the request one step at a time and stop at the
    first step that leaves a usable title.

    Never relaxed: the kids ratings, limit genres, the request's own genre
    excludes and active genre rules (all in `hard_excl` / `df`), and watched titles.
    Returns (retrieval result, relaxed, exclude_ids for reduce, usage), where
    `relaxed` lists what the results no longer meet, as phrases for the reply.
    """
    usage = blank_usage()
    kids_f = build_filters({"kids": True}) if d.get("kids") else None

    def usable(p, excl):
        s = set(excl)
        return [t for t in p["candidates"] if t not in s]

    if d.get("category") == "micro-drama":
        # Stay short-form rather than leaking full-length titles.
        print("[v2] relax: micro-drama empty -> 'short' genre")
        p = catch_all_short(knn_text, kids=bool(d.get("kids")))
        return p, ["micro dramas"], exclude_ids, usage

    # (kind, request dict, movie_only, exclude ids, what this step gives up)
    steps = []
    cur = dict(df)
    if d["path"] == "catch_all" and not d.get("exact_title"):
        lost = ([f"the {cur['min_rating']}+ rating"] if cur.get("min_rating") else []) + \
               (["the award requirement"] if cur.get("awards") else [])
        if lost:
            cur = dict(cur, min_rating=None, awards=False)
            steps.append(("filtered", dict(cur), None, exclude_ids, lost))
        if cur.get("min_year") or cur.get("max_year"):
            lost = [_years_label(cur)]
            cur = dict(cur, min_year=None, max_year=None)
            steps.append(("filtered", dict(cur), None, exclude_ids, lost))
    people = d.get("person") or []
    people = [people] if isinstance(people, str) else [x for x in people if x]
    lost = ([" and ".join(people)] if people else []) + \
           ([f"the exact title '{d['exact_title']}'"] if d.get("exact_title") else [])
    movie_only = d.get("category") == "movie"
    steps.append(("semantic", None, movie_only, exclude_ids, lost))
    if movie_only:
        steps.append(("semantic", None, False, exclude_ids, ["the movies-only format"]))
    if set(exclude_ids) != set(watched):
        steps.append(("semantic", None, False, watched, ["titles already suggested"]))

    seed = d.get("exact_title") or d["search_text"] or knn_text
    relaxed: list[str] = []
    p = {"candidates": {}, "usage": blank_usage()}
    for i, (kind, req, mo, excl, lost) in enumerate(steps, 1):
        relaxed += lost
        try:
            if kind == "filtered":
                p = catch_all(req, knn_text, taste=taste, exclude_ids=watched)
            else:
                p = catch_all_semantic(seed, movie_only=mo, exclude_ids=excl, filters=kids_f)
                p["candidates"] = drop_excluded_genres(p["candidates"], hard_excl)
        except Exception as e:  # noqa: BLE001 -- a failed step just moves to the next
            print(f"[v2] relax step {i} failed: {e}")
            p = {"candidates": {}, "usage": blank_usage()}
        accumulate(usage, p.get("usage") or blank_usage())
        n = len(usable(p, excl))
        print(f"[v2] relax step {i}/{len(steps)} {kind} dropped={lost} -> {n} usable")
        if n:
            return p, relaxed, excl, usage
    return p, relaxed, exclude_ids, usage


def run_agent_stream(user_query: str, user_id: str = "demo-user", session=None):
    """Generator yielding status dicts, then a final response dict."""
    run_start = time.time()
    totals = blank_usage()
    timings: dict[str, float] = {}

    # --- Step 1: intent gate (chitchat answered directly) ---
    # Skipped by default: when invoked as a tool (e.g. by Nova Sonic) the caller
    # has already decided a recommendation is needed. Set SKIP_INTENT=false to
    # re-enable for callers that pass raw user text.
    yield _status("Analyzing your request...", phase="router", step=1)
    if not SKIP_INTENT:
        _t = time.time()
        intent_res = classify_intent(user_query, session)
        timings["intent"] = time.time() - _t
        accumulate(totals, intent_res["usage"])

        if not intent_res["is_recommendation"]:
            answer = intent_res["answer"] or ""
            if session:
                session.add_turn(user_query, [], response=answer)
            _log_totals(totals, time.time() - run_start, timings)
            yield {"type": "response", "response": answer,
                   "movie-recommendation": [], "tappable_options": []}
            return

    # --- conversation + member context (all cached per session) ---
    history, prev_recs = _history_and_recs(session)
    # Soft guidance for reduce = seeded profile text + AgentCore LTM. It never
    # reaches the understanding step, so taste can't rewrite what was asked.
    if session:
        session.ensure_ltm_refresher()
    # Member's L2 taste centroids + recently completed titles (cached per session).
    watched = session.get_watched_ids() if session else []
    # L1 behaviour facts from playback this session (stream processor): titles
    # played are excluded like completed ones, and the facts reach the rerank.
    behavior = getattr(session, "get_session_behavior", lambda: None)() if session else None
    played = session_behavior.viewed_ids(behavior)
    behavior_line = session_behavior.context_line(behavior)
    watched = list(dict.fromkeys(played + watched))
    taste = session.get_taste_vectors() if session else []
    prefs, limits = session.get_declared() if session else ([], {})
    print(f"[v2] taste: {len(taste)} centroid(s), watched-exclude: {len(watched)} "
          f"(played this session: {len(played)}), "
          f"rules: {len(prefs)}, limits: {sorted(k for k, v in limits.items() if v)}")

    # --- Step 2: understand (one cached LLM call) + route ---
    yield _status("Planning search strategy...", phase="planner", step=2)
    _t = time.time()
    d = understand(user_query, history, prev_recs, rules=[p["text"] for p in prefs])
    d = apply_profile(d, prefs, limits)
    timings["understand"] = time.time() - _t
    accumulate(totals, d["_usage"] or blank_usage())
    # Every hard genre exclusion: the request's own, active L3 genre rules, limits.
    hard_excl = list(dict.fromkeys(d["genres_exclude"] + d["rule_genres_exclude"]
                                   + d["limit_genres_exclude"]))
    df = dict(d, genres_exclude=hard_excl)
    # Empty-filter guard: a filtered route with nothing to filter on -> fan-out
    # (unless a limit forced the filtered path).
    if df["path"] == "catch_all" and not df.get("exact_title") and not build_filters(df):
        df["path"] = d["path"] = "fanout"
    query = d["llm_text"]
    knn_text = d["knn_text"] or d["search_text"] or user_query
    print(f"[v2] understand ok={d['_ok']} ({d['_s']}s): intent={d['intent']} path={d['path']} category={d['category']} "
          f"search={d['search_text']!r} avoid={d['genres_exclude'] + d['avoid_soft']} "
          f"overrides={d['overridden_rules']} active_rules={d['active_rules']} must_not={hard_excl}")
    yield _status("Search strategy ready", phase="planner_done", step=2)

    # --- Answer turn: a question about titles already shown -> reply, no new list ---
    if d["intent"] == "answer":
        yield _status("Looking that up...", phase="responder", step=4)
        _t = time.time()
        ans = answer_question(user_query, history, shown_titles(session))
        timings["answer"] = time.time() - _t
        accumulate(totals, ans["usage"] or blank_usage())
        print(f"[v2] answer about={ans['title_ids']} chars={len(ans['response'])}")
        if session:
            _t = time.time()
            session.add_turn(user_query, [], response=ans["response"])
            timings["memory"] = time.time() - _t
        _log_totals(totals, time.time() - run_start, timings)
        yield {"type": "response", "intent": "answer", "response": ans["response"],
               "movie-recommendation": [], "tappable_options": ans["tappable_options"],
               "about_title_ids": ans["title_ids"]}
        return

    # --- Step 3: retrieval ---
    yield _status("Searching for movies...", phase="executor", step=3)
    _t = time.time()
    try:
        if d["path"] == "taste":
            # Open request (open_ended): the member's taste is the query.
            excl = list(dict.fromkeys(watched + (session.get_excluded_ids() if session else [])))
            p = catch_all_taste(taste, exclude_ids=excl, movie_only=(d["category"] == "movie"),
                                filters=build_filters(df))
        elif d["path"] == "catch_all":
            p = catch_all(df, knn_text, taste=taste, exclude_ids=watched)
        else:
            p = fan_out(query, d["category"], kids=bool(d.get("kids")))
            p["candidates"] = drop_excluded_genres(p["candidates"], hard_excl)
    except Exception as e:
        # A malformed filter (e.g. a bad OpenSearch clause) must not sink the
        # request — treat it like an empty result so the semantic fallback runs.
        print(f"[v2] primary retrieval failed ({d['path']}): {e} -> semantic fallback")
        p = {"candidates": {}, "usage": blank_usage()}
    accumulate(totals, p.get("usage", blank_usage()))
    # Titles reduce will drop: everything shown this session, plus watched
    # titles -- except when the member asked for a title by name, which they
    # get even if it was already shown or watched.
    exclude_ids = []
    if not d.get("exact_title"):
        session_ids = session.get_excluded_ids() if session else []
        exclude_ids = list(dict.fromkeys(list(session_ids) + watched))
    usable = [t for t in p["candidates"] if t not in set(exclude_ids)]
    # Robustness: if retrieval left nothing to recommend (empty result, over-
    # constrained filters, a throttled fan-out, or every candidate already
    # watched/recommended), relax the request one step at a time until a step
    # returns something -- and tell the viewer the original found nothing.
    relaxed = None
    if not usable:
        p, relaxed, exclude_ids, usage = _relax(d, df, knn_text, taste, watched, exclude_ids, hard_excl)
        accumulate(totals, usage)
    timings["retrieval"] = time.time() - _t
    yield _status(f"Found {len(p['candidates'])} movies", phase="executor_done", step=3)

    # --- Step 4: reduce / rerank ---
    yield _status("Picking the best matches for you...", phase="responder", step=4)
    # Re-read: the LTM background refresher may have populated the cache during
    # the pipeline, so reduce gets preferences even if understand didn't.
    _t = time.time()
    # A request is "genre-authoritative" when it's essentially just genre(s)
    # (+ maybe a format like micro-drama) with no person, exact title, concrete
    # keywords, or leftover mood phrase — i.e. the genre is explicit, not
    # inferred. Only then does reduce hard-restrict to on-genre titles; mixed /
    # vibe / "like X" requests keep genre soft (on-genre floated to the top).
    genre_authoritative = (
        bool(d.get("genres_include"))
        and not d.get("person")
        and not d.get("exact_title")
        and not d.get("keywords")
        and not (d.get("mood") or "").strip()
    )
    red = reduce(query, p["candidates"], exclude_ids=exclude_ids,
                 preferences="\n\n".join(b for b in [_combined_preferences(session), behavior_line] if b),
                 request_genres=d.get("genres_include"),
                 genre_authoritative=genre_authoritative,
                 relaxed=relaxed)
    timings["reduce"] = time.time() - _t
    accumulate(totals, red["usage"])

    _t = time.time()
    recommendations, content_urls = build_recommendations(red["title_ids"])
    timings["rehydrate"] = time.time() - _t

    if session:
        _t = time.time()
        # Store the viewer's own words: the understanding step re-reads them as history.
        session.add_turn(user_query, recommendations, response=red["response"])
        timings["memory"] = time.time() - _t
        emit_impressions(getattr(session, "actor_id", ""), getattr(session, "session_id", ""),
                         len(getattr(session, "_turns", None) or []),
                         [r.get("title_id") for r in recommendations if r.get("title_id")])

    _log_totals(totals, time.time() - run_start, timings)

    event = {
        "type": "response",
        "intent": "recommend",
        "response": red["response"],
        "movie-recommendation": recommendations,
        "tappable_options": red["tappable_options"],
    }
    if content_urls:
        event["content_urls"] = content_urls
    yield event


def run_agent(user_query: str, user_id: str = "demo-user", session=None) -> dict:
    """Non-streaming convenience wrapper: returns only the final response dict."""
    result = None
    for event in run_agent_stream(user_query, user_id=user_id, session=session):
        if event.get("type") == "response":
            result = event
    return result or {"type": "response", "response": "",
                      "movie-recommendation": [], "tappable_options": []}
