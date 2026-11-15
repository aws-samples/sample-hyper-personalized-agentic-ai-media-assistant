"""Intent gate (Phase 0).

Decides whether the user's message needs a database search. If not (greetings,
questions about already-discussed movies, off-topic), the model answers
directly and the pipeline short-circuits — mirroring v1's router phase and
preserving the same response contract for non-recommendation turns.
"""

from __future__ import annotations

from pathlib import Path

from config import INTENT_MODEL_ID
from llm import call

_ROUTER_PROMPT = (Path(__file__).parent / "prompts" / "router.txt").read_text()


def classify_intent(user_query: str, session=None) -> dict:
    """Return {"is_recommendation": bool, "answer": str|None, "usage", "latency_s"}.

    When not a recommendation, `answer` holds the model's direct reply. Session
    history + previously recommended movies are provided so the model can answer
    follow-up questions about earlier picks.
    """
    router_input = ""
    if session and session.get_history_text():
        router_input += f"## Conversation History\n{session.get_history_text()}\n\n"
    if session:
        last_recs = session.get_last_recommendations()
        if last_recs:
            router_input += "## Previously Recommended Movies\n"
            for r in last_recs:
                router_input += (
                    f"- {r.get('originalTitle', '?')} ({r.get('title_id', '?')})"
                    f" | Genres: {r.get('genres', 'N/A')}"
                    f" | rating: {r.get('imdb_rating', 'N/A')}"
                    f" | Cast: {r.get('principalCastMembers', 'N/A')}"
                    f" | Plot: {r.get('plot', 'N/A')}\n"
                )
            router_input += "\n"
    router_input += f"## User Message\n{user_query}"

    text, usage, elapsed = call(
        _ROUTER_PROMPT, router_input,
        model_id=INTENT_MODEL_ID, temperature=0.0, max_tokens=512,
    )

    is_rec = text.strip().upper() == "RECOMMENDATION"
    return {
        "is_recommendation": is_rec,
        "answer": None if is_rec else text,
        "usage": usage,
        "latency_s": round(elapsed, 3),
    }
