"""Environment selection shared by CDK, deployment tooling, and verification."""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "infra/environments/example.json"
# Optional buckets with the deployer's own scene-analysis data (BDA scene
# images, episode scripts). Absent = those agent features degrade gracefully.
OPTIONAL_BUCKETS = ("sceneBucket", "scriptsBucket")


def _placeholders(value):
    if isinstance(value, dict):
        return [p for v in value.values() for p in _placeholders(v)]
    if isinstance(value, list):
        return [p for v in value for p in _placeholders(v)]
    return re.findall(r"<[^<>]+>", value) if isinstance(value, str) else []


def load_config(path=None):
    path = Path(path or os.environ.get("HPM_CONFIG", DEFAULT_CONFIG)).resolve()
    cfg = json.loads(path.read_text())
    found = sorted(set(_placeholders(cfg)))
    if found:
        raise ValueError(f"Replace the placeholders {', '.join(found)} in {path} "
                         "(copy infra/environments/example.json to <name>.json and edit it)")
    for key in ("name", "prefix"):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", cfg[key]):
            raise ValueError(f"{key} must be a short lowercase deployment identifier")
    if not re.fullmatch(r"\d{12}", cfg["account"]):
        raise ValueError("account must be a 12-digit AWS account ID")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", cfg["region"]):
        raise ValueError("region must be an AWS region name")
    cfg.setdefault("stackPrefix", "".join(part.title() for part in cfg["prefix"].split("-")))
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,99}", cfg["stackPrefix"]):
        raise ValueError("Invalid stackPrefix")
    if not re.fullmatch(r"[a-z0-9]{1,10}", cfg["qualifier"]):
        raise ValueError("Invalid bootstrap qualifier")
    bucket = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
    if not bucket.fullmatch(cfg.get("mediaBucket", "")):
        raise ValueError("mediaBucket must be an S3 bucket name in your account")
    for key in OPTIONAL_BUCKETS:
        if cfg.get(key) and not bucket.fullmatch(cfg[key]):
            raise ValueError(f"{key} must be an S3 bucket name")
        cfg[key] = cfg.get(key) or ""
    cfg.setdefault("scriptsPrefix", "input/kb/")
    cfg.setdefault("dataLoaderPrincipals", [])
    for arn in cfg["dataLoaderPrincipals"]:
        if not re.fullmatch(r"arn:aws[a-z-]*:iam::\d{12}:(role|user)/[\w+=,.@/-]+", arn):
            raise ValueError("dataLoaderPrincipals entries must be IAM role or user ARNs")
    cfg.setdefault("soccerUrl", "/media/soccer/horizontal/football.mp4")
    cfg.setdefault("soccerVerticalUrl", "/media/soccer/vertical/football.mp4")
    cfg.setdefault("mediaBaseUrl", "")
    # Optional fixed agent session for events (scripts/prewarm_session.py); AgentCore
    # runtime session ids must be at least 33 characters.
    cfg.setdefault("fixedSessionId", "")
    if cfg["fixedSessionId"] and not re.fullmatch(r"[A-Za-z0-9-]{33,100}", cfg["fixedSessionId"]):
        raise ValueError("fixedSessionId must be 33-100 letters, digits or hyphens (e.g. a UUID plus a suffix)")
    cfg["runtimePrefix"] = cfg["prefix"].replace("-", "_")
    cfg.setdefault("runtimePlatformVersion", "V2")
    if cfg["runtimePlatformVersion"] not in {"V1", "V2"}:
        raise ValueError("runtimePlatformVersion must be V1 or V2")
    cfg["configPath"] = str(path)
    cfg["artifactDir"] = str(Path(os.environ.get("HPM_ARTIFACT_DIR", ROOT / "infra/.artifacts" / cfg["name"])).resolve())
    cfg["frontendDir"] = str(ROOT / "frontend/dist-deploy" / cfg["name"])
    return cfg


def stack_name(cfg, component):
    return cfg["stackPrefix"] + component
