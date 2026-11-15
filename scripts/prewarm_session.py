"""Keep the movie agent's shared session warm during an event (optional).

    export HPM_PREWARM_PASSWORD=...          # the login's password (or you are prompted)
    .venv/bin/python scripts/prewarm_session.py --config infra/environments/<name>.json \
        --username <booth-login> [--interval 240]

Use it with `fixedSessionId` in the environment file, which makes every client
use one AgentCore runtime session. This script pings that session every
`--interval` seconds with a `{"request_type": "ping"}` call. The movie runtime
answers "pong" without running the pipeline or calling a model, so the session's
microVM stays warm and the first visitor's question has no cold start. Stop it
with Ctrl+C after the event.

The login is any Cognito user of the deployment; a member of the `booth` group
is the natural choice (README: Shared demo logins).
"""
import argparse
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/deployment"))
from config import DEFAULT_CONFIG, load_config, stack_name  # noqa: E402
from deploy import aws_environment, check_identity  # noqa: E402

TOKEN_REFRESH_S = 45 * 60  # Cognito access tokens last an hour


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--username", required=True, help="Cognito login used for the pings")
    parser.add_argument("--interval", type=int, default=240, help="Seconds between pings")
    args = parser.parse_args()
    cfg = load_config(args.config)
    session_id = cfg.get("fixedSessionId")
    if not session_id:
        raise SystemExit("Set fixedSessionId in the environment file (and deploy the frontend phase) first")
    password = os.environ.get("HPM_PREWARM_PASSWORD") or getpass.getpass(f"Password for {args.username}: ")

    os.environ.update(aws_environment(cfg["region"]))
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    check_identity(session, cfg)
    stack = session.client("cloudformation").describe_stacks(StackName=stack_name(cfg, "Application"))["Stacks"][0]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
    url = (session.client("bedrock-agentcore").meta.endpoint_url + "/runtimes/"
           + urllib.parse.quote(outputs["MovieRuntimeArn"], safe="") + "/invocations?qualifier=DEFAULT")
    cognito = session.client("cognito-idp")

    token, fetched = None, 0.0
    print(f"Pinging session {session_id} every {args.interval}s (Ctrl+C to stop)", flush=True)
    while True:
        if not token or time.time() - fetched > TOKEN_REFRESH_S:
            token = cognito.initiate_auth(ClientId=outputs["UserPoolClientId"], AuthFlow="USER_PASSWORD_AUTH",
                                          AuthParameters={"USERNAME": args.username, "PASSWORD": password}
                                          )["AuthenticationResult"]["AccessToken"]
            fetched = time.time()
        request = urllib.request.Request(url, data=json.dumps({"request_type": "ping"}).encode(), headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id})
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                ok = "pong" in response.read().decode()
            print(f"{time.strftime('%H:%M:%S')} {'pong' if ok else 'unexpected reply'} "
                  f"in {time.time() - started:.1f}s", flush=True)
        except urllib.error.HTTPError as err:
            print(f"{time.strftime('%H:%M:%S')} HTTP {err.code}: {err.read()[:200]!r}", flush=True)
            if err.code in (401, 403):
                token = None  # sign in again on the next ping
        except Exception as exc:  # noqa: BLE001 -- keep pinging through transient errors
            print(f"{time.strftime('%H:%M:%S')} error: {exc}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped")
