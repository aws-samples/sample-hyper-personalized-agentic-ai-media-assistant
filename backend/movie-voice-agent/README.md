# Movie Voice Agent (Nova Sonic, bidirectional)

Real-time voice concierge for the movie assistant. Hosts a Strands `BidiAgent`
powered by **Amazon Nova Sonic** on a WebSocket, deployed as its own **AgentCore
Runtime** (sibling to `../movie-ai-agent-v2`).

**Nova Sonic does not recommend anything.** It's the voice layer + orchestrator.
When the user asks for something to watch, Sonic calls the
`get_movie_recommendations` tool, which forwards the request to the deployed
`movie_rec_agent_v2` recommendation agent. The voice agent then:

1. relays that agent's **structured results** to the browser as a
   `recommendations` WebSocket event (so the poster rail renders), and
2. returns a short text summary to Sonic to speak.

```
browser ──audio──►  voice agent (Nova Sonic)  ──tool call──►  movie_rec_agent_v2
        ◄─audio+text─┤                          ◄─results──────┘
        ◄─recommendations event (structured JSON, for the UI)
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | FastAPI WebSocket app (`/ws`, `/ping`); builds the Nova Sonic `BidiAgent`, defines the recommendation tool, relays structured results to the client. |
| `movie_agent.py` | The **only** place that authenticates to and invokes `movie_rec_agent_v2`. Portable; auth swap is a one-file change. |
| `requirements.txt` | Python deps. |
| `Dockerfile` | ARM64 container image (installs PortAudio) used for deployment. |

## Configuration (environment variables)

**No secrets required** — the agent calls the movie recommendation agent using
the *caller's* forwarded Cognito token (identity propagation), not a stored
password. Copy `.env.example` to `.env` (or export in your shell).

| Var | Required | Notes |
|-----|----------|-------|
| `MOVIE_AGENT_ARN` | ✅ | ARN of the deployed recommendation runtime to call. |
| `MOVIE_AGENT_REGION` | — | Defaults to the region parsed from the ARN, else `AWS_REGION`. |
| `MOVIE_AGENT_PROFILE` | — | Default personalization profile (`rom-com_lover`). |
| `MOVIE_AGENT_TIMEOUT` | — | Movie-agent invoke timeout, seconds (`60`). |
| `MODEL_ID` | — | Nova Sonic model (`amazon.nova-2-sonic-v1:0`). |
| `BEDROCK_REGION` | — | Where Nova Sonic runs (`us-east-1`). |
| `SONIC_VOICE` | — | `tiffany` or `matthew`. |

AWS credentials are still needed for the **Nova Sonic model** calls themselves
(locally via `AWS_PROFILE`; in the deployed runtime via its execution role).

## Run locally

Use the repo-root helper (starts this agent + the frontend together):
```bash
./run-voice-local.sh
```
Or run just the agent:
```bash
cd backend/movie-voice-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # needs PortAudio: `brew install portaudio`
export AWS_PROFILE=<your-profile>
set -a && source .env && set +a           # loads MOVIE_AGENT_ARN
python app.py                            # serves ws://127.0.0.1:8080/ws + /ping
```
Health check: `curl http://127.0.0.1:8080/ping` → `{"status":"Healthy",...}`.
Full voice testing needs the browser client (`NovaSonicVoiceClient`) streaming mic audio.

## Auth (identity propagation, end-to-end)

Everything uses **AgentCore Identity (JWT)** — the same model `movie_rec_agent_v2`
uses. No SigV4, no Identity Pool, no service password.

- **Browser → this runtime:** the runtime is configured with the same custom JWT
  authorizer; the browser passes its Cognito **access token**. Because the native
  `WebSocket` API can't set an `Authorization` header, AgentCore accepts the token
  in the `Sec-WebSocket-Protocol` subprotocol (base64url-encoded, prefixed with
  `base64UrlBearerAuthorization.`, plus the sentinel subprotocol
  `base64UrlBearerAuthorization`). The frontend client handles this.
- **This runtime → movie agent:** the client also sends its access token as the
  first WebSocket message (`{"type":"auth","token":...}`); the agent **forwards
  that token** when calling `movie_rec_agent_v2` (which trusts the same pool).
  So the movie agent runs as the real signed-in user — correct personalization,
  and nothing secret is stored here.

## Deploy (container + CDK)

This is a bidirectional WebSocket agent that depends on the native PortAudio
library (via `strands[bidi-all]`), so it deploys as a **container** — matching
the AWS Nova Sonic reference sample. The `Dockerfile` here (ARM64, installs
PortAudio, runs `uvicorn app:app` on 8080) is the artifact.

Deployment is via **CDK** (consistent with this project's IaC and the reference
sample): a CDK construct builds this image, pushes it to ECR, and creates an
`AWS::BedrockAgentCore::Runtime` with:
- the container image,
- `networkMode: PUBLIC`,
- the same **custom JWT authorizer** the movie agent uses (so the browser's
  Cognito access token is accepted over `wss/.../ws`),
- env vars from `.env.example` (`MOVIE_AGENT_ARN`, etc.),
- an execution role with Bedrock (Nova Sonic) model access, ECR image pull, and
  CloudWatch Logs. Note: **no** `bedrock-agentcore:Invoke*` is granted — the
  movie agent is called with the forwarded user JWT (identity propagation), not
  IAM.

The runtime construct lives in `infra/stack/stack_constructs/` alongside the
Cognito + API constructs. After `cdk deploy`, set the runtime ARN as
`VITE_AGENTCORE_VOICE_ARN` in the frontend env and (re)deploy the frontend.

Prerequisite: Docker Desktop running locally (CDK builds the ARM64 image).

## Credits / prior art

The bidirectional Nova Sonic + AgentCore approach here is adapted from AWS
sample code (MIT-0). We took the *idea and shape* — a Strands `BidiAgent` on an
AgentCore WebSocket runtime — and rebuilt it around this project's movie agent,
auth model (JWT/AgentCore Identity instead of the sample's SigV4), and env-only
config. The sample's React/Cloudscape frontend and CloudFormation stacks were
not used.

- [aws-samples/sample-nova-sonic-websocket-agentcore](https://github.com/aws-samples/sample-nova-sonic-websocket-agentcore)
  — Strands `BidiAgent`, local dev mode, WebSocket contract.
- [awslabs/amazon-bedrock-agentcore-samples — bidirectional streaming](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-features/02-host-your-agent/01-runtime/03-advanced/03-bidirectional-streaming)
  — reference Sonic/Strands/echo WebSocket implementations.
