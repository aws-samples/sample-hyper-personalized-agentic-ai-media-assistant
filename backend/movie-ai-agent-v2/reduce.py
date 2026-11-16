"""Reduce / rerank step + final payload assembly.

Takes the merged candidates from either retrieval path and produces the v1
response contract: a warm 1-2 sentence message, an ordered list of title_ids,
and 1-3 tappable options. Ranking is relevance-first, then popularity-aware
(awards count + rating + the model's own sense of how iconic a title is), since
the index has no vote-count field. Selected ids are validated against the
candidate set (the model occasionally mistypes an id) and then rehydrated into
full display records for the UI.
"""

from __future__ import annotations

import json
import re

from config import REDUCE_MODEL_ID, FINAL_TOP_N
from llm import call, blank_usage
from retrieval import fetch_records_by_ids
from content_url import get_content_url, generate_presigned_url

REDUCE_SYSTEM = (
    "You are a movie recommendation reranker. You are given the user's request and "
    "candidate titles, each formatted 'Title [id] | year | rating | awards <count> "
    "| genres: <list> | <short plot>' (genres and/or plot may be missing on some). "
    f"Select and rank the best {FINAL_TOP_N} (or fewer if fewer are provided).\n\n"
    "Ranking rules:\n"
    "- Rank by RELEVANCE to the request FIRST, judged primarily from the genres and plot: "
    "a title whose genres/plot match the requested genre, theme, or mood outranks one that "
    "does not, even if the non-matching title has a higher rating.\n"
    "- Among titles of COMPARABLE relevance, prefer more popular/iconic ones — use the awards "
    "count and rating as acclaim signals AND your own knowledge of how well-known each "
    "title is — then break ties toward more recent releases. Do this UNLESS the user asked otherwise.\n"
    "- If a 'Viewer preferences' section is provided, treat it as SOFT guidance that affects ORDER "
    "ONLY — never which titles you return or how many. Among titles of comparable relevance, gently "
    "order the viewer's liked genres/themes/sports/teams/players higher. NEVER drop, exclude, or "
    "withhold a title that matches the user's explicitly requested genre/theme just because it "
    "conflicts with a preference (e.g. if the user asked for sci-fi, include every sci-fi candidate "
    "up to the limit even if some are action-heavy or violent and the viewer usually avoids that). "
    "NEVER surface an off-request title just because of preferences.\n"
    "- The request may end with 'Avoid: ...' (what the viewer said they don't want) and "
    "'Viewer's standing avoids: ...' (their settings). Judge each candidate from its genres, "
    "plot and your own knowledge of the title: leave out or rank last a title that clearly "
    "has an avoided quality, and prefer ones that don't. What the request explicitly asks for "
    "always wins over a standing avoid.\n"
    "- A title the request uses only as a REFERENCE for what the viewer wants ('like X', 'in "
    "the vein of X', 'more like X', 'I loved X, something similar') must never be one of your "
    "picks -- they want titles similar to it, not it. Only return that title when the viewer "
    "asks for it itself ('play X').\n"
    "- Return as MANY on-request matches as the candidates provide, up to the limit above: include "
    "ALL candidates that match the request's genre/theme, and only return fewer when there genuinely "
    "are fewer matching candidates. Preferences and taste only REORDER candidates; they must NEVER "
    "reduce the count, return an empty list, apologize, or refuse.\n"
    "- Only use id values that appear in the candidates. NEVER invent or guess an id.\n\n"
    "Output ONLY a JSON object (no markdown fences, no extra text) with exactly these keys:\n"
    '{\n'
    '  "response": "A short 1-2 sentence, warm explanation of why these picks fit.",\n'
    '  "title_ids": ["tt...", "tt..."],\n'
    '  "tappable_options": ["Two three words", "Another theme"]\n'
    '}\n\n'
    "Response text rules:\n"
    "- NEVER mention any actor, director, person, or movie title in the response text — the UI "
    "renders those separately. Write generically (e.g. 'Here are some tense, gripping picks').\n"
    "- Keep it warm and conversational, 1-2 sentences max. Be subtle — do not call out the user's "
    "preferences or watch history.\n"
    "- NEVER apologize, hedge, or say the picks don't match, aren't quite right, or that options are "
    "limited/few. Always present the picks positively and confidently, even if there's only one.\n"
    "- tappable_options: 1-3 items, each exactly 2-3 words. Always include them."
)


# Apology / hedge / redirect markers the small reranker sometimes emits despite
# the prompt. If any appears, we replace the copy with a neutral positive line.
_HEDGE_MARKERS = (
    "sorry", "apolog", "unfortunately", "doesn't match", "does not match",
    "doesn't quite", "isn't quite", "only candidate", "only one candidate",
    "only have one", "only one available", "might want to explore",
    "you might want to", "other platforms", "dedicated short", "elsewhere",
    "limited options", "not a perfect", "not quite what",
)


def _looks_hedgy(text: str) -> bool:
    """True if the response text apologizes, hedges, or redirects the user away."""
    t = text.lower()
    return any(m in t for m in _HEDGE_MARKERS)


def _parse_json(text: str) -> dict:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        s = s.rsplit("```", 1)[0]
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        # Fallback: recover ids + tappable options from loose text.
        ids = re.findall(r"\[(tt[0-9a-zA-Z]+)\]", text)
        if not ids:
            ids = re.findall(r"\btt\d+\b", text)
        resp = ""
        m = re.search(r'"response"\s*:\s*"([^"]+)"', text)
        if m:
            resp = m.group(1)
        return {"response": resp, "title_ids": ids, "tappable_options": []}


def _line_genres(line: str) -> set[str]:
    """Extract the genre tokens from a candidate line ('... | genres: a, b | ...')."""
    m = re.search(r"genres:\s*([^|]+)", line)
    if not m:
        return set()
    return {g.strip().lower() for g in m.group(1).split(",") if g.strip()}


def relaxed_lead(relaxed: list[str] | None) -> str:
    """Opening sentence when the full request found nothing and the search was
    relaxed, so the viewer is not surprised by picks that miss part of it.
    Deliberately high level: what was relaxed goes only to the reranker."""
    if relaxed is None:
        return ""
    return "I couldn't find an exact match, so here are the closest picks."


def reduce(query: str, candidates: dict[str, str], exclude_ids: list[str] | None = None,
           preferences: str = "", request_genres: list[str] | None = None,
           genre_authoritative: bool = False, relaxed: list[str] | None = None) -> dict:
    """Rerank candidates and return
    {response, title_ids, tappable_options, usage, latency_s}.

    `relaxed` (set only when the full request found nothing) lists what the
    candidates no longer meet; the reply then opens with `relaxed_lead`.

    `candidates` maps title_id -> candidate line. `exclude_ids` (already
    recommended this session) are dropped before reranking. `preferences` is
    the viewer's long-term taste block (soft guidance), if any.

    `request_genres` are the genres the router parsed from the request; they are
    used so soft preferences/persona can't bury or drop on-genre matches:
      - `genre_authoritative=True` (a pure genre/format request, no person /
        title / keywords / mood phrase): keep ONLY on-genre titles.
      - otherwise (mixed / vibe / "like X" request): keep everything but float
        on-genre titles to the top. This preserves the deliberately-soft genre
        behavior for mood/similar-to queries where genre was only inferred.
    """
    if exclude_ids:
        excl = set(exclude_ids)
        candidates = {k: v for k, v in candidates.items() if k not in excl}

    if not candidates:
        return {
            "response": "I couldn't find anything new in the catalog for that. "
                        "Want to try a different vibe?",
            "title_ids": [], "tappable_options": [],
            "usage": blank_usage(), "latency_s": 0.0,
        }

    user_input = f"User request: {query}\n\n"
    if relaxed is not None:
        user_input += ("Search note: nothing matched the full request, so these candidates are the "
                       "closest matches" + (f" and may not meet: {', '.join(relaxed)}" if relaxed else "")
                       + ". The viewer is told this separately: do not repeat it, and do not describe "
                       "the picks as meeting what they may not meet. Your response follows a sentence "
                       "that already introduces the picks, so do NOT open with 'Here are', 'Here's' or "
                       "any other introduction; describe what the picks share instead (e.g. 'They're "
                       "tense and gripping, with plenty of twists.').\n\n")
    if preferences:
        user_input += f"{preferences}\n\n"
    user_input += "Candidates:\n" + "\n".join(candidates.values())
    text, usage, elapsed = call(REDUCE_SYSTEM, user_input, model_id=REDUCE_MODEL_ID)
    parsed = _parse_json(text)

    # Validate ids against the candidate set, DEDUPE (the model occasionally
    # repeats a title), and preserve the model's ordering.
    valid: list[str] = []
    seen: set[str] = set()
    for tid in parsed.get("title_ids", []):
        if tid in candidates and tid not in seen:
            seen.add(tid)
            valid.append(tid)
            if len(valid) >= FINAL_TOP_N:
                break

    # Genre-faithful selection: when the user explicitly named genre(s), the
    # result must contain ONLY titles matching them. Soft preferences/persona may
    # reorder WITHIN that set but must never rank or inject an off-genre title
    # (e.g. an action-fan persona pulling sci-fi shorts above a 'comedy' request,
    # or pruning an on-genre pick). This is deterministic — independent of whether
    # the small reranker obeyed the prompt. Falls back to the reranker's list only
    # if the catalog genuinely has no on-genre candidate (never return empty).
    dropped_offgenre = False
    if request_genres:
        want = {g.lower() for g in request_genres if g}
        on_genre = [tid for tid in candidates if want & _line_genres(candidates[tid])] if want else []
        if on_genre:
            on_set = set(on_genre)
            if genre_authoritative:
                # Pure genre/format request -> keep ONLY on-genre titles; persona
                # may order within them but can't inject or rank off-genre above.
                dropped_offgenre = any(tid not in on_set for tid in valid)
                ordered = [tid for tid in valid if tid in on_set]
                for tid in on_genre:  # cover any on-genre pick the reranker omitted
                    if len(ordered) >= FINAL_TOP_N:
                        break
                    if tid not in ordered:
                        ordered.append(tid)
                valid = ordered[:FINAL_TOP_N]
            else:
                # Mixed / vibe / "like X" request -> float on-genre to the top but
                # KEEP off-genre titles as fillers (genre was likely inferred, so
                # don't over-narrow). Ensure omitted on-genre matches appear too.
                on_first = [tid for tid in valid if tid in on_set]
                for tid in on_genre:
                    if tid not in on_first:
                        on_first.append(tid)
                rest = [tid for tid in valid if tid not in on_set]
                valid = (on_first + rest)[:FINAL_TOP_N]

    tappable = parsed.get("tappable_options", []) or []
    tappable = [str(t).strip() for t in tappable if str(t).strip()][:3]

    response_text = (parsed.get("response") or "").strip()

    # Hard guarantee: never return empty when candidates exist. The reranker
    # (a small model) sometimes declines to pick anything on a preference
    # mismatch, which strands the UI with no cards. Backfill with the candidates
    # in their given order.
    if not valid and candidates:
        valid = list(candidates.keys())[:FINAL_TOP_N]
        response_text = ""

    # If genre-faithful selection dropped off-genre titles the reranker had
    # included, its explanation may describe genres/picks no longer shown
    # (e.g. "blends comedy with sci-fi" over a comedy-only result) — neutralize it.
    if dropped_offgenre:
        response_text = ""

    # The small reranker also ignores the "never apologize" instruction and
    # writes hedging/redirect copy ("only candidate", "explore other platforms",
    # "doesn't quite match"). Detect that and swap in a neutral, positive line so
    # the response never apologizes or sends the user elsewhere.
    if not response_text or _looks_hedgy(response_text):
        response_text = ("Here's a pick you might like." if len(valid) == 1
                         else "Here are a few you might like.")
        if relaxed is not None:
            response_text = ""  # the lead already introduces the picks
    lead = relaxed_lead(relaxed)
    if lead:
        # The lead already introduces the picks; a second "Here are..." from the
        # reranker reads as a repeat, so drop its line if it still opens that way.
        if re.match(r"here(?:'s| are| is)\b", response_text, re.IGNORECASE):
            response_text = ""
        response_text = f"{lead} {response_text}".strip()

    return {
        "response": response_text,
        "title_ids": valid,
        "tappable_options": tappable,
        "usage": usage,
        "latency_s": round(elapsed, 3),
    }


def _presign_image_if_s3(rec: dict) -> None:
    """If a record's poster `image` is an S3 URL (s3://bucket/key), replace it in
    place with a presigned HTTPS URL so the frontend can display it directly.

    HTTP(S) images are left untouched. Any failure leaves the original value so a
    poster issue never sinks the recommendation.
    """
    img = rec.get("image")
    if not isinstance(img, str) or not img.startswith("s3://"):
        return
    try:
        presigned = generate_presigned_url(img)
        if presigned and presigned != img:
            rec["image"] = presigned
    except Exception as e:
        print(f"[reduce] image presign failed for {rec.get('title_id', '?')}: {e}")


def build_recommendations(title_ids: list[str]) -> tuple[list[dict], dict[str, str]]:
    """Rehydrate ordered title_ids into full display records + a content_urls map.

    Returns (recommendations, content_urls). Records preserve the reranked order;
    ids missing from OpenSearch are skipped. content_urls only contains the demo
    titles that have a playable mapping.
    """
    if not title_ids:
        return [], {}

    records = fetch_records_by_ids(title_ids)
    recommendations: list[dict] = []
    content_urls: dict[str, str] = {}
    seen: set[str] = set()
    for tid in title_ids:
        if tid in seen:  # defensive: never rehydrate the same title twice
            continue
        seen.add(tid)
        rec = records.get(tid)
        if not rec:
            continue
        # Poster: if the indexed image is an S3 URL, swap in a presigned HTTPS URL.
        _presign_image_if_s3(rec)
        recommendations.append(rec)
        try:
            url = get_content_url(tid)
        except Exception as e:
            print(f"[reduce] content_url failed for {tid}: {e}")
            url = None
        if url:
            content_urls[tid] = url
    return recommendations, content_urls
