"""Nova Sonic bidirectional voice agent for the movie assistant.

Hosts a Strands ``BidiAgent`` (Amazon Nova Sonic) on a WebSocket at ``/ws`` —
the AgentCore Runtime bidirectional-streaming contract (WebSocket on port 8080,
health check on ``/ping``). The browser streams mic audio in and receives audio
+ transcript back.

Recommendations are NOT produced here. When the user wants something to watch,
Sonic calls ``get_movie_recommendations``, which forwards the request to the
deployed ``movie_rec_agent_v2`` (see ``movie_agent.py``). We then:
  1. relay the movie agent's *structured* results to the browser as a custom
     ``recommendations`` event (so the poster rail renders), and
  2. return a short text summary to Sonic so it can voice it.

Auth (browser -> this runtime) reuses the demo's existing AgentCore Identity /
custom JWT authorizer — the browser passes the same Cognito access token it
already uses for the text search path. No SigV4 / Identity Pool needed. See the
project's Search Accuracy analysis doc for the confirmation against AWS docs.
"""

import asyncio
import os
import time
from datetime import datetime
from typing import Awaitable, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from strands.experimental.bidi import BidiAgent
from strands.experimental.bidi.models import BidiNovaSonicModel
from strands.experimental.bidi.tools import stop_conversation
from strands import tool

from movie_agent import invoke_movie_agent, invoke_analysis_agent, new_session_id

# --- Environment configuration ---
MODEL_ID = os.getenv("MODEL_ID", "amazon.nova-2-sonic-v1:0")
BEDROCK_REGION = os.getenv("BEDROCK_REGION", os.getenv("AWS_REGION", "us-east-1"))
VOICE = os.getenv("SONIC_VOICE", "tiffany")  # "tiffany" or "matthew"
INPUT_SAMPLE_RATE = int(os.getenv("INPUT_SAMPLE_RATE", "16000"))
OUTPUT_SAMPLE_RATE = int(os.getenv("OUTPUT_SAMPLE_RATE", "16000"))
CHANNELS = int(os.getenv("CHANNELS", "1"))
FORMAT = os.getenv("FORMAT", "pcm")

app = FastAPI()

SendFn = Callable[[dict], Awaitable[None]]


def make_movie_tool(send: SendFn, profile: str | None, access_token: str | None,
                    session_id: str | None = None):
    """Build a `get_movie_recommendations` tool bound to one connection.

    The tool is created fresh per WebSocket connection with that connection's
    `send` channel, `profile`, and the caller's `access_token` captured directly
    (explicit dependency injection). This is why the structured `recommendations`
    relay reliably reaches the right browser, and why the movie agent is called
    on behalf of the real signed-in user (identity propagation) rather than a
    service account.

    In-flight de-duplication: Nova Sonic can emit the same tool call more than
    once for a single utterance. We key concurrent identical calls to a single
    underlying agent invocation so we only do the ~20s work (and relay) once.
    """
    inflight: dict[str, asyncio.Task] = {}

    async def _run(query: str) -> str:
        # Tell the UI a search has started so it can show the thinking/skeleton
        # state while the (~20s) movie agent call runs.
        try:
            await send({"type": "tool_start", "query": query})
        except Exception as exc:
            print(f"[voice] failed to send tool_start: {exc}", flush=True)

        # Forward the movie agent's real progress lines to the browser as they
        # arrive. invoke_movie_agent runs on a worker thread (below), so hop
        # back onto this event loop to send. Display-only — never spoken.
        loop = asyncio.get_running_loop()

        def on_status(message: str, phase: str = "") -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    send({"type": "status", "message": message, "phase": phase, "query": query}), loop
                )
            except Exception as exc:
                print(f"[voice] failed to forward status: {exc}", flush=True)

        # Run the blocking HTTP/SSE call off the event loop so Nova Sonic can
        # keep the conversation responsive (async tool calling → speak filler).
        _t0 = time.perf_counter()
        result = await asyncio.to_thread(
            invoke_movie_agent, query, profile, access_token, session_id, on_status
        )
        print(f"[timing] tool=get_movie_recommendations session={session_id} "
              f"elapsed={time.perf_counter() - _t0:.2f}s recs={len(result.recommendations)}", flush=True)

        # Relay the movie agent's OWN structured results to the browser so the
        # poster rail renders. Shape mirrors the text path's SSE "response"
        # event, so the frontend reuses its existing mapping.
        if result.recommendations:
            try:
                await send(
                    {
                        "type": "recommendations",
                        "query": query,
                        "response": result.spoken_text,
                        "movie-recommendation": result.recommendations,
                        "tappable_options": result.followups,
                        "content_urls": result.content_urls,
                    }
                )
            except Exception as exc:
                print(f"[voice] failed to relay recommendations: {exc}", flush=True)
        else:
            # Answer-only turn (a question about titles already shown) or an empty
            # result: tell the UI this tool call is finished so it drops the
            # result skeleton and shows the reply instead of waiting for cards.
            try:
                await send({"type": "answer", "query": query, "intent": result.intent,
                            "response": result.spoken_text, "tappable_options": result.followups})
            except Exception as exc:
                print(f"[voice] failed to relay answer: {exc}", flush=True)
        return result.spoken_text

    @tool
    async def get_movie_recommendations(query: str) -> str:
        """Search the streaming catalog for something to watch, or answer a
        question about titles already shown. Call this for ANY request about
        finding, recommending, or searching for movies/shows (by mood, genre,
        actor, similar titles, etc.) and for questions about titles (plot, cast,
        director, rating, which one to pick). The catalog's own system does the
        thinking; you just relay the request and voice the result.

        Args:
            query: The user's request, in their own words.

        Returns:
            A short spoken reply. For searches the titles are shown on screen
            separately; for questions it is the answer itself.
        """
        key = query.strip().lower()
        task = inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(_run(query))
            inflight[key] = task
            task.add_done_callback(lambda _t: inflight.pop(key, None))
        return await task

    return get_movie_recommendations


def make_scene_tool(send: SendFn, profile: str | None, access_token: str | None, scene_ctx: dict,
                    session_id: str | None = None):
    """Build an `ask_about_scene` tool bound to one connection (analysis mode).

    Mirrors `make_movie_tool`, but forwards to the analysis pipeline with the
    connection's latest playback context (`scene_ctx`, updated live by the
    browser's `scene_context` messages). The unified runtime routes scene vs.
    recommendation intent itself, so this one tool answers scene questions AND
    "recommend more like this" — relaying any structured recommendations to the
    browser so the poster rail renders, exactly like the search path.
    """
    inflight: dict[str, asyncio.Task] = {}

    async def _run(question: str) -> str:
        try:
            await send({"type": "tool_start", "query": question})
        except Exception as exc:
            print(f"[voice] failed to send tool_start: {exc}", flush=True)

        # Forward the analysis pipeline's real progress lines to the browser as
        # they arrive (display-only — never spoken).
        loop = asyncio.get_running_loop()

        def on_status(message: str, phase: str = "") -> None:
            try:
                asyncio.run_coroutine_threadsafe(
                    send({"type": "status", "message": message}), loop
                )
            except Exception as exc:
                print(f"[voice] failed to forward status: {exc}", flush=True)

        _t0 = time.perf_counter()
        result = await asyncio.to_thread(
            invoke_analysis_agent,
            question,
            profile,
            access_token,
            scene_ctx.get("timestamp", 0),
            scene_ctx.get("video_name"),
            scene_ctx.get("title_id"),
            scene_ctx.get("title"),
            session_id,
            on_status,
        )
        print(f"[timing] tool=ask_about_scene session={session_id} "
              f"elapsed={time.perf_counter() - _t0:.2f}s recs={len(result.recommendations)}", flush=True)

        # If the analysis turn crossed over to recommendations, relay them so
        # the player shows poster cards (same event the search path uses).
        if result.recommendations:
            try:
                await send(
                    {
                        "type": "recommendations",
                        "query": question,
                        "response": result.spoken_text,
                        "movie-recommendation": result.recommendations,
                        "tappable_options": result.followups,
                        "content_urls": result.content_urls,
                    }
                )
            except Exception as exc:
                print(f"[voice] failed to relay recommendations: {exc}", flush=True)
        return result.spoken_text

    @tool
    async def ask_about_scene(question: str) -> str:
        """Answer a question about the movie or show the user is CURRENTLY
        watching — the current scene, the characters, the plot — or find similar
        or other titles they might want. Call this for essentially every user
        request while watching; the catalog's own agent does the thinking and
        returns a ready-to-speak reply, which you voice.

        Args:
            question: The user's request, in their own words.

        Returns:
            A short spoken reply. (Any recommended titles are shown on screen.)
        """
        key = question.strip().lower()
        task = inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(_run(question))
            inflight[key] = task
            task.add_done_callback(lambda _t: inflight.pop(key, None))
        return await task

    return ask_about_scene


ANALYSIS_SYSTEM_PROMPT = (
    "You are streamOS, the friendly voice concierge helping someone with the "
    "movie or show they are CURRENTLY watching. You do NOT answer from your own "
    "knowledge. For essentially every request — what's happening in the scene, "
    "who the characters/actors are, the plot, or finding similar or other titles "
    "— call ask_about_scene with the user's request in their own words. The tool "
    "returns a ready-to-speak reply written by the catalog; voice it faithfully, "
    "lightly smoothing for natural speech, but do NOT change its meaning, add "
    "opinions, or introduce titles it didn't mention. Recommended titles appear "
    "on screen, so never read a long list aloud. Only handle pure pleasantries "
    "(hello, thanks, goodbye) yourself, briefly, without the tool."
)


SYSTEM_PROMPT = (
    "You are streamOS, the friendly voice concierge for a movie and TV "
    "streaming service. You do NOT answer movie or show questions from your own "
    "knowledge — the streaming service's recommendation system does that. For "
    "essentially every user request — finding something to watch, "
    "recommendations, searches by mood/genre/actor/similar titles, or questions "
    "about specific titles — call get_movie_recommendations with the user's "
    "request in their own words. The tool returns a ready-to-speak reply written "
    "by the catalog — voice that reply faithfully, lightly trimming or smoothing "
    "it for natural speech, but do NOT change its meaning, add opinions, or "
    "introduce titles it didn't mention. The titles appear on screen, so never "
    "read a long list aloud. When the user asked a question about a title (plot, "
    "cast, rating, which one to pick), the tool's reply IS the answer: voice it in "
    "full. Then offer to refine. Only handle pure pleasantries "
    "(hello, thanks, goodbye) yourself, briefly, without the tool."
)


def _build_model() -> BidiNovaSonicModel:
    return BidiNovaSonicModel(
        model_id=MODEL_ID,
        provider_config={
            "audio": {
                "voice": VOICE,
                "input_rate": INPUT_SAMPLE_RATE,
                "output_rate": OUTPUT_SAMPLE_RATE,
                "channels": CHANNELS,
                "format": FORMAT,
            },
            "inference": {},
        },
        client_config={"region": BEDROCK_REGION},
    )


@app.get("/ping")
async def ping():
    """Health check endpoint required by AgentCore Runtime."""
    return {"status": "Healthy", "time_of_last_update": int(datetime.now().timestamp())}


@app.websocket("/ws")
async def voice_chat(websocket: WebSocket) -> None:
    """Bidirectional voice streaming endpoint (AgentCore WebSocket contract)."""
    voice_agent: BidiAgent | None = None
    try:
        await websocket.accept()
        print("WebSocket connection accepted", flush=True)

        # Per-user personalization: profile may arrive as a query param (local)
        # or custom header (deployed).
        profile = websocket.query_params.get("profile") or websocket.headers.get(
            "x-amzn-bedrock-agentcore-runtime-custom-profile"
        )

        # Which screen opened this connection: "search" (default) or "analysis"
        # (the VOD player). Selects the tool + prompt below. May arrive as a
        # query param (local) or in the auth message (deployed).
        mode = websocket.query_params.get("mode") or "search"

        # Latest playback context for analysis mode, updated live by the
        # browser's `scene_context` messages (see read_input).
        scene_ctx: dict = {"timestamp": 0, "video_name": None, "title_id": None, "title": None}

        # The client sends its Cognito access token as the first message so we
        # can call the movie agent on behalf of the signed-in user (identity
        # propagation). Any non-auth first message is buffered and replayed so
        # no input is lost.
        access_token: str | None = None
        # Per-connection fallback, generated after snapshot restore (Runtime V2):
        # replaced by the browser's session id when the auth message carries one.
        session_id: str = new_session_id()
        buffered: list[dict] = []
        try:
            first = await websocket.receive_json()
            if isinstance(first, dict) and first.get("type") == "auth":
                access_token = first.get("token")
                profile = first.get("profile") or profile
                mode = first.get("mode") or mode
                # Browser's session id — forwarded to the movie agent so the
                # frontend and backend share ONE session (affinity + memory).
                session_id = first.get("session_id") or session_id
            else:
                buffered.append(first)
        except Exception as exc:
            print(f"[voice] no initial message: {exc}", flush=True)

        # Build the agent + tool fresh for THIS connection (no ambient context).
        # analysis mode = the player (scene questions + "more like this");
        # search mode = the existing search screen, unchanged.
        if mode == "analysis":
            connection_tool = make_scene_tool(
                websocket.send_json, profile, access_token, scene_ctx, session_id
            )
            system_prompt = ANALYSIS_SYSTEM_PROMPT
        else:
            connection_tool = make_movie_tool(websocket.send_json, profile, access_token, session_id)
            system_prompt = SYSTEM_PROMPT
        print(f"[voice] connection mode={mode} session_id={session_id}", flush=True)
        voice_agent = BidiAgent(
            model=_build_model(),
            tools=[connection_tool, stop_conversation],
            system_prompt=system_prompt,
        )

        async def read_input() -> dict:
            # Replay any buffered (non-auth) first message, then stream the rest.
            # `scene_context` messages update the analysis context and are NOT
            # passed to the agent as turns — loop until a real input arrives.
            while True:
                if buffered:
                    msg = buffered.pop(0)
                else:
                    msg = await websocket.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "scene_context":
                    scene_ctx["timestamp"] = int(msg.get("timestamp") or 0)
                    if msg.get("video_name"):
                        scene_ctx["video_name"] = msg.get("video_name")
                    if msg.get("title_id"):
                        scene_ctx["title_id"] = msg.get("title_id")
                    if msg.get("title"):
                        scene_ctx["title"] = msg.get("title")
                    continue
                return msg

        # Strands wires the WebSocket directly to the agent's I/O.
        await voice_agent.run(
            inputs=[read_input],
            outputs=[websocket.send_json],
        )
    except WebSocketDisconnect:
        print("Client disconnected", flush=True)
    except Exception as exc:
        print(f"Error in voice chat: {exc}", flush=True)
        import traceback

        traceback.print_exc()
    finally:
        if voice_agent is not None:
            try:
                await voice_agent.stop()
            except Exception as cleanup_error:
                print(f"Error during cleanup: {cleanup_error}", flush=True)
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn

    # Port is configurable for local dev to avoid conflicts on 8080. In the
    # container, AgentCore always drives port 8080 (CONTAINER_ENV is set), so
    # this default is unchanged there.
    port = int(os.getenv("PORT", "8080"))
    print(f"Starting movie voice agent on port {port}...", flush=True)
    print(f"Model: {MODEL_ID}  Region: {BEDROCK_REGION}  Voice: {VOICE}", flush=True)
    host = "0.0.0.0" if os.getenv("CONTAINER_ENV") else "127.0.0.1"
    uvicorn.run(app, host=host, port=port)
