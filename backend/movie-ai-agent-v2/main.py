"""AgentCore entry point for movie-ai-agent-v2.

Streams status updates while the recommendation pipeline runs. Request and
response payloads match movie-ai-agent:

  request : {request_type?, prompt|user_query, profile?, session_id?, user_id?}
  status  : {type:"status", message, phase, step, total_steps, timestamp}
  final   : {type:"response", response, movie-recommendation, tappable_options, content_urls?}
  error   : {type:"error", message}

Startup (`_warm_startup`) builds static clients and imports before app.run(),
so on AgentCore Runtime V2 they are captured in the snapshot. Anything that
varies per request (session ids, randomness, time, credentials) stays in the
handler. Shard prompt caches are kept warm externally (backend/shard-warmer/).
"""

import asyncio
import json
import re
import time
import uuid

from agent_v2 import run_agent_stream, run_agent
from memory import create_memory

# In-memory session store keyed by (session_id, user_id). Keying on user_id too
# (which encodes the profile, e.g. "demo-user-action_fan") means a profile switch
# on a pinned/booth session gets its own memory object and the correct actor
# namespace for long-term memory, instead of reusing the first profile's actor.
_sessions: dict[tuple[str, str], object] = {}

# Shard prompt caches are kept warm by the external shard-warmer Lambda
# (backend/shard-warmer/), scheduled under the 1h cache TTL — not by this runtime.


# --- User / session resolution (mirrors v1) ---------------------------------

_PROFILE_ID_RE = re.compile(r"^p-[0-9a-f]{32}$")


def _resolve_user_id(payload: dict, context=None) -> str:
    """Build the final user_id from JWT claims + profile.

    A household member's opaque profile_id (`p-<32 hex>`) is used as-is -- it is
    the profile key and the Memory actorId -- but only after checking it belongs
    to the caller's household (JWT `sub`). A profile_id that doesn't is dropped,
    so the request runs unpersonalized rather than as someone else.

    Legacy persona slugs keep the "{user_id}-{profile}" form (profile lowercased,
    spaces -> underscores), with JWT username > payload user_id.
    """
    user_id = payload.get("user_id", "anonymous")
    sub = None

    if context and getattr(context, "request_headers", None):
        auth_header = context.request_headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                import base64
                token = auth_header.split(" ", 1)[1]
                payload_part = token.split(".")[1]
                payload_part += "=" * (4 - len(payload_part) % 4)
                # The runtime's JWT authorizer has already verified this token.
                claims = json.loads(base64.urlsafe_b64decode(payload_part))
                sub = claims.get("sub")
                jwt_user = claims.get("username", claims.get("cognito:username", sub))
                if jwt_user:
                    user_id = jwt_user
                    print(f"[resolve_user_id] JWT username: {user_id}")
            except Exception as e:
                print(f"[resolve_user_id] Failed to decode JWT: {e}")

    profile = (payload.get("profile") or "").strip()
    if _PROFILE_ID_RE.match(profile):
        from user_profile import is_member
        if sub and is_member(sub, profile):
            return profile
        print("[resolve_user_id] profile_id not in caller's household; ignoring it")
        return user_id
    if profile:
        normalized_profile = profile.lower().replace(" ", "_")
        return f"{user_id}-{normalized_profile}"
    return user_id


def _get_session(session_id: str | None, user_id: str = "anonymous"):
    if not session_id:
        return None
    key = (session_id, user_id)
    if key not in _sessions:
        _sessions[key] = create_memory(session_id=session_id, actor_id=user_id)
    return _sessions[key]


# Sentinel enqueued when the pipeline finishes, so the drain loop exits
# immediately after the last event instead of waiting on a separate Event
# (which raced with the 5s keepalive and added a spurious trailing ~5s).
_STREAM_END = object()


def _run_recommendation(payload: dict, queue, loop):
    """Run the recommendation pipeline in a background thread, pushing events to the queue."""
    try:
        prompt = payload.get("prompt", payload.get("user_query", "Recommend me a good movie"))
        user_id = payload.get("user_id", "anonymous")
        session_id = payload.get("session_id")
        session = _get_session(session_id, user_id=user_id)
        for event in run_agent_stream(prompt, user_id=user_id, session=session):
            loop.call_soon_threadsafe(queue.put_nowait, event)
    except Exception as e:
        import traceback
        print(f"[v2] recommendation pipeline error: {e}\n{traceback.format_exc()}")
        loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(e)})
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, _STREAM_END)


def _run_analysis(payload: dict, queue, loop):
    """Run the scene-analysis pipeline in a background thread.

    On a 'movie_recommendation' intent the analysis pipeline emits a crossover
    event; we hand that off to the v2 recommendation pipeline (run_agent_stream)
    mid-stream, so "recommend more like this" while watching returns real recs.
    """
    try:
        # Already imported by _warm_startup() when running under AgentCore;
        # this is a cheap sys.modules lookup then (kept local for local mode).
        from analysis_agent import run_analysis_stream, AnalysisSession

        user_query = payload.get("user_query", payload.get("prompt", ""))
        user_id = payload.get("user_id", "anonymous")
        timestamp_millis = payload.get("timestamp", 0)
        video_name = payload.get("video_name", "")
        session_id = payload.get("session_id")
        title_id = payload.get("title_id", "")
        scene_image_s3_key = payload.get("scene_image_s3_key", "")

        # Authoritatively resolve the indexed scene video_name from title_id
        # instead of trusting the client. The scene index's `video_name` is the
        # basename of the title's source video, which is exactly what the
        # title_video_mapping table stores — so this is data-driven (add a
        # mapping row for a new title, no code/hardcoded map needed). Falls back
        # to the client-supplied video_name when there's no mapping.
        if title_id:
            try:
                from content_url import get_title_video_mapping_s3
                s3_url = get_title_video_mapping_s3(title_id)
                if s3_url:
                    resolved = s3_url.rsplit("/", 1)[-1]
                    if resolved:
                        if resolved != video_name:
                            print(f"[analysis] resolved video_name from title_id "
                                  f"{title_id}: {video_name!r} -> {resolved!r}")
                        video_name = resolved
            except Exception as e:
                print(f"[analysis] video_name resolve failed for {title_id}: {e}")

        if not video_name:
            loop.call_soon_threadsafe(queue.put_nowait, {
                "type": "error", "message": "video_name is required for analysis requests",
            })
            return

        session = AnalysisSession(session_id) if session_id else None
        for event in run_analysis_stream(
            user_query, user_id, timestamp_millis, video_name,
            session=session, title_id=title_id, scene_image_s3_key=scene_image_s3_key,
        ):
            if event.get("type") == "crossover" and event.get("target") == "recommendation":
                # Hand off to the v2 recommendation pipeline with its own session.
                rec_query = event["query"]
                rec_session = _get_session(session_id, user_id=user_id)
                for rec_event in run_agent_stream(rec_query, user_id=user_id, session=rec_session):
                    loop.call_soon_threadsafe(queue.put_nowait, rec_event)
            else:
                loop.call_soon_threadsafe(queue.put_nowait, event)
    except Exception as e:
        loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "message": str(e)})
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, _STREAM_END)


def _warm_startup() -> None:
    """Build static, reusable objects once at import, before app.run().

    On AgentCore Runtime V2 the runtime snapshots the process after startup and
    restores every instance from it, so work done here (imports, service-model
    parsing, endpoint resolution, client construction) is paid once per deployed
    version instead of on each instance's first request. On V1 it just moves
    that cost from the first request to container start.

    Only snapshot-safe work belongs here: no network calls, no per-request or
    expiring values (the boto3/OpenSearch clients hold refreshable credentials
    and re-resolve them on use), no random IDs, no data that can change without
    a redeploy. Each step is best-effort; a failure only means that client is
    built lazily on first use, as before.
    """
    import importlib

    steps = [
        ("analysis_agent import", lambda: importlib.import_module("analysis_agent")),
        ("opensearch client", lambda: importlib.import_module("retrieval").get_client()),
        ("bedrock embed client", lambda: importlib.import_module("retrieval")._get_bedrock()),
        ("memory client", lambda: importlib.import_module("memory")._get_memory_client()),
        ("profile table", lambda: importlib.import_module("user_profile")._get_table()),
        ("rekognition client",
         lambda: importlib.import_module("analysis_agent")._get_rekognition_client()),
    ]
    t0 = time.perf_counter()
    for name, fn in steps:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — never block startup on warm-up
            print(f"[startup] warm-up '{name}' skipped: {e}")
    print(f"[startup] warm-up done in {time.perf_counter() - t0:.2f}s")


try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()
    _warm_startup()

    @app.entrypoint
    async def handle(payload: dict, context: dict = None):
        t0 = time.time()

        request_type = payload.get("request_type", "recommendation")
        user_id = _resolve_user_id(payload, context)
        context_session_id = getattr(context, "session_id", None)
        session_id = context_session_id or payload.get("session_id")
        session_source = "context" if context_session_id else "payload"

        payload["user_id"] = user_id
        payload["session_id"] = session_id

        print(f"[handle] request_type={request_type} user_id={user_id} "
              f"session_id={session_id} (from {session_source})")

        # Cheap keepalive: touches the container (keeps this session's microVM
        # warm) without running the pipeline or spending tokens. Used by the
        # external prewarm loop against a fixed session id.
        if request_type == "ping":
            yield {"type": "response", "response": "pong",
                   "movie-recommendation": [], "tappable_options": []}
            return

        yield {"type": "status", "message": "Processing your request..."}

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        if request_type == "analysis":
            loop.run_in_executor(None, _run_analysis, payload, queue, loop)
        else:
            loop.run_in_executor(None, _run_recommendation, payload, queue, loop)

        # Drain events. A 5s silence emits a keepalive; the sentinel ends the
        # stream immediately after the final event (no spurious trailing wait).
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                yield {"type": "status", "message": "Still working..."}
                continue
            if event is _STREAM_END:
                break
            yield event

        print(f"[handle] Total time: {time.time() - t0:.1f}s")

except ImportError:
    app = None


def main():
    """Interactive local mode (no AgentCore)."""
    print("\n🎬 Movie Recommendation Agent v2 (type 'quit' to exit)")
    print("=" * 60)
    session = create_memory(session_id=f"local-{uuid.uuid4().hex[:8]}")
    while True:
        try:
            query = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not query or query.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break
        result = run_agent(query, session=session)
        print(f"\n{result['response']}")
        recs = result.get("movie-recommendation", [])
        if recs:
            print(f"\n📦 {len(recs)} recommendations")
            for i, r in enumerate(recs, 1):
                print(f"  {i}. {r.get('originalTitle', r.get('title_id', '?'))}")


if __name__ == "__main__":
    if app is not None:
        app.run()
    else:
        main()
