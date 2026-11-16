"""Deploy the selected environment with your AWS credentials (profile, SSO,
environment keys or an instance role), after checking they are for the
configured account. HPM_INSTANCE_PROFILE_ONLY=1 accepts only an EC2 instance profile.

    .venv/bin/python infra/deployment/deploy.py --config infra/environments/<name>.json --create-user <login>

Run with the repository .venv after installing requirements.txt and the CDK CLI.
`--phase all` (the default) runs, in order: synth and template tests, bootstrap,
build, application, the media signing key, frontend, data (the loaders for the
data in this repository), insights, and check (smoke.py and e2e.py). Each phase
can also run alone. `--create-user <login>` then adds a sign-in user and writes
its password to infra/.artifacts/<name>/login-<login>.txt (mode 0600); `--phase user --create-user <login>` only does that.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
from config import DEFAULT_CONFIG, load_config, stack_name


# Credential settings removed when only the EC2 instance profile may be used.
_OTHER_CREDENTIALS = ["AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                      "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN",
                      "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI"]


def instance_profile_only():
    """HPM_INSTANCE_PROFILE_ONLY=1 restores the strict mode: every script ignores
    profiles, SSO and keys and uses only the EC2 instance profile."""
    return os.environ.get("HPM_INSTANCE_PROFILE_ONLY", "").lower() in ("1", "true", "yes")


def aws_environment(region):
    """Environment for the scripts and the CDK CLI they start.

    By default the standard AWS credential chain applies: AWS_PROFILE (including
    IAM Identity Center / SSO profiles), environment keys, CloudShell, or an
    instance profile. Each script then checks the account against the
    environment file before changing anything (check_identity)."""
    env = dict(os.environ)
    if instance_profile_only():
        for key in _OTHER_CREDENTIALS:
            env.pop(key, None)
        env.update(AWS_CONFIG_FILE="/dev/null", AWS_SHARED_CREDENTIALS_FILE="/dev/null",
                   AWS_EC2_METADATA_DISABLED="false")
    env.update(AWS_REGION=region, AWS_DEFAULT_REGION=region)
    return env


def check_identity(session, cfg):
    """Fail unless the credentials are for the configured account; return the caller identity."""
    creds = session.get_credentials()
    if not creds:
        raise SystemExit("No AWS credentials found: set AWS_PROFILE, run `aws sso login`, or use an instance role")
    if instance_profile_only() and creds.method != "iam-role":
        raise SystemExit("HPM_INSTANCE_PROFILE_ONLY is set: EC2 instance-profile credentials are required")
    identity = session.client("sts").get_caller_identity()
    if identity["Account"] != cfg["account"]:
        raise SystemExit(f"Credentials are for account {identity['Account']}, "
                         f"the environment file is for {cfg['account']}")
    return identity


def role_arn(caller_arn):
    """IAM ARN usable as an OpenSearch Serverless data-access principal:
    arn:...:sts::<acct>:assumed-role/<role>/<session> -> arn:...:iam::<acct>:role/<role>.
    Without the role's path; principal_arn() adds it (SSO roles have one)."""
    parts = caller_arn.split(":")
    if len(parts) == 6 and parts[2] == "sts" and parts[5].startswith("assumed-role/"):
        role = parts[5].split("/")[1]
        return f"{parts[0]}:{parts[1]}:iam::{parts[4]}:role/{role}"
    return caller_arn


def principal_arn(session, caller_arn):
    """The caller's full IAM ARN, including a role path such as
    role/aws-reserved/sso.amazonaws.com/... for IAM Identity Center roles. Falls
    back to role_arn() when iam:GetRole is not allowed."""
    arn = role_arn(caller_arn)
    if ":role/" not in arn:
        return arn
    try:
        return session.client("iam").get_role(RoleName=arn.rsplit("/", 1)[-1])["Role"]["Arn"]
    except Exception:  # noqa: BLE001 -- no iam:GetRole: use the path-less form
        return arn


def create_user(session, pool_id, username):
    """Add a Cognito user with a random permanent password and return the password.
    Refuses an existing user, so a re-run never resets someone's password."""
    cognito = session.client("cognito-idp")
    password = secrets.token_urlsafe(12) + "aA1!"
    try:
        cognito.admin_create_user(UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS")
    except cognito.exceptions.UsernameExistsException:
        raise SystemExit(f'User "{username}" already exists. To give it a new password: aws cognito-idp '
                         f"admin-set-user-password --user-pool-id {pool_id} --username {username} "
                         "--password <new-password> --permanent")
    cognito.admin_set_user_password(UserPoolId=pool_id, Username=username, Password=password, Permanent=True)
    return password


PHASES = ["synth", "bootstrap", "build", "application", "frontend", "data", "insights", "check", "user", "all"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=PHASES, default="all")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Environment JSON file")
    parser.add_argument("--create-user", metavar="LOGIN",
                        help="Afterwards, add this sign-in user; its password is written to a file only you can read")
    args = parser.parse_args()
    if args.phase == "user" and not args.create_user:
        parser.error("--phase user needs --create-user <login>")
    cfg = load_config(args.config)
    artifacts = Path(cfg["artifactDir"])
    artifacts.mkdir(parents=True, exist_ok=True)
    env = aws_environment(cfg["region"])
    env["HPM_CONFIG"] = cfg["configPath"]
    env["HPM_ARTIFACT_DIR"] = str(artifacts)
    # Template tests after each synth must run, never skip.
    env["HPM_REQUIRE_SYNTH"] = "1"
    # The SDK and the CDK CLI see the same environment, so both use the same
    # credentials; check_identity() stops a deploy into the wrong account.
    os.environ.clear()
    os.environ.update(env)
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    identity = check_identity(session, cfg)
    print("Deploying as", identity["Arn"], f"(credentials: {session.get_credentials().method})", flush=True)
    env.update(CDK_DEFAULT_ACCOUNT=cfg["account"], CDK_DEFAULT_REGION=cfg["region"])
    # The deploying role may write the search indexes (load_catalog.py runs as it).
    env["HPM_DEPLOYER_ARN"] = principal_arn(session, identity["Arn"])
    cli = os.environ.get("CDK_CLI") or shutil.which("cdk")
    if not cli and args.phase not in {"data", "check", "user"}:
        raise SystemExit("Install aws-cdk or set CDK_CLI")

    def run(command, **kwargs):
        print("Running:", " ".join(map(str, command)), flush=True)
        return subprocess.run(command, cwd=ROOT, env=env, check=True, **kwargs)

    def cdk(command, stacks=(), phase="application", extra=()):
        run([cli, command, *stacks, "--app", f"{sys.executable} infra/deployment/app.py",
             "--output", str(artifacts / ("cdk.out-build" if phase == "build" else "cdk.out")), "-c", f"phase={phase}", *extra])

    def outputs(stack):
        data = session.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
        return {x["OutputKey"]: x["OutputValue"] for x in data.get("Outputs", [])}

    if args.phase in {"synth", "all", "application"}:
        cdk("synth", extra=["--quiet"])
        run([sys.executable, "-m", "pytest", "infra/deployment", "-q"])
    if args.phase == "synth":
        return
    if args.phase in {"bootstrap", "all"}:
        run([cli, "bootstrap", f'aws://{cfg["account"]}/{cfg["region"]}',
             "--toolkit-stack-name", stack_name(cfg, "Bootstrap"), "--qualifier", cfg["qualifier"],
             *[item for key, value in cfg["tags"].items() for item in ("--tags", f"{key}={value}")]])
    if args.phase in {"build", "all"}:
        cdk("deploy", [stack_name(cfg, "Build")], phase="build", extra=["--require-approval", "never"])
        build = session.client("codebuild")
        build_id = build.start_build(projectName=outputs(stack_name(cfg, "Build"))["BuildProject"])["build"]["id"]
        while True:
            info = build.batch_get_builds(ids=[build_id])["builds"][0]
            print("Agent image build:", info["buildStatus"], info.get("currentPhase"), flush=True)
            if info["buildStatus"] != "IN_PROGRESS":
                if info["buildStatus"] != "SUCCEEDED":
                    raise SystemExit(f"Build failed: {info.get('logs', {})}")
                break
            time.sleep(20)
    if args.phase in {"application", "all"}:
        # Pin runtimes to immutable image digests, not mutable tags. Refuse a
        # deployment when source changed after the latest image build.
        built_tag = outputs(stack_name(cfg, "Build"))["ImageTag"]
        assembly = json.loads((artifacts / "cdk.out" / (stack_name(cfg, "Build") + ".template.json")).read_text())
        if assembly["Outputs"]["ImageTag"]["Value"] != built_tag:
            raise SystemExit("Backend source changed: run the build phase first")
        digests = {}
        for name in ["movie", "sports", "voice"]:
            digest = session.client("ecr").describe_images(repositoryName=f'{cfg["prefix"]}-{name}',
                imageIds=[{"imageTag": built_tag}])["imageDetails"][0]["imageDigest"]
            env[f"HPM_{name.upper()}_IMAGE_DIGEST"] = digest
            digests[name] = digest
        (artifacts / "outputs-images.json").write_text(json.dumps({"sourceAssetTag": built_tag, "digests": digests}, indent=2) + "\n")
        # Rollback stays on: a failed deploy returns to the last good state, and
        # replacements (e.g. a new shared-layer version) deploy without a prompt.
        cdk("deploy", [stack_name(cfg, "Application")], extra=["--require-approval", "never"])
    script = lambda path, *extra: run([sys.executable, path, "--config", cfg["configPath"], *extra])  # noqa: E731
    if args.phase in {"frontend", "all"}:
        # CloudFront signing key for the /media/* cookies; skipped when it exists.
        script("infra/deployment/media_signing_key.py")
        values = outputs(stack_name(cfg, "Application"))
        (artifacts / "outputs-application.json").write_text(json.dumps(values, indent=2) + "\n")
        # Set EVERY VITE variable explicitly so .env.local cannot reconnect
        # this clean deployment to the original demo.
        keys = set()
        for path in (ROOT / "frontend/src").rglob("*"):
            if path.suffix in {".ts", ".tsx"}:
                import re
                keys.update(re.findall(r"\bVITE_[A-Z0-9_]+", path.read_text()))
        env.update({key: "" for key in keys})
        env.update({"VITE_AWS_REGION": cfg["region"], "VITE_AWS_ACCOUNT_ID": cfg["account"],
            "VITE_COGNITO_USER_POOL_ID": values["UserPoolId"], "VITE_COGNITO_CLIENT_ID": values["UserPoolClientId"],
            "VITE_API_BASE_URL": values["ApiUrl"], "VITE_AGENTCORE_AGENT_ARN": values["MovieRuntimeArn"],
            "VITE_AGENTCORE_VOICE_ARN": values["VoiceRuntimeArn"], "VITE_AGENTCORE_MATCH_ARN": values["SportsRuntimeArn"],
            "VITE_ISOLATED_DEPLOYMENT": "true", "VITE_MEDIA_BASE_URL": cfg["mediaBaseUrl"],
            "VITE_LIVE_SPORTS_URL": cfg["soccerUrl"], "VITE_LIVE_SPORTS_URL_VERTICAL": cfg["soccerVerticalUrl"],
            # Optional: one agent session shared by every client (scripts/prewarm_session.py).
            "VITE_SESSION_ID": cfg.get("fixedSessionId", "")})
        run(["npm", "--prefix", "frontend", "run", "build", "--", "--outDir", cfg["frontendDir"]])
        cdk("synth", phase="frontend", extra=["--quiet"])
        run([sys.executable, "-m", "pytest", "infra/deployment", "-q"])
        cdk("deploy", [stack_name(cfg, "Frontend")], phase="frontend", extra=["--exclusively", "--require-approval", "never"])
        values.update(outputs(stack_name(cfg, "Frontend")))
        values["SourceCommit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        values["SourceDirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
        (artifacts / "deployment.json").write_text(json.dumps(values, indent=2) + "\n")
        print(json.dumps(values, indent=2))
    if args.phase in {"data", "all"}:
        # The data shipped in this repository; every loader is safe to re-run.
        script("infra/deployment/load_media.py")           # posters, micro-dramas, football video
        script("infra/deployment/load_catalog.py")         # embeds and indexes the catalog
        script("infra/deployment/seed_video_mapping.py")   # playable micro-dramas
        script("backend/live-sports-agent/load_events_dynamodb.py")
        script("backend/live-sports-agent/load_commentary_dynamodb.py")
    if args.phase in {"insights", "all"}:
        script("insights/infra/deploy_insights.py")        # after the catalog: it builds the map from it
    if args.phase in {"check", "all"}:
        script("infra/deployment/smoke.py")
        script("infra/deployment/e2e.py")
    if args.create_user:
        pool = outputs(stack_name(cfg, "Application"))["UserPoolId"]
        password = create_user(session, pool, args.create_user)
        site = outputs(stack_name(cfg, "Frontend"))["SiteUrl"]
        try:
            insights = outputs(stack_name(cfg, "Insights")).get("InsightsUrl")
        except Exception:  # noqa: BLE001 -- the insights console is not deployed
            insights = None
        # The password goes to a file only you can read, never to the console or logs.
        login_file = artifacts / f"login-{args.create_user}.txt"
        fd = os.open(login_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"{password}\n")
        print(f"\nApp:      {site}" + (f"\nInsights: {insights}" if insights else "") +
              f"\n  login:    {args.create_user}\n  password: in {login_file} (readable only by you)\n"
              "The same login works for both. Store the password elsewhere and delete the file.", flush=True)


if __name__ == "__main__":
    main()
