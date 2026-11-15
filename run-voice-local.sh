#!/bin/bash
# Run the full voice experience on localhost: the Nova Sonic voice agent +
# the frontend, wired together. One command, two services, Ctrl+C stops both.
#
# Everything else in the app (home, text search, recommendations) works with
# plain `npm run dev` — this script is only needed for the real-time VOICE path,
# because the voice agent isn't deployed yet and must run locally.
#
# Usage:
#   AWS_PROFILE=<your-profile> ./run-voice-local.sh
# (or have AWS credentials set in the environment some other way)

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$ROOT/backend/movie-voice-agent"
FRONTEND_DIR="$ROOT/frontend"
: "${AWS_PROFILE:?Set AWS_PROFILE to your AWS profile (or provide AWS creds in the env) before running}"

echo "== Voice local dev =="
echo "AWS_PROFILE=$AWS_PROFILE"

# --- 1. Voice agent: venv + deps (first run only) ---
cd "$AGENT_DIR"
if [ ! -d .venv ]; then
  echo "[agent] creating venv + installing deps (first run; needs PortAudio: brew install portaudio)"
  python3 -m venv .venv
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  ./.venv/bin/python -m pip install --quiet -r requirements.txt
fi

# --- 2. Load agent env (.env) ---
if [ ! -f .env ]; then
  echo "[agent] ERROR: $AGENT_DIR/.env not found. Copy .env.example to .env and fill it in."
  exit 1
fi
set -a; . ./.env; set +a

# Port for the local voice agent. 8080 is often taken by other local services,
# so default to 8081 (override with VOICE_PORT=... ./run-voice-local.sh).
VOICE_PORT="${VOICE_PORT:-8081}"

# --- 3. Start the voice agent (background) ---
echo "[agent] starting on http://127.0.0.1:${VOICE_PORT} ..."
PORT="$VOICE_PORT" ./.venv/bin/python app.py &
AGENT_PID=$!

cleanup() {
  echo ""
  echo "Stopping services..."
  kill "$AGENT_PID" 2>/dev/null || true
  exit 0
}
trap cleanup SIGINT SIGTERM

# --- 4. Wait for the agent to be healthy ---
echo -n "[agent] waiting for /ping "
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${VOICE_PORT}/ping" >/dev/null 2>&1; then echo "ok"; break; fi
  echo -n "."; sleep 1
done

# --- 5. Start the frontend with voice enabled (env-injected, no file edits) ---
cd "$FRONTEND_DIR"
[ -d node_modules ] || npm install
echo "[frontend] starting on http://localhost:5173 (voice ENABLED via VITE_VOICE_WS_URL → :${VOICE_PORT})"
VITE_VOICE_WS_URL="ws://localhost:${VOICE_PORT}/ws" VITE_VOICE_DEBUG="true" npm run dev

# If the frontend exits, stop the agent too.
cleanup
