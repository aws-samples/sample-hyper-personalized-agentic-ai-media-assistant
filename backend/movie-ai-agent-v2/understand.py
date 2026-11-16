"""Understanding step: one LLM call that reads the request and decides the route.

Reads the latest message + the conversation (never the viewer's preferences) and
returns structured intent. Fixed code (route/filters) derives the path from the
fields; nothing is generated at runtime. The member's L3 preference rules are
shown only so the same call can report which ones the request overrides
(apply_profile() then applies the active ones). Limits never reach the model.

The system prompt (+ worked examples in fewshot.py) is >4,096 tokens so Bedrock
caches it for Haiku 4.5. Evaluation suites: evals/ (not shipped in the image).
"""
from __future__ import annotations

import json
import re

from config import UNDERSTAND_MODEL_ID, UNDERSTAND_CACHE_TTL
from fewshot import render as _render_examples
from llm import call, cached_system
from router import FILTER_KEYS, normalize_genres

UNDERSTAND_SYSTEM = (
    "You interpret the viewer's latest message in a movie/TV recommendation chat and output "
    "their CURRENT intent as JSON. You see only what the viewer said; you never see or guess "
    "their long-term taste, so never add genres, moods or people they did not state.\n\n"
    "How to read the conversation:\n"
    "- The latest message has the highest priority. Earlier requests still apply unless the "
    "latest message changes or drops them: a short follow-up ('more upbeat', 'up to you', "
    "'but skip the cartoons') REFINES the request built so far, so carry forward its format, "
    "genres, mood, people, era and avoids, then apply the change on top. If the latest message "
    "reverses something ('actually westerns are okay'), drop it.\n"
    "- Every preference has a POLARITY: something the viewer wants (include) or does not want "
    "(avoid). Never lose an avoid -- it matters as much as what they want.\n"
    "- Every avoid has a STRENGTH:\n"
    "  * hard: the viewer rules a whole catalog genre out completely ('no westerns', 'anything "
    "except documentaries'). Only these go in genres_exclude.\n"
    "  * soft: the viewer wants LESS of a quality, tone, theme, person or element, or limits "
    "degree ('not too sappy', 'go easy on the violence', 'without much swearing', 'nothing "
    "with Jim Carrey', 'not too slow'). These go in avoid_soft as short phrases describing the "
    "unwanted quality. A degree word ('too', 'very', 'overly') always means soft. Concepts that "
    "are not catalog genres (a theme, an element, a person, a tone) are always soft.\n"
    "- open_ended is true when the viewer states NOTHING they want -- no genre, mood, theme, "
    "person, title, era, rating, audience or format -- and leaves the pick to the recommender. "
    "Avoids alone do not make a request specific: 'up to you, just no westerns' is open_ended. "
    "An avoid never implies a want: 'nothing gloomy' is an avoid only -- do not turn it into a "
    "mood or search_text such as 'uplifting'. A request that names options ('X or maybe Y') "
    "is not open_ended, even if it also says 'you choose'.\n"
    "- search_text: a short positive description of what to look for, used for semantic "
    "search. It must contain ONLY wanted qualities: an embedding treats 'without X' or 'less X' "
    "as MORE X, so never mention an avoided quality there, not even negated. Restate the want "
    "positively instead (an avoid of 'too bloody' on a crime request -> 'restrained, "
    "suspenseful crime story'). Every avoid belongs in genres_exclude or avoid_soft only. "
    "Empty string when open_ended.\n\n"
    "JSON keys:\n"
    "category ('movie'|'tv'|'both'|'micro-drama'; default 'movie' -- a generic word like "
    "'watch', 'something' or 'anything' still means 'movie'; 'tv' only when TV/series/show is "
    "stated; 'both' only when the viewer says either is fine; 'micro-drama' only when explicitly "
    "asked for), person (name, list of names, or "
    "null), exact_title (ONLY when the viewer wants that specific title ITSELF -- to play, "
    "find or check it: 'play Jaws', 'is Jaws on here?'. A title used as a reference for what "
    "they want -- 'like Jaws', 'in the vein of Jaws', 'I loved Jaws, more of that' -- is NOT "
    "exact_title: leave exact_title null and describe the wanted qualities in search_text, "
    "where you may name the reference title), genres_include (list), genres_exclude (list, hard avoids only), avoid_soft (list "
    "of short phrases), min_year (int|null), max_year (int|null), min_rating (float|null), "
    "kids (true if for children/family-safe), awards (true if acclaimed/award-winning asked), "
    "keywords (concrete plot elements wanted, e.g. heist, time travel), mood (residual vibe "
    "words wanted), other_constraints (list of short phrases for any other requirement the "
    "viewer states that no field above captures -- never drop a stated requirement), "
    "search_text (string), open_ended (bool), intent ('recommend'|'answer', see below).\n"
    "genres_include/genres_exclude use ONLY these tokens: drama, thriller, action, comedy, "
    "adventure, crime, sci-fi, fantasy, mystery, animation, romance, horror, biography, family, "
    "history, war, sport, music, musical, short, western, documentary, talk-show, reality-tv, "
    "news, game-show. A rom-com is romance + comedy. Eras: '90s' -> 1990..1999, 'recent' / "
    "'last few years' -> min_year about 5 years before the current year (2026). Ratings in this "
    "catalog are 7.0-9.9, so 'highly rated' -> 8.0.\n\n"
    "STANDING RULES. The viewer may have standing rules from their settings, listed at the very "
    "end under [Settings rules] (free text, e.g. 'avoid genre: western', 'avoid theme: "
    "smoking'). The viewer did NOT say them in this chat, and the system enforces them by itself "
    "after you answer. So:\n"
    "- Extract every field ONLY from what the viewer said in this conversation, exactly as you "
    "would with no rules. A settings rule must never appear in genres_exclude, avoid_soft or "
    "any other field. Before answering, check that every entry in genres_exclude and "
    "avoid_soft is something the viewer actually said in the conversation; remove any that is "
    "not.\n"
    "- Your only use of the rules is the key `overrides`: list the exact text of each rule the "
    "viewer's CURRENT request goes against, i.e. what they are asking for right now IS, or "
    "clearly CONTAINS, what the rule avoids. Judge by meaning, not by shared words:\n"
    "  * a description counts: 'a noir with chain-smoking detectives' overrides 'avoid theme: "
    "smoking'; 'cowboys, saloons and a showdown at noon' overrides 'avoid genre: western';\n"
    "  * intensity and style words count: 'creepy-crawly, eight-legged nightmares' overrides "
    "'avoid theme: spiders';\n"
    "  * permission counts: 'I don't mind smoking in it', 'smoking is fine tonight' override "
    "'avoid theme: smoking';\n"
    "  * a subject counts: a documentary or drama ABOUT the avoided thing overrides the rule.\n"
    "- Judge each rule separately: asking for a genre does not override a theme rule unless "
    "the viewer also asks for that content ('a western' overrides 'avoid genre: western' but "
    "NOT 'avoid theme: smoking').\n"
    "- NOT an override: a request that is merely compatible with a rule, vague ('something "
    "intense', 'a thriller'), or silent about it. When unsure, do not override -- the viewer's "
    "rules then stay in force.\n"
    "- Across turns: an override holds while the current request still builds on the request "
    "that made it ('more like that', 'another one'). It ends when the viewer takes it back "
    "('skip the smoking again') or asks for something different ('ok, something funny "
    "instead').\n"
    "- `overrides` is [] when there are no rules or nothing is overridden.\n\n"
    "INTENT. intent is 'recommend' (the default) whenever the viewer wants titles: to find, "
    "search, get more, get different or similar ones, refine the last results, or check whether "
    "a title is in the catalog. intent is 'answer' when the viewer asks a QUESTION about titles "
    "already shown or discussed in this conversation (listed under [Already recommended]) -- "
    "their plot, cast, director, length, rating, age suitability, why they were picked, how two "
    "of them compare, which one to start with -- or makes small talk, so the reply is an answer, "
    "not a new list. Asking for MORE titles about the same thing is still 'recommend' ('more "
    "like the second one'). With intent 'answer', still fill the other fields as best you can "
    "(they are ignored).\n\n"
    "Output only the JSON object."
)

_BLANK_LISTS = ("genres_include", "genres_exclude", "avoid_soft", "keywords", "other_constraints")
# Worked examples are appended so the prompt passes Haiku 4.5's 4,096-token cache
# minimum (and improve accuracy). The cachePoint makes Bedrock reuse it across calls.
UNDERSTAND_SYSTEM = UNDERSTAND_SYSTEM + _render_examples()
_SYSTEM = cached_system(UNDERSTAND_SYSTEM, ttl=UNDERSTAND_CACHE_TTL or None)
_MODEL = UNDERSTAND_MODEL_ID


def understand(latest: str, history: list[str], prev_recs: list[str],
               rules: list[str] | None = None) -> dict:
    """`rules` = the member's L3 PREFERENCE rules as free text. They are used only for the
    `overrides` key of this same call (no extra LLM or embedding call). Limits are never
    passed to any model."""
    ctx = ""
    if history:
        ctx += "[Earlier requests in this conversation]\n" + "\n".join(f"- {h}" for h in history) + "\n\n"
    if prev_recs:
        ctx += "[Already recommended]\n" + "\n".join(f"- {t}" for t in prev_recs) + "\n\n"
    tail = ""
    if rules:
        # After the message, outside the conversation, labelled as not said by the viewer.
        tail = ("\n\n[Settings rules -- NOT said by the viewer in this chat. Use ONLY to fill "
                "`overrides`; they must not appear in any other field]\n"
                + "\n".join(f"- {r}" for r in rules))
    try:
        text, usage, elapsed = call(_SYSTEM, f"{ctx}[Latest message]: {latest}{tail}",
                                    model_id=_MODEL, temperature=0.0, max_tokens=500)
    except Exception:  # noqa: BLE001
        text, usage, elapsed = "", {}, 0.0  # parse fails below -> ok=False, raw-text fallback
    try:
        d = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        ok = True
    except Exception:  # noqa: BLE001
        d, ok = {"search_text": latest}, False  # fallback: raw message, fan-out
    d.setdefault("category", "movie")
    for k in ("person", "min_year", "max_year", "min_rating", "exact_title"):
        d.setdefault(k, None)
    for k in _BLANK_LISTS:
        d[k] = [str(x).strip().lower() for x in (d.get(k) or []) if str(x).strip()]
    d["genres_include"] = normalize_genres(d["genres_include"])
    d["genres_exclude"] = normalize_genres(d["genres_exclude"])
    d["kids"], d["awards"] = bool(d.get("kids")), bool(d.get("awards"))
    d["mood"] = d.get("mood") or ""
    if isinstance(d["mood"], list):
        d["mood"] = ", ".join(map(str, d["mood"]))
    d["search_text"] = d.get("search_text") or ""
    d["intent"] = "answer" if str(d.get("intent") or "").strip().lower() == "answer" else "recommend"
    # Overrides: keep only exact texts of rules we passed (anything else is ignored).
    given = {r.lower(): r for r in (rules or [])}
    d["overrides"] = [given[str(o).strip().lower()] for o in (d.get("overrides") or [])
                      if str(o).strip().lower() in given]

    # Hard filters route to catch_all, except an exclusion alone does not
    # make a request specific (open + hard avoid -> taste with must_not).
    positive = [k for k in FILTER_KEYS if k != "genres_exclude"]
    # kids alone does not make a request filtered: fan-out serves it from the
    # kids shard. With any other filter, catch_all applies the kids ratings too.
    d["has_filter"] = any(d.get(k) for k in FILTER_KEYS if k != "kids")
    d["open_ended"] = (bool(d.get("open_ended")) and not any(d.get(k) for k in positive)
                       and not d["search_text"].strip() and not d["mood"].strip()
                       and not d["genres_include"] and not d["keywords"]
                       and d["category"] in ("movie", "both"))
    d["path"] = "taste" if d["open_ended"] else ("catch_all" if d["has_filter"] else "fanout")
    if d["category"] == "micro-drama":
        d["path"] = "catch_all"
    # Embedding text: positive fields only, so no avoid can reach a kNN query.
    d["knn_text"] = " ".join(p for p in [d["mood"]] + d["genres_include"] + d["keywords"] if p).strip()
    # LLM-stage text (fan-out shards, reduce): the request plus explicit avoids.
    avoids = d["genres_exclude"] + d["avoid_soft"]
    d["llm_text"] = (d["search_text"] or "surprise me with a movie") + (
        f". Requirements: {', '.join(d['other_constraints'])}" if d["other_constraints"] else "") + (
        f". Avoid: {', '.join(avoids)}" if avoids else "")
    d["_ok"], d["_usage"], d["_s"] = ok, usage, round(elapsed, 3)
    return d


def apply_profile(d: dict, prefs: list[dict], limits: dict) -> dict:
    """Precedence, highest first: limits > this conversation (incl. overrides) > L3 prefs.

    prefs:  [{"kind": "genre"|"theme", "value": str, "text": str}] -- L3 preference rules.
    limits: {"kids": bool, "genres": [...]} --
            guardrails; never shown to the model, never overridable.
    Mutates and returns d with:
      rule_genres_exclude  -> must_not on catch_all/taste, post-filter on fan-out
      rule_avoid_soft      -> passed to reduce()
      active_rules / overridden_rules for logging and the reduce preference text.
    The route was decided from the request alone and is not changed here.
    """
    # Overrides come from the model (meaning-based, same call). A rule genre the request
    # itself includes is also overridden (closed catalog field, exact token).
    want = set(d["genres_include"])
    ov = set(d.get("overrides") or []) | {p["text"] for p in prefs
                                          if p["kind"] == "genre" and p["value"] in want}
    d["overrides"] = sorted(ov)
    active = [p for p in prefs if p["text"] not in ov]
    d["overridden_rules"] = [p["text"] for p in prefs if p["text"] in ov]
    d["active_rules"] = [p["text"] for p in active]
    d["rule_genres_exclude"] = [p["value"] for p in active if p["kind"] == "genre"]
    d["rule_avoid_soft"] = [p["value"] for p in active if p["kind"] == "theme"]
    # Limits: enforced unconditionally, after everything else.
    d["limit_genres_exclude"] = list(limits.get("genres") or [])
    if limits.get("kids"):
        d["kids"] = True  # fan-out then reads only the kids shard
    rule_avoids = d["rule_genres_exclude"] + d["rule_avoid_soft"] + d["limit_genres_exclude"]
    if rule_avoids:
        d["llm_text"] += f". Viewer's standing avoids: {', '.join(rule_avoids)}"
    return d
