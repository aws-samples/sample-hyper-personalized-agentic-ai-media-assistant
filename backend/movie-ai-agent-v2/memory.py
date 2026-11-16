"""AgentCore Memory integration for session (STM) and long-term (LTM) memory.

Replaces the in-memory SessionMemory with AgentCore's persistent memory service.
Falls back to local SessionMemory if AGENTCORE_MEMORY_ID is not configured.

Memory strategies:
- STM: Raw conversation events within a session (auto-managed by AgentCore)
- LTM summary: Consolidates session interactions into a summary for cross-session context
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from config import AWS_REGION, AGENTCORE_MEMORY_ID, LTM_CACHE_TTL_S, PROFILE_TTL_S
from session import merge_recent as _merge_recent

logger = logging.getLogger(__name__)

_memory_client = None

# Background writer for short-term-memory events (created on first use, so no
# thread exists when the runtime snapshots the process after startup).
_event_writer: ThreadPoolExecutor | None = None
_event_writer_lock = threading.Lock()


def _writer() -> ThreadPoolExecutor:
    global _event_writer
    if _event_writer is None:
        with _event_writer_lock:
            if _event_writer is None:
                _event_writer = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stm-write")
    return _event_writer


def _get_memory_client():
    """Get or create a singleton MemoryClient."""
    global _memory_client
    if _memory_client is None:
        from bedrock_agentcore.memory import MemoryClient
        _memory_client = MemoryClient(region_name=AWS_REGION)
    return _memory_client


class AgentCoreMemory:
    """Persistent memory backed by AgentCore Memory service.

    Provides the same interface as SessionMemory but persists across restarts.
    Uses STM for conversation history and LTM for cross-session context.
    """

    def __init__(self, memory_id: str, session_id: str, actor_id: str):
        self.memory_id = memory_id
        self.session_id = session_id
        self.actor_id = actor_id
        self.client = _get_memory_client()
        # Local cache for current session (avoids repeated API calls)
        self._recommended_ids: list[str] = []         # shown this session, newest first
        self._last_recommendations: list[dict] = []
        self._turns: list[dict] = []
        self._ltm_cache: str = ""                      # preference block, kept fresh
        self._ltm_thread: threading.Thread | None = None  # background refresher
        self._ltm_stop = threading.Event()
        self._ltm_lock = threading.Lock()
        self._profile_cache: str | None = None         # L2 taste text (refreshed, PROFILE_TTL_S)
        self._watched_cache: list[str] | None = None   # completed titles to exclude (refreshed)
        self._taste_cache: list | None = None          # L2 taste centroids (refreshed)
        self._loaded_at: dict[str, float] = {}         # cache name -> monotonic load time

    def _stale(self, name: str) -> bool:
        """True when cache `name` was never loaded or is older than PROFILE_TTL_S.
        The stream processor updates L2 and watch state while a session runs, so
        these are re-read about once a minute (one GetItem/Query each)."""
        at = self._loaded_at.get(name)
        if getattr(self, f"_{name}_cache") is None or at is None or time.monotonic() - at >= PROFILE_TTL_S:
            self._loaded_at[name] = time.monotonic()
            return True
        return False

    def add_turn(self, query: str, recommendations: list[dict], response: str = "") -> None:
        """Record a turn: save to AgentCore Memory as an event and update local cache."""
        titles = [r.get("originalTitle", "?") for r in recommendations]
        ids = [r.get("title_id") for r in recommendations if r.get("title_id")]

        self._turns.append({
            "query": query,
            "recommended_titles": titles,
            "recommended_ids": ids,
            "recommendations": recommendations,
            "response": response,
        })
        # Exclude everything shown this session, most recent first, capped.
        # An answer-only turn (no titles) keeps the batch it was answering about,
        # so "more like that" / "the second one" still refer to it.
        if recommendations:
            self._recommended_ids = _merge_recent(ids, self._recommended_ids)
            self._last_recommendations = recommendations

        # Save to AgentCore Memory as conversation events. Nothing reads these
        # back during the session (turns live in this process for its lifetime);
        # they feed the long-term strategy, so the write runs off the request path.
        messages = [
            (query, "user"),
        ]
        if response:
            messages.append((response, "assistant"))
        elif titles:
            messages.append((f"Recommended: {', '.join(titles)}", "assistant"))

        # Stamp now so events keep turn order even if background writes overlap.
        stamp = datetime.now(timezone.utc)
        _writer().submit(self._save_event, messages, stamp)

    def _save_event(self, messages: list[tuple[str, str]], stamp: datetime) -> None:
        try:
            self.client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=messages,
                event_timestamp=stamp,
            )
        except Exception as e:
            logger.warning(f"Failed to save event to AgentCore Memory: {e}")

    def get_history_text(self) -> str:
        """Format conversation history from local cache (current session)."""
        if not self._turns:
            return ""
        lines = []
        for i, turn in enumerate(self._turns, 1):
            titles = ", ".join(turn["recommended_titles"])
            if titles:
                lines.append(f"Turn {i}: User: \"{turn['query']}\" → Recommended: {titles}")
            else:
                resp = turn.get("response", "")
                summary = resp[:200] + "..." if len(resp) > 200 else resp
                lines.append(f"Turn {i}: User: \"{turn['query']}\" → Assistant: {summary}")
        return "\n".join(lines)

    def get_excluded_ids(self) -> list[str]:
        """Return the most recent turn's title_ids (exclude only the latest batch)."""
        return list(self._recommended_ids)

    def get_last_recommendations(self) -> list[dict]:
        """Return full recommendation data from the most recent turn."""
        return self._last_recommendations

    @staticmethod
    def _clean_ltm_entry(text: str) -> str:
        """Strip stray XML tags / timestamp prefixes and trim a single entry."""
        import re
        text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)          # any XML-ish tags
        text = re.sub(r"At timestamp \d+,?\s*", "", text)       # timestamp prefixes
        return text.strip()

    def _fetch_ltm(self) -> str:
        """Blocking retrieve of consolidated preferences -> formatted block (or '').

        Reads the `TastePreferences` records written by the custom User Preference
        strategy at namespace `/preferences/{actorId}`. Runs on the background
        refresher thread, never on the request path.
        """
        results = self.client.retrieve_memories(
            memory_id=self.memory_id,
            actor_id=self.actor_id,
            namespace=f"/preferences/{self.actor_id}",
            query=("user's entertainment and sports preferences: liked and "
                   "disliked genres, themes, favorite sports, teams, players, "
                   "actors and directors"),
            top_k=5,
        )
        lines = ["Viewer preferences (from this viewer's history):"]
        for mem in results or []:
            content = mem.get("content", {}).get("text", "")
            cleaned = self._clean_ltm_entry(content) if content else ""
            if cleaned:
                if len(cleaned) > 200:
                    cleaned = cleaned[:197] + "..."
                lines.append(f"- {cleaned}")
        return "\n".join(lines) if len(lines) > 1 else ""

    def _ltm_refresh_loop(self) -> None:
        """Background loop: refresh the preference cache every LTM_CACHE_TTL_S.

        The first fetch runs immediately; on error the previous cache is kept.
        """
        while not self._ltm_stop.is_set():
            try:
                self._ltm_cache = self._fetch_ltm()
            except Exception as e:
                logger.debug(f"LTM refresh failed (keeping previous value): {e}")
            self._ltm_stop.wait(LTM_CACHE_TTL_S)

    def ensure_ltm_refresher(self) -> None:
        """Start the background preference refresher once (idempotent, non-blocking).

        Safe to call on every request; the thread starts only the first time and
        then refreshes the in-memory cache every LTM_CACHE_TTL_S. Triggered from
        run_agent_stream so preference retrieval never blocks the pipeline.
        """
        if os.environ.get("SKIP_LTM", "").lower() in ("true", "1", "yes"):
            return
        if self._ltm_thread is not None:
            return
        with self._ltm_lock:
            if self._ltm_thread is not None:
                return
            self._ltm_thread = threading.Thread(
                target=self._ltm_refresh_loop,
                name=f"ltm-refresh-{self.actor_id}",
                daemon=True,
            )
            self._ltm_thread.start()

    def get_long_term_context(self) -> str:
        """Non-blocking read of the in-memory preference cache.

        Populated by the background refresher (ensure_ltm_refresher). Returns ''
        until the first refresh completes, so it never blocks understand/reduce.
        """
        return self._ltm_cache or ""

    def get_user_profile_context(self) -> str:
        """The member's taste block: L2 AFFINITIES rendered as text ('' for a
        member with no AFFINITIES yet). Re-read every PROFILE_TTL_S so
        viewing during the session shows up; never blocks on failure."""
        if not self._stale("profile"):
            return self._profile_cache
        try:
            from user_profile import get_profile_block
            try:   # this session's UTC offset (L1), for the time-of-day line
                tz = (self.get_session_behavior() or {}).get("tz_offset_min")
            except Exception:  # noqa: BLE001
                tz = None
            self._profile_cache = get_profile_block(self.actor_id, tz)
        except Exception as e:
            logger.debug(f"profile load failed: {e}")
            self._profile_cache = self._profile_cache or ""
        return self._profile_cache

    def get_watched_ids(self) -> list[str]:
        """Member's recently completed titles (re-read every PROFILE_TTL_S)."""
        if self._stale("watched"):
            try:
                from user_profile import get_watched_ids
                self._watched_cache = get_watched_ids(self.actor_id)
            except Exception as e:
                logger.debug(f"watched load failed: {e}")
                self._watched_cache = self._watched_cache or []
        return self._watched_cache

    def get_taste_vectors(self) -> list[tuple[float, list[float]]]:
        """Member's L2 taste centroids as [(weight, vector)] (re-read every PROFILE_TTL_S)."""
        if self._stale("taste"):
            try:
                from user_profile import get_taste_vectors
                self._taste_cache = get_taste_vectors(self.actor_id, self.get_watched_ids())
            except Exception as e:
                logger.debug(f"taste load failed: {e}")
                self._taste_cache = self._taste_cache or []
        return self._taste_cache

    def get_declared(self) -> tuple[list[dict], dict]:
        """Member's L3 (preference rules, limits), loaded once per session."""
        if getattr(self, "_declared_cache", None) is None:
            try:
                from user_profile import get_declared
                self._declared_cache = get_declared(self.actor_id)
            except Exception as e:
                logger.debug(f"declared load failed: {e}")
                self._declared_cache = ([], {})
        return self._declared_cache

    def get_session_behavior(self) -> dict | None:
        """L1 SESSION#behavior facts for this session. Read every turn (one
        GetItem): the stream processor updates them within seconds of playback."""
        from session_behavior import read
        return read(self.actor_id, self.session_id)


def create_memory(session_id: str, actor_id: str = "demo-user"):
    """Factory: create AgentCoreMemory if configured, else fall back to SessionMemory."""
    if AGENTCORE_MEMORY_ID:
        logger.info(f"Using AgentCore Memory: {AGENTCORE_MEMORY_ID}")
        return AgentCoreMemory(
            memory_id=AGENTCORE_MEMORY_ID,
            session_id=session_id,
            actor_id=actor_id,
        )
    else:
        from session import SessionMemory
        logger.info("AGENTCORE_MEMORY_ID not set — using local SessionMemory")
        return SessionMemory()
