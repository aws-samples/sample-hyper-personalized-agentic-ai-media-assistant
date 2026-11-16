"""Smoke-test only the selected deployment; remove the temporary user."""
import json
import os
import secrets
import urllib.parse
import urllib.request
import uuid
import ast
import argparse
import re
from pathlib import Path

from deploy import aws_environment, check_identity
from config import DEFAULT_CONFIG, load_config, stack_name


def check_frontend(outputs):
    for route in ["/", "/home", "/search", "/title/tt1001001", "/watch/live/soccer"]:
        with urllib.request.urlopen(outputs["SiteUrl"] + route, timeout=60) as response:
            html = response.read()
            assert response.status == 200 and b'<div id="root">' in html
        print("PASS: CloudFront SPA route", route)
    # A missing file is a real 404, not the SPA shell.
    try:
        urllib.request.urlopen(outputs["SiteUrl"] + "/assets/does-not-exist.js", timeout=60)
        raise AssertionError("missing asset was served")
    except urllib.error.HTTPError as error:
        assert error.code in (403, 404), f"missing asset returned HTTP {error.code}"
    print("PASS: missing asset is not served as index.html")
    with urllib.request.urlopen(outputs["SiteUrl"] + "/home", timeout=60) as response:
        html = response.read()
    scripts = re.findall(rb'<script[^>]+src="([^"]+)"', html)
    assert scripts
    source = b""
    for script in scripts:
        with urllib.request.urlopen(urllib.parse.urljoin(outputs["SiteUrl"], script.decode()), timeout=60) as response:
            assert "javascript" in response.headers.get("Content-Type", "")
            source += response.read()
    for key in ["UserPoolId", "ApiUrl", "MovieRuntimeArn", "SportsRuntimeArn", "VoiceRuntimeArn"]:
        assert outputs[key].encode() in source, f"Missing new endpoint: {key}"
    print("PASS: published JavaScript uses new application endpoints")


def check_media(outputs, cfg, id_token):
    """/media/* plays only with the signed cookies GET /media/session issues."""
    def head(path, cookie=None):
        """First bytes of the media file (MP4 starts with a box whose type is 'ftyp')."""
        headers = {"Range": "bytes=0-11", **({"Cookie": cookie} if cookie else {})}
        try:
            with urllib.request.urlopen(urllib.request.Request(outputs["SiteUrl"] + path, headers=headers), timeout=60) as response:
                return response.read(12)
        except urllib.error.HTTPError as error:
            return f"HTTP {error.code}".encode()
    paths = [p for p in (cfg["soccerUrl"], cfg["soccerVerticalUrl"]) if p.startswith("/media/")]
    # Unsigned: CloudFront refuses with a real 403 (no SPA error page on /media/*).
    for path in paths:
        got = head(path)
        assert got == b"HTTP 403", f"{path} without signed cookies returned {got!r}, expected HTTP 403"
    print("PASS: media refused with 403 without signed cookies")
    request = urllib.request.Request(outputs["ApiUrl"] + "media/session", headers={"Authorization": id_token})
    with urllib.request.urlopen(request, timeout=60) as response:
        cookies = json.load(response)["cookies"]
    cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
    for path in paths:
        assert head(path, cookie)[4:8] == b"ftyp", f"{path} not served with signed cookies (run load_media.py)"
    print("PASS: media served with signed cookies", len(paths), "streams")


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--application-only", action="store_true")
    group.add_argument("--frontend-only", action="store_true")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    cfg = load_config(args.config)
    env = aws_environment(cfg["region"])
    os.environ.clear()
    os.environ.update(env)
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    check_identity(session, cfg)
    cfn = session.client("cloudformation")
    outputs = {}
    stacks = [stack_name(cfg, "Application")] if args.application_only else [stack_name(cfg, "Application"), stack_name(cfg, "Frontend")]
    for stack in stacks:
        data = cfn.describe_stacks(StackName=stack)["Stacks"][0]
        assert data["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
        assert all({t["Key"]: t["Value"] for t in data["Tags"]}.get(k) == v for k, v in cfg["tags"].items())
        outputs.update({x["OutputKey"]: x["OutputValue"] for x in data["Outputs"]})
    if args.frontend_only:
        check_frontend(outputs)
        return
    pool = outputs["UserPoolId"]
    control = session.client("bedrock-agentcore-control")
    for name in ["Movie", "Sports", "Voice"]:
        runtime_id = outputs[name + "RuntimeArn"].rsplit("/", 1)[1]
        assert runtime_id.startswith(cfg["runtimePrefix"] + "_")
        runtime = control.get_agent_runtime(agentRuntimeId=runtime_id)
        assert runtime["status"] == "READY"
        assert runtime["platformVersion"] == cfg["runtimePlatformVersion"]
        print("PASS: runtime ready", name, runtime["platformVersion"])
    # The Knowledge Base starts empty (episode scripts are the deployer's own
    # data), so only check that retrieval works.
    result = session.client("bedrock-agent-runtime").retrieve(
        knowledgeBaseId=outputs["KnowledgeBaseId"], retrievalQuery={"text": "what happens in this scene"})
    print("PASS: Knowledge Base retrieval", len(result["retrievalResults"]), "results")
    # Exercise the sports agent's actual MCP client without importing its model
    # and data-store initialization or invoking any existing environment.
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    source = Path(__file__).resolve().parents[2] / "backend/live-sports-agent/agentcore_app.py"
    functions = [n for n in ast.parse(source.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name in {"_mcp_post", "_web_search"}]
    scope = {"json": json, "os": os, "urllib": __import__("urllib"), "SigV4Auth": SigV4Auth,
        "AWSRequest": AWSRequest, "_WEBSEARCH_SESSION": session, "_SIGV4_SERVICE": "bedrock-agentcore",
        "WEBSEARCH_URL": outputs["WebSearchUrl"], "WEBSEARCH_REGION": cfg["region"],
        "WEBSEARCH_TOOL_NAME": "web-search-tool___WebSearch"}
    os.environ["WEBSEARCH_TIMEOUT_SECONDS"] = "125"
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), scope)
    search = scope["_web_search"]("What is the official FIFA website?", 2)
    assert search["results"] and all(r["url"].startswith("https://") for r in search["results"]), search
    print("PASS: gateway web search with source URLs")
    assert pool.startswith(cfg["region"] + "_")
    cognito = session.client("cognito-idp")
    assert cognito.describe_user_pool(UserPoolId=pool)["UserPool"]["Name"] == cfg["prefix"] + "-users"
    username = "deployment-check-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32) + "aA1!"
    created = False
    try:
        cognito.admin_create_user(UserPoolId=pool, Username=username, MessageAction="SUPPRESS")
        created = True
        cognito.admin_set_user_password(UserPoolId=pool, Username=username, Password=password, Permanent=True)
        auth = cognito.initiate_auth(ClientId=outputs["UserPoolClientId"], AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password})["AuthenticationResult"]
        print("PASS: new Cognito login")
        for route in ["get_list", "get_carousels", "get_top_picks"]:
            params = urllib.parse.urlencode({"user_id": username, "profile": "action_fan"})
            request = urllib.request.Request(outputs["ApiUrl"] + route + "?" + params, headers={"Authorization": auth["IdToken"]})
            with urllib.request.urlopen(request, timeout=60) as response:
                json.load(response)
                assert response.status == 200
            print("PASS: authenticated API", route)
        arn = outputs["MovieRuntimeArn"]
        assert f':{cfg["region"]}:' in arn and f'/{cfg["runtimePrefix"]}_movie-' in arn
        url = session.client("bedrock-agentcore").meta.endpoint_url + "/runtimes/" + urllib.parse.quote(arn, safe="") + "/invocations?qualifier=DEFAULT"
        request = urllib.request.Request(url, data=json.dumps({"request_type": "ping"}).encode(),
            headers={"Authorization": "Bearer " + auth["AccessToken"], "Content-Type": "application/json",
                     "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid.uuid4())})
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read().decode()
            assert "pong" in body, body[:300]
        print("PASS: authenticated movie runtime ping")
        if not args.application_only:
            check_media(outputs, cfg, auth["IdToken"])
            check_frontend(outputs)
    finally:
        if created:
            cognito.admin_delete_user(UserPoolId=pool, Username=username)
            print("Temporary validation user removed")


if __name__ == "__main__":
    main()
