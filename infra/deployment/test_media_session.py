"""GET /media/session: signed cookies CloudFront accepts, and the CDK wiring
for the private /media/* path."""
import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from config import load_config, stack_name

ROOT = Path(__file__).resolve().parents[2]
# Lambda and layer directories are CDK assets: no bytecode there.
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "infra/layers/shared/python"))
CFG = load_config()
OUT = Path(CFG["artifactDir"]) / "cdk.out"


def _application_template():
    path = OUT / (stack_name(CFG, "Application") + ".template.json")
    if not path.exists() and not os.environ.get("HPM_REQUIRE_SYNTH"):
        pytest.skip("Synthesize infra/deployment/app.py first")
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def media():
    import os
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
    spec = importlib.util.spec_from_file_location("media_session_fn", ROOT / "infra/lambdas/media_session/lambda_function.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    folder = tmp_path_factory.mktemp("key")
    private = subprocess.run(["openssl", "genrsa", "-traditional", "2048"], check=True, capture_output=True, text=True).stdout
    public = subprocess.run(["openssl", "rsa", "-pubout"], input=private, check=True, capture_output=True, text=True).stdout
    (folder / "public.pem").write_text(public)
    return private, folder / "public.pem"


def cf_b64decode(text):
    return base64.b64decode(text.translate(str.maketrans("-_~", "+=/")))


def test_signature_verifies_with_openssl(media, keypair, tmp_path):
    private, public = keypair
    n, d = media.load_private_key(private)
    cookies = media.signed_cookies("K2TESTKEYID", n, d, 1_900_000_000)
    policy = cf_b64decode(cookies["CloudFront-Policy"])
    (tmp_path / "policy").write_bytes(policy)
    (tmp_path / "sig").write_bytes(cf_b64decode(cookies["CloudFront-Signature"]))
    result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature",
                             str(tmp_path / "sig"), str(tmp_path / "policy")], capture_output=True, text=True)
    assert result.returncode == 0 and "Verified OK" in result.stdout, result.stdout + result.stderr


def test_cookie_contents(media, keypair):
    n, d = media.load_private_key(keypair[0])
    cookies = media.signed_cookies("K2TESTKEYID", n, d, 1_900_000_000)
    assert cookies["CloudFront-Key-Pair-Id"] == "K2TESTKEYID"
    assert cookies["CloudFront-Hash-Algorithm"] == "SHA256"
    statement = json.loads(cf_b64decode(cookies["CloudFront-Policy"]))["Statement"]
    assert statement == [{"Resource": "https://*/media/*", "Condition": {"DateLessThan": {"AWS:EpochTime": 1_900_000_000}}}]
    # CloudFront's cookie alphabet: no +, = or / in any value.
    assert not any(c in value for value in cookies.values() for c in "+=/")


def test_rejects_pkcs8_keys(media):
    with pytest.raises(ValueError):
        media.load_private_key("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")


def test_handler_issues_four_hour_cookies(media, keypair, monkeypatch):
    n, d = media.load_private_key(keypair[0])
    monkeypatch.setattr(media, "_signing_key", lambda: ("K2TESTKEYID", n, d))
    monkeypatch.setattr(media.time, "time", lambda: 1_000_000)
    response = media.lambda_handler({"httpMethod": "GET"}, None)
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert body["expires"] == 1_000_000 + 4 * 3600 and body["path"] == "/media/"
    assert set(body["cookies"]) == {"CloudFront-Policy", "CloudFront-Signature", "CloudFront-Key-Pair-Id", "CloudFront-Hash-Algorithm"}
    assert media.lambda_handler({"httpMethod": "POST"}, None)["statusCode"] == 405


def test_media_route_uses_cognito_and_reads_only_its_secret():
    app = _application_template()
    methods = [r for r in app["Resources"].values() if r["Type"] == "AWS::ApiGateway::Method"
               and "MediaSessionFn" in json.dumps(r["Properties"].get("Integration", {}))]
    assert len(methods) == 1 and methods[0]["Properties"]["AuthorizationType"] == "COGNITO_USER_POOLS"
    statements = [s for r in app["Resources"].values() if r["Type"] == "AWS::IAM::Policy"
                  for s in r["Properties"]["PolicyDocument"]["Statement"] if "secretsmanager" in json.dumps(s["Action"])]
    assert statements and all(f'secret:{CFG["prefix"]}/media-signing-key-*' in json.dumps(s["Resource"]) for s in statements)


def _frontend_template():
    path = OUT / (stack_name(CFG, "Frontend") + ".template.json")
    if not path.exists():
        pytest.skip("Synthesize infra/deployment/app.py first")
    return json.loads(path.read_text())


def test_refused_media_is_not_turned_into_the_spa_page():
    # SPA routing is a viewer-request function on the default behavior only, and
    # the distribution has no error pages, so /media/* keeps its real 403.
    tpl = _frontend_template()
    dist = next(r for r in tpl["Resources"].values() if r["Type"] == "AWS::CloudFront::Distribution")
    conf = dist["Properties"]["DistributionConfig"]
    assert not conf.get("CustomErrorResponses")
    assert conf["DefaultCacheBehavior"]["FunctionAssociations"][0]["EventType"] == "viewer-request"
    media_behavior = next(b for b in conf["CacheBehaviors"] if b["PathPattern"] == "/media/*")
    assert not media_behavior.get("FunctionAssociations")


@pytest.mark.parametrize("uri,expected", [
    ("/", "/index.html"), ("/home", "/index.html"), ("/title/tt1001001", "/index.html"),
    ("/watch/live/soccer", "/index.html"), ("/assets/index-abc.js", "/assets/index-abc.js"),
    ("/favicon.svg", "/favicon.svg"), ("/media/soccer/horizontal/football.mp4", "/media/soccer/horizontal/football.mp4"),
])
def test_spa_route_function(uri, expected):
    import shutil
    from stacks import SPA_ROUTES_JS
    if not shutil.which("node"):
        pytest.skip("node not installed")
    script = SPA_ROUTES_JS + f"\nconsole.log(handler({{request: {{uri: {json.dumps(uri)}}}}}).uri);"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout.strip()
    assert out == expected
