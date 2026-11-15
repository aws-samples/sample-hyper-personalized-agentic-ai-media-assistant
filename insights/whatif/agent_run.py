"""Run the REAL movie-agent pipeline (backend/movie-ai-agent-v2, bundled
unchanged) against a sandbox profile, recording every stage.

The agent normally reads its member context through an AgentCore memory
session object. `SandboxSession` provides the same methods from the sandbox
instead, using the agent's own user_profile functions for the taste text,
taste vectors and L3 rules (pointed at the sandbox through a stand-in table).
Stage functions are wrapped, not changed, to record inputs and outputs.
Writes the agent would make (memory turns, impression events) are disabled.
"""

import threading
import time

import agent_v2
import stream
import user_profile
from memory import AgentCoreMemory

_local = threading.local()


def _rec():
    return getattr(_local, "rec", None)


class _ProfileTable:
    """Stand-in for the agent's profiles table, reading one sandbox."""

    def get_item(self, Key, **_kw):
        sb = _local.sandbox
        item = sb.profiles.items.get(Key["data_type"]) if Key.get("user_id") == sb.profile_id else None
        return {"Item": item} if item else {}


user_profile._get_table = lambda: _ProfileTable()   # noqa: E731 (module-level redirect)


class SandboxSession:
    """The subset of AgentCoreMemory that run_agent_stream uses."""

    actor_id = ""        # empty: emit_impressions writes nothing
    session_id = ""
    _turns: list = []

    def __init__(self, sandbox, now, ltm_text=""):
        self.sb, self.now, self.ltm = sandbox, now, ltm_text

    def ensure_ltm_refresher(self):
        pass

    def get_long_term_context(self):
        return self.ltm

    def get_user_profile_context(self):
        # Same text as user_profile.get_profile_block, rendered at the sandbox's time of day.
        lines = user_profile.render_affinities(self.sb.affinities, self.sb.declared.get("excluded_genres") or [],
                                               now=self.now)
        if lines:
            return "\n".join(["Viewer taste (from viewing history; soft guidance, never override the request):", *lines])
        return ""

    def get_watched_ids(self):
        return self.sb.completed()

    def get_taste_vectors(self):
        return user_profile.get_taste_vectors(self.sb.profile_id, self.get_watched_ids())

    def get_declared(self):
        return user_profile.get_declared(self.sb.profile_id)

    def get_session_behavior(self):
        return None

    def get_excluded_ids(self):
        return []

    def get_last_recommendations(self):
        return []

    def add_turn(self, *_a, **_kw):
        pass


def _wrap(name, record, before=None):
    original = getattr(agent_v2, name)

    def wrapper(*args, **kwargs):
        if before:
            before(args, kwargs)
        out = original(*args, **kwargs)
        rec = _rec()
        if rec is not None:
            record(rec, args, kwargs, out)
        return out
    setattr(agent_v2, name, wrapper)


def _retrieval(path):
    def record(rec, args, kwargs, out):
        ids = list((out or {}).get("candidates") or {})
        rec.setdefault("retrieval", []).append({"path": path, "ids": ids})
        stream.emit("retrieval", path=path, ids=ids)
    return record


def _understood(rec, a, kw, d):
    rec["understand"] = {k: d.get(k) for k in [
        "intent", "path", "category", "search_text", "knn_text", "genres_include", "genres_exclude",
        "avoid_soft", "mood", "person", "keywords", "exact_title", "overridden_rules", "active_rules"]}
    stream.emit("understood", **rec["understand"])


def _reduced(rec, a, kw, red):
    rec["reduce"] = {"candidates": len(a[1]) if len(a) > 1 else None, "exclude_ids": list(kw.get("exclude_ids") or []),
                     "preferences": kw.get("preferences") or "", "title_ids": list(red.get("title_ids") or []),
                     "response": red.get("response") or ""}
    stream.emit("reranked", title_ids=rec["reduce"]["title_ids"], response=rec["reduce"]["response"])


def _rerank_input(a, kw):
    cands = a[1] if len(a) > 1 else {}
    excl = set(kw.get("exclude_ids") or [])
    stream.emit("rerank_input", ids=list(cands), excluded=[t for t in cands if t in excl],
                preferences=kw.get("preferences") or "")


_wrap("understand", _understood)
for _name, _path in [("catch_all_taste", "taste"), ("catch_all", "catch_all"), ("fan_out", "fanout"),
                     ("catch_all_semantic", "semantic_fallback"), ("catch_all_short", "short_fallback")]:
    _wrap(_name, _retrieval(_path))
_wrap("reduce", _reduced, before=_rerank_input)
# No DynamoDB/S3 lookups for playback URLs: the console only needs the ids.
agent_v2.build_recommendations = lambda ids: ([{"title_id": t} for t in ids], {})
agent_v2.emit_impressions = lambda *a, **kw: None
stream.install()


def run(sandbox, query, now, ltm_text="", emitter=None, side=None):
    """{statuses, understand, retrieval, reduce, picks, taste_text, seconds}; streams events to `emitter`."""
    _local.sandbox = sandbox
    _local.rec = rec = {"statuses": []}
    stream.bind(emitter, side)
    session = SandboxSession(sandbox, now, ltm_text)
    started = time.time()
    stream.emit("agent_start", query=query, taste_text=session.get_user_profile_context(),
                watched=len(session.get_watched_ids()))
    try:
        for ev in agent_v2.run_agent_stream(query, session=session):
            if ev.get("type") == "status":
                st = {"phase": ev.get("phase"), "message": ev.get("message"), "t": round(time.time() - started, 2)}
                rec["statuses"].append(st)
                stream.emit("status", **st)
            elif ev.get("type") == "response":
                rec["final"] = {"intent": ev.get("intent"), "response": ev.get("response")}
    finally:
        _local.rec = None
    rec["taste_text"] = session.get_user_profile_context()
    rec["picks"] = (rec.get("reduce") or {}).get("title_ids") or []
    rec["seconds"] = round(time.time() - started, 2)
    stream.emit("agent_done", seconds=rec["seconds"], picks=rec["picks"])
    stream.bind(None, None)
    return rec


def long_term_memory(memory_id, profile_id, region):
    """The member's consolidated AgentCore preferences, formatted as the agent does (read only)."""
    if not memory_id:
        return "", []
    import boto3
    client = boto3.client("bedrock-agentcore", region_name=region)
    resp = client.retrieve_memory_records(
        memoryId=memory_id, namespace=f"/preferences/{profile_id}",
        searchCriteria={"searchQuery": ("user's entertainment and sports preferences: liked and disliked genres, "
                                        "themes, favorite sports, teams, players, actors and directors"), "topK": 5})
    lines, raw = ["Viewer preferences (from this viewer's history):"], []
    for rec in resp.get("memoryRecordSummaries") or []:
        text = (rec.get("content") or {}).get("text", "")
        cleaned = AgentCoreMemory._clean_ltm_entry(text) if text else ""
        if cleaned:
            raw.append(cleaned)
            lines.append(f"- {cleaned[:197] + '...' if len(cleaned) > 200 else cleaned}")
    return ("\n".join(lines) if len(lines) > 1 else ""), raw
