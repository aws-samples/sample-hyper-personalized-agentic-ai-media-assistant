"""Bridge to the deployed recommendation agent (`movie_rec_agent_v2`).

The only place that knows how to call the movie recommendation agent. Nova Sonic
never generates recommendations itself — it calls this bridge as a tool, gets
back the movie agent's own structured results, and voices a short summary.

Auth is **identity propagation**: the caller's Cognito access token (the same one
the browser already uses, validated by this voice runtime's JWT authorizer) is
forwarded to the movie agent, which trusts the same pool. No stored password, no
service credentials — and the movie agent derives the correct user from the JWT,
so personalization is per real user.

All configuration comes from environment variables. No account IDs, ARNs, or
secrets are hardcoded.
"""

from __future__ import annotations

import functools
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field


@dataclass
class MovieAgentResult:
    """What the movie agent returned for one query."""

    spoken_text: str
    # Raw recommendation dicts, exactly as the movie agent emits them
    # (title_id, originalTitle, image, genres, imdb_rating, ...). Forwarded
    # verbatim so the frontend can reuse its existing mapping.
    recommendations: list[dict] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    # "answer" when the movie agent replied to a question about titles already
    # shown (no new list); "recommend" otherwise.
    intent: str = "recommend"
    # Presigned playback URLs keyed by title_id (the movie agent streams these
    # separately from the rec objects). Forwarded so the frontend can play video.
    content_urls: dict = field(default_factory=dict)


def _require(name: str) -> str:
    """Read a required env var or raise a clear, actionable error."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "See backend/movie-voice-agent/.env.example."
        )
    return value


@functools.lru_cache(maxsize=1)
def _config() -> dict:
    """Resolve configuration from the environment once, on first use."""
    arn = _require("MOVIE_AGENT_ARN")
    region = os.getenv("MOVIE_AGENT_REGION") or os.getenv("AWS_REGION")
    if not region:
        parts = arn.split(":")
        region = parts[3] if len(parts) > 3 and parts[3] else "us-east-1"
    return {
        "arn": arn,
        "region": region,
        "default_profile": os.getenv("MOVIE_AGENT_PROFILE", "rom-com_lover"),
        "timeout": int(os.getenv("MOVIE_AGENT_TIMEOUT", "60")),
    }


def default_profile() -> str:
    """Personalization profile to use when the caller doesn't specify one."""
    return _config()["default_profile"]


def new_session_id() -> str:
    """Fresh movie-agent session id (38 chars; AgentCore requires >= 33).

    Must be called per connection/request, never at import: on AgentCore Runtime
    V2 every instance is restored from one snapshot, so a module-level id (or one
    built from the PID/start time, which are identical after restore) would be
    shared by every instance and merge unrelated users' sessions.
    """
    return "voice-" + uuid.uuid4().hex


def _invoke_agent(
    request_type: str,
    query: str,
    profile: str | None,
    access_token: str,
    extra: dict | None = None,
    session_id: str | None = None,
    on_status=None,
) -> MovieAgentResult:
    """Blocking call: POST to the unified agent runtime with the caller's access
    token, drain the SSE stream, and return its spoken reply + any structured
    recommendations.

    ``request_type`` selects the pipeline ("recommendation" or "analysis").
    ``extra`` carries per-type fields (e.g. the analysis scene context). The
    stream shape is identical for both — a ``response`` event with text and,
    when present, ``movie-recommendation`` / ``tappable_options``.

    Callers on an event loop should wrap this in ``asyncio.to_thread``.
    """
    if not access_token:
        return MovieAgentResult(
            spoken_text="I can't reach the agent without a sign-in. Please sign in and try again."
        )

    cfg = _config()
    arn = urllib.parse.quote(cfg["arn"], safe="")
    url = (
        f"https://bedrock-agentcore.{cfg['region']}.amazonaws.com/runtimes/{arn}"
        f"/invocations?qualifier=DEFAULT"
    )
    # Prefer the caller's (browser) session id so the frontend and the movie
    # agent share ONE session — same AgentCore session affinity (warm microVM)
    # and same memory/continuity. Direct callers without one get a fresh id
    # (app.py assigns one per WebSocket connection, so a connection stays on
    # one session across turns).
    sid = session_id or new_session_id()
    body = json.dumps(
        {
            "session_id": sid,
            "profile": profile or cfg["default_profile"],
            "user_query": query,
            "request_type": request_type,
            **(extra or {}),
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sid,
        },
    )

    final_text = ""
    recs: list[dict] = []
    followups: list[str] = []
    content_urls: dict = {}
    intent = "recommend"
    t0 = time.time()
    print(f"[agent] calling {request_type} pipeline with: {query!r} (session={sid})", flush=True)
    # Iterate the response line-by-line as it arrives (not resp.read(), which
    # would block until the whole stream finishes) so status events can be
    # forwarded live while the pipeline is still working.
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "status":
                # Forward the movie agent's real progress line (e.g. "Searching
                # for movies...", "Found 12 movies") to the caller so the UI can
                # show it. Shown on screen only — never spoken by Nova Sonic.
                if on_status:
                    msg = event.get("message")
                    if msg:
                        try:
                            # The phase lets the UI tell a real search
                            # ("executor") from an answer-only turn.
                            on_status(str(msg), str(event.get("phase") or ""))
                        except Exception as exc:
                            print(f"[agent] on_status callback failed: {exc}", flush=True)
                continue
            if etype == "response":
                final_text = event.get("response", "") or final_text
                recs = event.get("movie-recommendation", []) or recs
                if event.get("intent") == "answer":
                    intent = "answer"
                opts = event.get("tappable_options")
                if isinstance(opts, list):
                    followups = opts
                urls = event.get("content_urls")
                if isinstance(urls, dict) and urls:
                    content_urls = urls
    # Inter-agent HTTP round-trip: voice runtime -> recommendation runtime.
    # Subtract the rec runtime's own "[handle] Total time" (same session) to get
    # the network + AgentCore routing/cold-start overhead between the two agents.
    print(
        f"[timing] interagent_http request_type={request_type} session={sid} "
        f"elapsed={time.time() - t0:.2f}s recs={len(recs)}",
        flush=True,
    )

    if not final_text:
        final_text = "I couldn't find anything for that just now."
    return MovieAgentResult(spoken_text=final_text, recommendations=recs,
                            followups=followups, content_urls=content_urls, intent=intent)


def invoke_movie_agent(query: str, profile: str | None, access_token: str,
                       session_id: str | None = None, on_status=None) -> MovieAgentResult:
    """Recommendation turn (search path)."""
    return _invoke_agent("recommendation", query, profile, access_token,
                         session_id=session_id, on_status=on_status)


def invoke_analysis_agent(
    question: str,
    profile: str | None,
    access_token: str,
    timestamp_ms: int = 0,
    video_name: str | None = None,
    title_id: str | None = None,
    title: str | None = None,
    session_id: str | None = None,
    on_status=None,
) -> MovieAgentResult:
    """Scene-analysis turn (player path).

    Sends ``request_type='analysis'`` with the current playback context. The
    runtime routes scene vs. recommendation intent internally (see
    ``main.py`` ``_run_analysis``), so a "recommend more like this" question
    streams back the recommendation pipeline's own results — same shape.

    Title anchoring (this agent only, not the recs agent): we frame the query
    with the current title so a "recommend more like this" turn anchors to
    what's playing instead of falling back to the profile, and we also pass the
    title as a ``title`` field so the recs agent can consume it cleanly later
    (and this framing shim can then be removed).
    """
    framed = question
    if title:
        framed = f'The user is currently watching "{title}". {question}'
    return _invoke_agent(
        "analysis",
        framed,
        profile,
        access_token,
        {
            "timestamp": max(0, int(timestamp_ms or 0)),
            "video_name": video_name or "",
            "title_id": title_id or "",
            "title": title or "",
        },
        session_id=session_id,
        on_status=on_status,
    )
