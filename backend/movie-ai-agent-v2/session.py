"""Session memory for multi-turn conversations.

Tracks conversation history and previously recommended title_ids
so the agent can maintain context and avoid repeat recommendations.
"""

from config import MAX_EXCLUDE_IDS


def merge_recent(new_ids: list[str], shown: list[str]) -> list[str]:
    """Session exclusion list: the newest batch first, then older picks, deduped,
    capped at MAX_EXCLUDE_IDS so the oldest drop off."""
    out: list[str] = []
    for i in list(new_ids) + list(shown):
        if i and i not in out:
            out.append(i)
    return out[:MAX_EXCLUDE_IDS]


class SessionMemory:
    """Lightweight in-memory session state for a single user conversation."""

    def __init__(self):
        self.turns: list[dict] = []  # [{query, recommended_titles, recommended_ids}]
        self.recommended_ids: list[str] = []  # shown this session, newest first (exclusion window)

    def add_turn(self, query: str, recommendations: list[dict], response: str = "") -> None:
        """Record a completed turn."""
        titles = [r.get("originalTitle", "?") for r in recommendations]
        ids = [r.get("title_id") for r in recommendations if r.get("title_id")]
        self.turns.append({
            "query": query,
            "recommended_titles": titles,
            "recommended_ids": ids,
            "recommendations": recommendations,
            "response": response,
        })
        # Exclude everything shown this session (newest first), capped.
        # An answer-only turn (no titles) leaves the list unchanged.
        if ids:
            self.recommended_ids = merge_recent(ids, self.recommended_ids)

    def get_history_text(self) -> str:
        """Format conversation history for the planner/router input."""
        if not self.turns:
            return ""
        lines = []
        for i, turn in enumerate(self.turns, 1):
            titles = ", ".join(turn["recommended_titles"])
            if titles:
                lines.append(f"Turn {i}: User: \"{turn['query']}\" → Recommended: {titles}")
            else:
                # Non-recommendation turn — include the response so downstream agents
                # can resolve references like "this one" or "that movie"
                resp = turn.get("response", "")
                summary = resp[:200] + "..." if len(resp) > 200 else resp
                lines.append(f"Turn {i}: User: \"{turn['query']}\" → Assistant: {summary}")
        return "\n".join(lines)

    def get_excluded_ids(self) -> list[str]:
        """Return title_ids shown this session, newest first (capped)."""
        return list(self.recommended_ids)

    def ensure_ltm_refresher(self) -> None:
        """No-op: local SessionMemory has no long-term store."""
        return None

    def get_long_term_context(self) -> str:
        """No-op: local SessionMemory has no long-term store."""
        return ""

    def get_user_profile_context(self) -> str:
        """No-op: local SessionMemory has no seeded profile."""
        return ""

    def get_watched_ids(self) -> list[str]:
        """No-op: local SessionMemory has no watch history."""
        return []

    def get_taste_vectors(self) -> list:
        """No-op: local SessionMemory has no taste centroids."""
        return []

    def get_declared(self) -> tuple[list, dict]:
        """No-op: local SessionMemory has no L3 rules."""
        return [], {}

    def get_session_behavior(self) -> None:
        """No-op: local SessionMemory has no behaviour facts."""
        return None

    def get_last_recommendations(self) -> list[dict]:
        """Return full recommendation data from the most recent turn."""
        # Most recent turn that showed titles (answer-only turns show none).
        for t in reversed(self.turns):
            if t.get("recommendations"):
                return t["recommendations"]
        return []
