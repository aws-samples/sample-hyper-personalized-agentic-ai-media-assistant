# live-sports-agent

The moment-aware match assistant that runs on **Amazon Bedrock AgentCore Runtime**.
The frontend signs in with **Amazon Cognito** and invokes this runtime with the
JWT. Facts come from a **DynamoDB** event store, queried bounded by the current
playhead (`event_time <= now`), so the agent is spoiler-safe.

The demo ships one **synthetic** football match (`SYN-MAT-0001`, Harbor City FC
vs Redcliffe Athletic). Teams, players, competition and venue are fictional;
the pictures are a 60 second football clip (`data/media/football.zip` at the
repo root), and the event feed and caption track are generated to match it.

## Contents

```
live-sports-agent/
├── agentcore_app.py            # AgentCore Runtime entrypoint (Strands + Bedrock, playhead-bound tools)
├── match_state.py              # deterministic match-state engine (score/stats/highlights up to a frame)
├── event_store.py              # DynamoDB event store (stats feed, query by event_time)
├── commentary_store.py         # DynamoDB commentary store (WebVTT, query by media time) + VTT parsing
├── viewer_follows.py           # the viewer's followed teams/players -> sanitized prompt context
├── build_synthetic_match.py    # deterministic generator for data/ (the synthetic match)
├── cut_highlight_thumbs.py     # key-moment stills from the clip -> frontend/public/sports/highlights/
├── load_events_dynamodb.py     # one-time: events + META item -> the events table
├── load_commentary_dynamodb.py # one-time: WebVTT captions -> the commentary table
├── requirements.txt            # runtime deps (bedrock-agentcore, strands-agents, boto3)
├── Dockerfile / .dockerignore  # runtime image (data/ is not in the image)
└── data/                       # loader input; never deployed or read at runtime
    ├── match_events.json       # 34 normalized events -> events table
    ├── match_meta.json         # clock/half anchors, clubs, roster -> the events table's META item
    └── match_commentary.vtt    # English play-by-play captions for the clip -> commentary table
```

The runtime reads only DynamoDB: the match structure (teams, clock, optional
face ids) from the events table's `META` item, and the events and commentary
by playhead. Until the tables are loaded, requests answer that the match
data is not loaded; there is no fallback to files. The CDK stack creates the
tables (`<prefix>-events`, `<prefix>-commentary`); `deploy.py --phase data`
loads them, or run the loaders yourself from the repository root:

```bash
.venv/bin/python backend/live-sports-agent/load_events_dynamodb.py --config infra/environments/<name>.json
.venv/bin/python backend/live-sports-agent/load_commentary_dynamodb.py --config infra/environments/<name>.json
```

Both loaders are idempotent and use the default AWS credential chain. Without
`--config`, pass `--table` and `--region` (and optionally `--profile`).

## The synthetic match

`build_synthetic_match.py` writes `data/` deterministically (`--check` fails
when the files are stale). The clip is the broadcast of a late stretch of the
second half, in real time (1 video second = 1 match second, 25 fps):

| video_s | match time | what happens |
|---|---|---|
| < 0 | 1' to 77' | already played: Redcliffe lead 0-1 (Ibe-Castro, 23'), Harbor City level 1-1 (Okafor-Hale header, 61'), plus shots, saves, corners, fouls, two yellow cards and four substitutions |
| 0 | 78' (second half 32:00) | the broadcast starts at 1-1 |
| 24.00 | 78' | Tobiah Lark (#7) shoots, Marek Zielonka saves |
| 25.84 | 78' | Kofi Asante-Brook (#11) scores the rebound, 2-1 Harbor City |
| ~38-51 | 79' | net-camera replay (not a new event) |
| 60 | 79' | end of the clip |

- Events before the clip have negative `video_s` and `clip: null`. The two
  in-clip highlights carry a `clip` window (17.3 s to 29/31 s, the build-up
  and the goal), so the UI groups them into one key-moment clip.
- The feed stops at the end of the clip, like a live feed read at that
  moment: no final whistle and no final score exist anywhere, so no route can
  reveal how the match ends.
- Half anchors: the first half starts at `video_s = -5700`, the second at
  `-1920`; `MatchState.frame_for_video_s` maps any playhead onto frames.
- The clip has no audio. `match_commentary.vtt` is a synthetic caption track
  timed to the picture; the assistant reads it as broadcast commentary.

To change the match, edit `SCRIPT`, `ROSTER` or `COMMENTARY` in the generator,
re-run it, reload both tables, and re-cut the thumbnails:

```bash
python build_synthetic_match.py
unzip ../../data/media/football.zip -d /tmp/football
python cut_highlight_thumbs.py --video /tmp/football/football/horizontal/football.mp4
```

## Two spoiler-safe data sources

- **Stats** (`event_store.py`): the event feed carries UTC event times, so it is
  keyed by wall-clock and queried `event_time <= now`. This is the factual
  backbone; the LLM never counts anything itself.
- **Commentary** (`commentary_store.py`): WebVTT cues are timed on the *media
  timeline* (the same clock as the request's `video_s`), the way a live caption
  feed arrives from **AWS Elemental Live**. They are keyed by media-time
  milliseconds and queried by a window up to the playhead.

Commentary reaches the model two ways: a short rolling window
(`COMMENTARY_INJECT_S`, default 45 s) is injected into every prompt as ambient
colour, and a `get_commentary(lookback_s)` tool serves deeper lookback. Numbers
always come from the stats tools.

`web_search` answers football questions beyond this match through an AgentCore
Gateway web-search connector (the CDK stack passes `WEBSEARCH_GATEWAY_URL`).
When that variable is unset the tool returns an "unavailable" result.

## Invocation contract (what the frontend sends)

`POST https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<runtime-arn>/invocations?qualifier=DEFAULT`
with `Authorization: Bearer <Cognito JWT>` and an
`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header.

```json
{
  "question": "how many shots so far?",
  "video_s": 26.0,
  "history": [ {"role": "user", "text": "..."}, {"role": "assistant", "text": "..."} ],
  "followed": [ {"id": "player:kofi-asante-brook", "name": "Kofi Asante-Brook"} ]
}
```

Response: SSE stream of `data: {"delta": "..."}` chunks.

## Configuration (env)

| variable | default | meaning |
|---|---|---|
| `EVENTS_TABLE` | `hpm-demo-events` | events table (set by the stack) |
| `COMMENTARY_TABLE` | `hpm-demo-commentary` | commentary table (set by the stack) |
| `MATCH_ID` | `SYN-MAT-0001` | match partition key |
| `MODEL_ID` | Claude Sonnet 4.5 inference profile | Bedrock model |
| `WEBSEARCH_GATEWAY_URL` | unset | AgentCore Gateway MCP URL for `web_search` |
| `COMMENTARY_INJECT_S` / `COMMENTARY_LOOKBACK_S` | 45 / 90 | commentary windows |

The execution role needs `dynamodb:GetItem` and `dynamodb:Query` on the events
table and `dynamodb:Query` on the commentary table.

## Local run (optional)

`agentcore_app.py` is a standard `BedrockAgentCoreApp`; with credentials for an
account where the tables are loaded:

```bash
pip install -r requirements.txt
EVENTS_TABLE=<prefix>-events COMMENTARY_TABLE=<prefix>-commentary python agentcore_app.py
# then POST to http://localhost:8080/invocations
```
