"""Answer step: reply to a question about titles already shown, without a new search.

Runs only when the understanding step returns intent='answer' (no retrieval, no
rerank). The model gets the conversation plus catalog facts for the titles shown
in this session, so answers about plot, cast, director, rating or age suitability
are grounded in the catalog rather than invented.
"""
from __future__ import annotations

import json
import re

from config import OPENSEARCH_INDEX, REDUCE_MODEL_ID
from llm import call

ANSWER_FIELDS = ["title_id", "originalTitle", "premiere_year", "genres", "imdb_rating",
                 "mpaa_ratings", "principalCastMembers", "principalCrewMembers", "awards", "plot"]
MAX_TITLES = 15
PLOT_CHARS = 600

ANSWER_SYSTEM = (
    "You are the friendly concierge of a movie and TV streaming service. The viewer asked a "
    "question about titles already shown to them. Answer it in 1-3 short, warm, spoken-style "
    "sentences.\n"
    "- Use the catalog facts provided for each shown title first. You may add widely known facts "
    "about a well-known title, but never invent specifics you are unsure of (runtime, box "
    "office, release details): say you're not sure instead.\n"
    "- Resolve references like 'the first one', 'the second one', 'that one' by the order in "
    "[Titles shown, most recent first], unless the conversation makes another title clear.\n"
    "- Age suitability: use the rating (mpaa) and plot; say what the rating is.\n"
    "- Do not recommend new titles. If the viewer seems to want more options, invite them to ask "
    "for more.\n"
    "- Small talk (thanks, hello): reply briefly and offer to help find something.\n"
    'Output ONLY JSON: {"response": "<your answer>", "title_ids": [<ids of the shown titles '
    'your answer is about>], "tappable_options": [<1-3 short follow-ups, 2-4 words each>]}'
)


def shown_titles(session) -> list[str]:
    """title_ids shown in this session, most recent turn first, de-duplicated."""
    if not session:
        return []
    turns = getattr(session, "_turns", None)
    if turns is None:
        turns = getattr(session, "turns", None) or []
    out: list[str] = []
    for t in reversed(turns):
        for tid in t.get("recommended_ids") or [r.get("title_id") for r in t.get("recommendations") or []]:
            if tid and tid not in out:
                out.append(tid)
    return out[:MAX_TITLES]


def _facts(title_ids: list[str]) -> list[dict]:
    if not title_ids:
        return []
    from retrieval import get_client
    body = {"_source": ANSWER_FIELDS, "size": len(title_ids),
            "query": {"terms": {"title_id": title_ids}}}
    hits = {h["_source"]["title_id"]: h["_source"]
            for h in get_client().search(index=OPENSEARCH_INDEX, body=body)["hits"]["hits"]}
    return [hits[t] for t in title_ids if t in hits]


def _fmt(i: int, r: dict) -> str:
    def j(v):
        return ", ".join(map(str, v)) if isinstance(v, list) else (v or "")
    plot = (r.get("plot") or "").replace("\n", " ")[:PLOT_CHARS]
    return (f"{i}. {r.get('originalTitle')} [{r.get('title_id')}] | {r.get('premiere_year', '')} | "
            f"genres: {j(r.get('genres'))} | rating {r.get('imdb_rating', '')} | rated {j(r.get('mpaa_ratings'))} | "
            f"cast: {j(r.get('principalCastMembers'))[:300]} | crew: {j(r.get('principalCrewMembers'))[:200]} | "
            f"awards: {j(r.get('awards'))[:150]} | plot: {plot}")


def answer_question(query: str, history: list[str], title_ids: list[str]) -> dict:
    """Return {response, title_ids, tappable_options, usage, latency_s}."""
    try:
        facts = _facts(title_ids)
    except Exception as e:  # noqa: BLE001 -- answer from history alone
        print(f"[answer] catalog lookup failed: {e}")
        facts = []
    ctx = ""
    if history:
        ctx += "[Conversation so far]\n" + "\n".join(f"- {h}" for h in history) + "\n\n"
    if facts:
        ctx += "[Titles shown, most recent first]\n" + "\n".join(_fmt(i + 1, r) for i, r in enumerate(facts)) + "\n\n"
    text, usage, elapsed = call(ANSWER_SYSTEM, f"{ctx}[Viewer's question]: {query}",
                                model_id=REDUCE_MODEL_ID, temperature=0.2, max_tokens=300)
    try:
        d = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    except Exception:  # noqa: BLE001
        d = {"response": text.strip()}
    known = {r["title_id"] for r in facts}
    return {
        "response": str(d.get("response") or "").strip() or "Sorry, I'm not sure about that one.",
        "title_ids": [t for t in d.get("title_ids") or [] if t in known],
        "tappable_options": [str(o) for o in (d.get("tappable_options") or [])][:3],
        "usage": usage, "latency_s": round(elapsed, 3),
    }
