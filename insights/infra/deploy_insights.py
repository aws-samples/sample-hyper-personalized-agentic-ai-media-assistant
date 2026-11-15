"""Deploy the insights console (same credential handling as infra/deployment/deploy.py).

  .venv/bin/python insights/infra/deploy_insights.py [--phase synth|deploy] [--rebuild-layout]

Reads the demo's stack outputs (read only), runs the insights tests, builds the
web app, deploys only the Insights stack, and builds the catalog map once if it
does not exist yet. The demo's stacks are never deployed from here.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/deployment"))
from config import DEFAULT_CONFIG, load_config, stack_name  # noqa: E402
from deploy import aws_environment, check_identity  # noqa: E402  (credential handling shared with the demo)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["synth", "deploy"], default="deploy")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--rebuild-layout", action="store_true", help="force a new catalog map after deploy")
    args = parser.parse_args()
    cfg = load_config(args.config)
    artifacts = Path(cfg["artifactDir"]) / "insights"
    artifacts.mkdir(parents=True, exist_ok=True)
    env = aws_environment(cfg["region"])
    os.environ.clear()
    os.environ.update(env)
    import boto3
    from botocore.config import Config
    session = boto3.Session(region_name=cfg["region"])
    check_identity(session, cfg)
    cli = os.environ.get("CDK_CLI") or shutil.which("cdk")
    if not cli:
        raise SystemExit("Install aws-cdk or set CDK_CLI")

    def outputs(stack):
        data = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
        return {x["OutputKey"]: x["OutputValue"] for x in data.get("Outputs", [])}

    ext = outputs(stack_name(cfg, "Application"))
    try:
        ext["SiteUrl"] = outputs(stack_name(cfg, "Frontend")).get("SiteUrl", "")
    except Exception:  # noqa: BLE001 - the demo link is optional
        ext["SiteUrl"] = ""
    ext_path = artifacts / "external.json"
    ext_path.write_text(json.dumps(ext, indent=2) + "\n")
    web_dir = artifacts / "web"
    whatif_dir = artifacts / "whatif-build"
    env.update(HPM_CONFIG=cfg["configPath"], INSIGHTS_EXT=str(ext_path), INSIGHTS_WEB_DIR=str(web_dir),
               INSIGHTS_WHATIF_DIR=str(whatif_dir),
               CDK_DEFAULT_ACCOUNT=cfg["account"], CDK_DEFAULT_REGION=cfg["region"])

    def run(command):
        print("Running:", " ".join(map(str, command)), flush=True)
        subprocess.run(command, cwd=ROOT, env=env, check=True)

    run([sys.executable, "-m", "pytest", "insights/tests", "-q"])
    run([sys.executable, "insights/infra/stage_whatif.py", str(whatif_dir)])
    run(["npm", "--prefix", "insights/web", "ci", "--no-audit", "--no-fund"])
    run(["npm", "--prefix", "insights/web", "run", "build", "--", "--outDir", str(web_dir), "--emptyOutDir"])
    common = ["--app", f"{sys.executable} insights/infra/app.py", "--output", str(artifacts / "cdk.out")]
    stack = stack_name(cfg, "Insights")
    run([cli, "synth", stack, *common, "--quiet"])
    if args.phase == "synth":
        return
    run([cli, "deploy", stack, *common, "--exclusively", "--require-approval", "never"])
    values = outputs(stack)
    s3 = session.client("s3")
    try:
        s3.head_object(Bucket=values["DataBucket"], Key="layout/layout.json")
        missing = False
    except s3.exceptions.ClientError:
        missing = True
    if missing or args.rebuild_layout:
        print("Building the catalog map (a few minutes)...", flush=True)
        lam = session.client("lambda", config=Config(read_timeout=900, retries={"max_attempts": 0}))
        resp = lam.invoke(FunctionName=values["LayoutFunction"], Payload=json.dumps({"force": True}).encode())
        print(resp["Payload"].read().decode(), flush=True)
    (artifacts / "deployment.json").write_text(json.dumps(values, indent=2) + "\n")
    print(json.dumps(values, indent=2))


if __name__ == "__main__":
    main()
