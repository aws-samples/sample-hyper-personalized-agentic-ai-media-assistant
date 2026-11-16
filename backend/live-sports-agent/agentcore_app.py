#!/usr/bin/env python3
"""AgentCore Runtime entrypoint for the live match assistant.

Hosts the same Strands agent + playhead-bounded tools that the local service
used, but as an AgentCore Runtime (HTTP protocol, /invocations + /ping on 8080,
handled by the bedrock-agentcore SDK). Facts come from the DynamoDB event store,
queried bounded by the playhead (video_s in the request) -> spoiler-safe.

Invocation payload:
    {
      "question": "how many shots so far?",   # required
      "video_s": 1668.0,                        # required (playhead position)
      "history": [                              # optional prior turns
          {"role": "user", "text": "..."},
          {"role": "assistant", "text": "..."}
      ],
      "followed": [                             # optional: the viewer's resolved follows
          {"id": "player:kofi-asante-brook", "name": "Kofi Asante-Brook"}
      ]
    }

Credentials come from the AgentCore execution role (default boto3 session);
config via env: EVENTS_TABLE, COMMENTARY_TABLE, MATCH_ID, MODEL_ID and the
optional WEBSEARCH_GATEWAY_URL (unset -> web_search reports "unavailable").
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

from match_state import DEFAULT_MATCH_ID, load_dynamodb
from viewer_follows import follows_prompt
from commentary_store import DynamoCommentaryStore, coalesce

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "hpm-demo-events")
COMMENTARY_TABLE = os.environ.get("COMMENTARY_TABLE", "hpm-demo-commentary")
MATCH_ID = os.environ.get("MATCH_ID", DEFAULT_MATCH_ID)
# Shift VTT cue times onto the video_s timeline if the caption track and reel
# do not share an origin (0 here -- they do).
COMMENTARY_OFFSET_MS = int(os.environ.get("COMMENTARY_OFFSET_MS", "0"))
# Default lookback for the on-demand get_commentary tool.
COMMENTARY_LOOKBACK_S = int(os.environ.get("COMMENTARY_LOOKBACK_S", "90"))
# Rolling window injected into every prompt as ambient colour (0 disables it).
COMMENTARY_INJECT_S = int(os.environ.get("COMMENTARY_INJECT_S", "45"))

# The match is named from the event store's META item at startup, so the
# prompt always describes the match that is actually loaded.
SYSTEM_PROMPT_TEMPLATE = """You are a live football (soccer) match assistant for a broadcast of
{home} vs {guest}.

Scope guardrail:
- You ONLY discuss football (soccer). If the user asks about anything unrelated
  to football — other sports, news, weather, finance, personal advice, coding,
  etc. — politely decline in one short sentence and steer them back to the match
  or football. Do not answer off-topic questions even if you know the answer.

Two sources of truth — pick the right one:
- CURRENT MATCH (this {home_short} vs {guest_short} game): its score, stats, who did what,
  what's happening now, and anything about how it's going — ALWAYS use the match
  tools (get_match_time, get_score, get_team_stats, get_recent_events,
  list_highlights, get_player_stats, get_commentary). These are spoiler-safe.
  NEVER use web_search for this match's live state or final result — that would
  risk spoilers.
- BEYOND THIS MATCH (football knowledge the match tools can't answer): a player's
  career history/honours, club history, head-to-head records, league standings,
  transfers, other competitions, rules — use the web_search tool. Summarize its
  results concisely in your own words; preserve source URLs as citations, and prefer recent,
  reputable information.

Rules:
- You only know what has happened up to the current moment in the live stream.
- Never speculate about or reveal events that have not happened yet.
- Always use the provided tools for any factual claim (score, stats, who did what).
  Never invent or estimate numbers.
- Be concise and conversational, like a knowledgeable co-commentator.
- If a tool shows a player wasn't found, say so rather than guessing.
- The most recent broadcast commentary up to now is provided inline below to
  give you a feel for the game. Absorb it as your own awareness of the mood and
  flow -- it is background context, NOT something to cite. Do not mention "the
  commentary", "the commentators", the broadcast, or say things like "as you can
  hear" or "they're saying". Never reveal that any part of your answer comes from
  commentary. Just speak as a co-commentator with your own read of the match.
- Never treat commentary as a source of statistics -- always use the stats tools
  (score, team/player stats) for any numbers, since commentary is colloquial and
  may be imprecise or figurative.
- The ONE exception: if the user explicitly asks what the commentators are
  saying, or to verify/quote the broadcast, then you may attribute and quote it.
  For commentary older than the inline snippet (e.g. "what did they say when the
  first goal went in?"), call get_commentary with a larger lookback_s. It never
  returns commentary aired after the current moment.
"""

# Default boto3 session -> credentials from the AgentCore execution role.
# Match structure, events and commentary all come from DynamoDB. They are read
# on the first request, not at import: the tables are loaded after the stack
# is deployed, and the runtime must start (and pass its health check) before
# that. Until they are loaded, requests return an error naming the loaders.
_session = boto3.Session()
STATE = None
COMMENTARY = None
SYSTEM_PROMPT = ""


def _ensure_loaded() -> None:
    """Load the match structure and stores once; raise if a table is empty."""
    global STATE, COMMENTARY, SYSTEM_PROMPT
    if STATE is not None:
        return
    state = load_dynamodb(EVENTS_TABLE, MATCH_ID, _session)
    n = state.store.count()
    if n <= 0:
        raise RuntimeError(f"table '{EVENTS_TABLE}' has no events for {MATCH_ID}")
    commentary = DynamoCommentaryStore(COMMENTARY_TABLE, MATCH_ID, _session)
    cn = commentary.count()
    if cn <= 0:
        raise RuntimeError(f"table '{COMMENTARY_TABLE}' has no commentary for {MATCH_ID}")
    print(f"[agentcore] event store: dynamodb:{EVENTS_TABLE}({n} events); "
          f"commentary store: dynamodb:{COMMENTARY_TABLE}({cn} cues)")
    SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(
        home=state.teams["home"]["name"], guest=state.teams["guest"]["name"],
        home_short=state.teams["home"]["short_name"], guest_short=state.teams["guest"]["short_name"],
    )
    COMMENTARY = commentary
    STATE = state

MODEL = BedrockModel(model_id=MODEL_ID, temperature=0.2, max_tokens=1024)

# --- Web search via the AgentCore Gateway (MCP over HTTP, SigV4/IAM) ----------
# Beyond-the-match football knowledge (player history, club records, standings,
# other competitions) comes from a built-in Web Search connector fronted by an
# AgentCore Gateway. Auth is AWS_IAM: we SigV4-sign each MCP call with the
# runtime's execution-role credentials (default boto3 session).
# Optional: the deployer's own gateway URL (".../mcp"). Unset -> web_search
# returns an "unavailable" result and the agent answers from the match tools.
WEBSEARCH_URL = os.environ.get("WEBSEARCH_GATEWAY_URL", "").strip()
WEBSEARCH_TOOL_NAME = os.environ.get("WEBSEARCH_TOOL_NAME", "web-search-tool___WebSearch")
WEBSEARCH_REGION = os.environ.get("WEBSEARCH_REGION", os.environ.get("AWS_REGION", "us-east-1"))
_SIGV4_SERVICE = "bedrock-agentcore"
_WEBSEARCH_SESSION = boto3.Session()


def _mcp_post(body: dict, session_id: str | None = None) -> tuple[dict | None, str | None]:
    """SigV4-signed MCP POST to the gateway. Returns (json_result, session_id)."""
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    creds = _WEBSEARCH_SESSION.get_credentials().get_frozen_credentials()
    signed = AWSRequest(method="POST", url=WEBSEARCH_URL, data=data, headers=headers)
    SigV4Auth(creds, _SIGV4_SERVICE, WEBSEARCH_REGION).add_auth(signed)
    req = urllib.request.Request(WEBSEARCH_URL, data=data, method="POST")
    for k, v in signed.headers.items():
        req.add_header(k, v)
    resp = urllib.request.urlopen(req, timeout=float(os.environ.get("WEBSEARCH_TIMEOUT_SECONDS", "25")))
    sid = resp.headers.get("Mcp-Session-Id") or session_id
    raw = resp.read().decode()
    if "data:" in raw:  # streamable-HTTP SSE framing
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), sid
        return None, sid
    try:
        return json.loads(raw), sid
    except json.JSONDecodeError:
        return None, sid


def _web_search(query: str, max_results: int = 5) -> dict:
    """Run one web search through the gateway; return {results:[{date,url,snippet}]}."""
    if not WEBSEARCH_URL:
        raise RuntimeError("no web search gateway configured (WEBSEARCH_GATEWAY_URL)")
    _r, sid = _mcp_post({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "hpm-demo-match-agent", "version": "1.0"}},
    })
    _mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    r, _ = _mcp_post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": WEBSEARCH_TOOL_NAME,
                   "arguments": {"query": query, "maxResults": max(1, min(int(max_results), 10))}},
    }, sid)
    result = (r or {}).get("result", {})
    out: list[dict] = []
    for block in result.get("content", []) or []:
        if block.get("type") != "text":
            continue
        try:
            payload = json.loads(block.get("text", "{}"))
        except json.JSONDecodeError:
            continue
        for item in payload.get("results", []) or []:
            snippet = (item.get("text") or "").strip()
            if len(snippet) > 500:
                snippet = snippet[:497] + "…"
            out.append({"date": item.get("publishedDate"),
                        "url": item.get("url") or item.get("link"),
                        "snippet": snippet})
    return {"query": query, "results": out[:max_results]}


app = BedrockAgentCoreApp()


def build_tools(frame: int, video_s: float) -> list:
    """Playhead-bound tools for a single request. `frame` bounds the stats
    engine; `video_s` (media-timeline seconds) bounds the commentary window."""

    @tool
    def get_match_time() -> str:
        """The current live match time: period and minute (e.g. "18'", first half).
        Use this whenever the question is about "now"/"so far"/"current"."""
        return json.dumps(STATE.clock(frame))

    @tool
    def get_score() -> str:
        """Get the current score and match period."""
        s = STATE.at(frame)
        return json.dumps({"score": s["score"], "period": s["period"]})

    @tool
    def get_team_stats() -> str:
        """Team stats so far: shots, shots on target, saves, corners, fouls, offsides."""
        return json.dumps(STATE.at(frame)["team_stats"], ensure_ascii=False)

    @tool
    def get_recent_events(count: int = 5) -> str:
        """Most recent events up to now. count: how many to return."""
        return json.dumps(STATE.at(frame, recent_n=count)["recent_events"], ensure_ascii=False)

    @tool
    def list_highlights() -> str:
        """Notable moments so far (goals, shots, saves, penalties), in order."""
        hl = [{"minute": h["minute"], "type": h["type"], "player": h["player"],
               "team": h["team"], "xg": h["xg"]} for h in STATE.at(frame)["highlights"]]
        return json.dumps(hl, ensure_ascii=False)

    @tool
    def get_player_stats(name: str) -> str:
        """A single player's tallies so far (goals, shots, assists, fouls, cards). name: player name or partial."""
        return json.dumps(STATE.player_stats(frame, name), ensure_ascii=False)

    @tool
    def get_commentary(lookback_s: int = COMMENTARY_LOOKBACK_S) -> str:
        """The broadcast commentators' words from the last `lookback_s` seconds,
        up to the current moment. Use for the atmosphere, mood, or what's being
        said right now -- not for statistics. Spoiler-safe: never returns
        commentary aired after the current playhead."""
        playhead_ms = int(round(video_s * 1000)) + COMMENTARY_OFFSET_MS
        cues = COMMENTARY.window(playhead_ms, int(lookback_s) * 1000)
        text = coalesce(cues)
        if not text:
            return json.dumps({"lookback_s": lookback_s, "commentary": None,
                               "note": "no commentary in this window yet"})
        return json.dumps({"lookback_s": lookback_s, "commentary": text},
                          ensure_ascii=False)

    @tool
    def web_search(query: str, max_results: int = 5) -> str:
        """Search the web for FOOTBALL (soccer) knowledge BEYOND this live match —
        e.g. a player's career history or honours, club history, head-to-head
        records, league standings, transfers, other competitions, or rules.

        Use this ONLY for background/general football facts that the match tools
        can't answer. Do NOT use it for the current match's live state,
        score, stats, or final result — those come from the
        spoiler-safe match tools. Only for football-related queries.
        """
        try:
            return json.dumps(_web_search(query, max_results), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"query": query, "results": [],
                               "error": f"web search unavailable: {exc}"})

    return [get_match_time, get_score, get_team_stats, get_recent_events,
            list_highlights, get_player_stats, get_commentary, web_search]


def _to_messages(history: list | None) -> list:
    """Convert [{role, text}] turns to Strands message format."""
    msgs = []
    for turn in history or []:
        role = turn.get("role")
        text = turn.get("text", "")
        if role in ("user", "assistant") and text:
            msgs.append({"role": role, "content": [{"text": text}]})
    return msgs


@app.entrypoint
async def invoke(payload: dict):
    question = (payload or {}).get("question")
    video_s = (payload or {}).get("video_s")
    if not question or video_s is None:
        yield {"error": "payload must include 'question' and 'video_s'"}
        return
    try:
        _ensure_loaded()
    except Exception as exc:  # noqa: BLE001 -- surface a load failure to the caller
        print(f"[agentcore] match data unavailable: {exc}")
        yield {"error": "match data is not loaded; run load_events_dynamodb.py and "
                        "load_commentary_dynamodb.py"}
        return

    frame = STATE.frame_for_video_s(float(video_s))
    clock = STATE.clock(frame)
    live_prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"CURRENT LIVE MOMENT: {clock['label']} ({clock['period']}). "
        "This is the exact moment on screen right now. The match clock advances "
        "between questions in this conversation, so figures you gave earlier may "
        "now be out of date -- always call the tools again to answer about the "
        "current moment rather than reusing earlier numbers."
    )

    if COMMENTARY_INJECT_S > 0:
        playhead_ms = int(round(float(video_s) * 1000)) + COMMENTARY_OFFSET_MS
        recent = coalesce(COMMENTARY.window(playhead_ms, COMMENTARY_INJECT_S * 1000))
        if recent:
            live_prompt += (
                f"\n\nBACKGROUND FEEL OF THE GAME (last ~{COMMENTARY_INJECT_S}s "
                "of broadcast audio, for your awareness only -- do NOT quote it, "
                "cite it, or mention the commentary/commentators unless the user "
                f"explicitly asks what is being said):\n\"{recent}\""
            )
    live_prompt += follows_prompt(payload.get("followed"))

    agent = Agent(model=MODEL, system_prompt=live_prompt,
                  tools=build_tools(frame, float(video_s)),
                  messages=_to_messages(payload.get("history")))

    async for event in agent.stream_async(question):
        text = event.get("data")
        if text:
            yield {"delta": text}


if __name__ == "__main__":
    app.run()
