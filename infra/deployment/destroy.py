"""Delete a deployment and everything the stacks keep on purpose.

    .venv/bin/python infra/deployment/destroy.py --config infra/environments/<name>.json          # list only
    .venv/bin/python infra/deployment/destroy.py --config infra/environments/<name>.json --yes    # delete

Without --yes it only lists what exists and would be deleted; nothing changes.
With --yes it deletes, in this order:

1. The stacks, newest first: Insights, Frontend, Application, Build (each waited for).
2. What they keep (RemovalPolicy.RETAIN) or leave behind: the stacks' S3 buckets
   (emptied first, all object versions), DynamoDB tables, the Cognito user pool,
   the search collection and its security and access policies, the ECR
   repositories, log groups, the media signing key (secret, SSM parameter and
   CloudFront public key), and the Bedrock Data Automation projects of the
   Tears of Steel notebooks.
3. The CDK staging bucket and container repository, then the Bootstrap stack.

A full run takes about 30-40 minutes, mostly CloudFront and the search collection.

Everything is found by exact name from the environment file (`prefix`,
`stackPrefix`, `qualifier`, `runtimePrefix`), so other deployments in the same
account and region, even with similar names, are never matched. The content
bucket `mediaBucket` is kept unless --include-media-bucket is given, because
several deployments can share it. Deletions are permanent. Safe to re-run: what
is already gone is skipped.
"""
import argparse
import os
import time

from config import DEFAULT_CONFIG, load_config, stack_name
from deploy import aws_environment, check_identity

STACK_ORDER = ["Insights", "Frontend", "Application", "Build"]
TABLES = ["profiles", "videos", "events", "commentary", "households", "recommendation-cache", "user-interactions"]
ACCESS_POLICIES = ["owner", "api", "clips", "home", "events", "insights"]
RUNTIMES = ["movie", "sports", "voice"]
BDA_PROJECTS = ["tears-of-steel-video", "tears-of-steel-script"]


class Plan:
    """Resources to delete, with the function that deletes each one."""

    def __init__(self):
        self.items = []

    def add(self, kind, name, delete):
        self.items.append((kind, name, delete))

    def show(self):
        width = max([len(k) for k, _, _ in self.items] or [0])
        for kind, name, _ in self.items:
            print(f"  {kind:<{width}}  {name}")


def _missing(err):
    code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
    return code in {"ResourceNotFoundException", "NoSuchBucket", "NotFoundException", "NoSuchEntity",
                    "ParameterNotFound", "NoSuchPublicKey", "RepositoryNotFoundException",
                    "ValidationException"}


def bucket_names(s3, prefixes):
    """Buckets whose names start with one of the stack-specific prefixes."""
    return sorted(b["Name"] for b in s3.list_buckets()["Buckets"] if b["Name"].startswith(tuple(prefixes)))


def empty_and_delete_bucket(s3, bucket):
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objects = [{"Key": o["Key"], "VersionId": o["VersionId"]}
                   for o in page.get("Versions", []) + page.get("DeleteMarkers", [])]
        for i in range(0, len(objects), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects[i:i + 1000], "Quiet": True})
    s3.delete_bucket(Bucket=bucket)


def build_plan(session, cfg, include_media_bucket):
    region, prefix, stack_prefix = cfg["region"], cfg["prefix"], cfg["stackPrefix"]
    account, runtime_prefix, qualifier = cfg["account"], cfg["runtimePrefix"], cfg["qualifier"]
    cfn, s3 = session.client("cloudformation"), session.client("s3")
    plan = Plan()

    def stack_status(name):
        try:
            return cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        except Exception as err:  # noqa: BLE001 -- "does not exist" is a ValidationError
            if "does not exist" in str(err):
                return None
            raise

    def delete_stack(name):
        cfn.delete_stack(StackName=name)
        cfn.get_waiter("stack_delete_complete").wait(StackName=name, WaiterConfig={"Delay": 15, "MaxAttempts": 240})

    # 1. Stacks (Bootstrap last, below: the others are deleted through its roles).
    for suffix in STACK_ORDER:
        name = stack_name(cfg, suffix)
        status = stack_status(name)
        if status:
            plan.add("stack", f"{name} ({status})", lambda n=name: delete_stack(n))

    # The signing key id lives in SSM; read it before anything is deleted.
    ssm = session.client("ssm")
    param = f"/{prefix}/media-signing/key-pair-id"
    try:
        key_id = ssm.get_parameter(Name=param)["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 -- no key was created
        key_id = None

    # 2. Retained resources.
    bucket_prefixes = [stack_name(cfg, s).lower() + "-" for s in STACK_ORDER]
    buckets = bucket_names(s3, bucket_prefixes)
    if include_media_bucket and cfg["mediaBucket"] not in buckets:
        try:
            s3.head_bucket(Bucket=cfg["mediaBucket"])
            buckets.append(cfg["mediaBucket"])
        except Exception:  # noqa: BLE001 -- not created
            pass
    for b in buckets:
        plan.add("s3 bucket", b, lambda b=b: empty_and_delete_bucket(s3, b))

    ddb = session.client("dynamodb")
    existing_tables = set()
    for page in ddb.get_paginator("list_tables").paginate():
        existing_tables.update(page["TableNames"])
    for t in TABLES:
        name = f"{prefix}-{t}"
        if name in existing_tables:
            plan.add("dynamodb table", name, lambda n=name: ddb.delete_table(TableName=n))

    cognito = session.client("cognito-idp")
    for page in cognito.get_paginator("list_user_pools").paginate(MaxResults=60):
        for pool in page["UserPools"]:
            if pool["Name"] == f"{prefix}-users":
                plan.add("cognito user pool", f'{pool["Name"]} ({pool["Id"]})',
                         lambda i=pool["Id"]: cognito.delete_user_pool(UserPoolId=i))

    aoss = session.client("opensearchserverless")
    for c in aoss.batch_get_collection(names=[f"{prefix}-search"]).get("collectionDetails", []):
        def delete_collection(cid=c["id"], name=c["name"], status=c.get("status")):
            if status != "DELETING":  # a previous run already started it; just wait
                aoss.delete_collection(id=cid)
            for _ in range(120):  # the policies can go once the collection is gone
                if not aoss.batch_get_collection(names=[name]).get("collectionDetails"):
                    return
                time.sleep(10)
        plan.add("search collection", f'{c["name"]} ({c["id"]}, bills hourly)', delete_collection)
    def all_names(call, key, **kw):
        """Every policy name from a paged OpenSearch Serverless list call."""
        names, token = set(), None
        while True:
            resp = call(**kw, **({"nextToken": token} if token else {}))
            names.update(p["name"] for p in resp[key])
            token = resp.get("nextToken")
            if not token:
                return names

    for ptype, suffix in (("network", "net"), ("encryption", "enc")):
        names = all_names(aoss.list_security_policies, "securityPolicySummaries", type=ptype)
        if f"{prefix}-{suffix}" in names:
            plan.add(f"aoss {ptype} policy", f"{prefix}-{suffix}",
                     lambda n=f"{prefix}-{suffix}", t=ptype: aoss.delete_security_policy(name=n, type=t))
    access = all_names(aoss.list_access_policies, "accessPolicySummaries", type="data")
    for suffix in ACCESS_POLICIES:
        if f"{prefix}-{suffix}" in access:
            plan.add("aoss access policy", f"{prefix}-{suffix}",
                     lambda n=f"{prefix}-{suffix}": aoss.delete_access_policy(name=n, type="data"))

    ecr = session.client("ecr")
    for r in RUNTIMES:
        name = f"{prefix}-{r}"
        try:
            ecr.describe_repositories(repositoryNames=[name])
            plan.add("ecr repository", name, lambda n=name: ecr.delete_repository(repositoryName=n, force=True))
        except Exception as err:  # noqa: BLE001
            if not _missing(err):
                raise

    logs = session.client("logs")
    log_prefixes = [stack_name(cfg, s) + "-" for s in STACK_ORDER] + \
                   [f"/aws/lambda/{stack_name(cfg, s)}-" for s in STACK_ORDER] + \
                   [f"/aws/bedrock-agentcore/runtimes/{runtime_prefix}_{r}-" for r in RUNTIMES]
    for lp in log_prefixes:
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=lp):
            for g in page["logGroups"]:
                plan.add("log group", g["logGroupName"],
                         lambda n=g["logGroupName"]: logs.delete_log_group(logGroupName=n))

    secrets = session.client("secretsmanager")
    secret = f"{prefix}/media-signing-key"
    try:
        secrets.describe_secret(SecretId=secret)
        plan.add("secret", secret, lambda: secrets.delete_secret(SecretId=secret, ForceDeleteWithoutRecovery=True))
    except Exception as err:  # noqa: BLE001
        if not _missing(err):
            raise
    if key_id:
        plan.add("ssm parameter", param, lambda: ssm.delete_parameter(Name=param))
        cloudfront = session.client("cloudfront")

        def delete_public_key():
            etag = cloudfront.get_public_key(Id=key_id)["ETag"]
            cloudfront.delete_public_key(Id=key_id, IfMatch=etag)
        plan.add("cloudfront public key", key_id, delete_public_key)

    bda = session.client("bedrock-data-automation")
    wanted = {f"{prefix}-{n}" for n in BDA_PROJECTS}
    try:
        # Without the filter only LIVE projects are listed; the notebooks create DEVELOPMENT ones.
        for page in bda.get_paginator("list_data_automation_projects").paginate(projectStageFilter="ALL"):
            for p in page.get("projects", []):
                if p.get("projectName") in wanted:
                    plan.add("bda project", p["projectName"],
                             lambda a=p["projectArn"]: bda.delete_data_automation_project(projectArn=a))
    except Exception as err:  # noqa: BLE001 -- BDA unavailable in this region
        print(f"  (skipped Bedrock Data Automation projects: {err})")

    # 3. The CDK bootstrap staging bucket and container repository, then the
    # Bootstrap stack. A non-empty repository makes the stack delete fail, so
    # both go first.
    staging = f"cdk-{qualifier}-assets-{account}-{region}"
    if staging in {b["Name"] for b in s3.list_buckets()["Buckets"]}:
        plan.add("s3 bucket", staging, lambda: empty_and_delete_bucket(s3, staging))
    container = f"cdk-{qualifier}-container-assets-{account}-{region}"
    try:
        ecr.describe_repositories(repositoryNames=[container])
        plan.add("ecr repository", container, lambda: ecr.delete_repository(repositoryName=container, force=True))
    except Exception as err:  # noqa: BLE001
        if not _missing(err):
            raise
    boot = stack_name(cfg, "Bootstrap")
    status = stack_status(boot)
    if status:
        plan.add("stack", f"{boot} ({status})", lambda: delete_stack(boot))
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--yes", action="store_true", help="Delete (without it, only list)")
    parser.add_argument("--include-media-bucket", action="store_true",
                        help="Also delete the content bucket `mediaBucket` (may be shared by other deployments)")
    args = parser.parse_args()
    cfg = load_config(args.config)
    os.environ.update(aws_environment(cfg["region"]))
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    identity = check_identity(session, cfg)
    print(f'Deployment "{cfg["prefix"]}" ({cfg["stackPrefix"]}*) in {cfg["account"]} {cfg["region"]}, as {identity["Arn"]}')
    plan = build_plan(session, cfg, args.include_media_bucket)
    if not plan.items:
        print("Nothing to delete.")
        return
    print(f"{'Deleting' if args.yes else 'Would delete'} {len(plan.items)} resource(s):")
    plan.show()
    if not args.include_media_bucket:
        print(f'  (keeping the content bucket {cfg["mediaBucket"]}; add --include-media-bucket to delete it)')
    if not args.yes:
        print("Nothing deleted. Re-run with --yes to delete these; this cannot be undone.")
        return
    failed = []
    for kind, name, delete in plan.items:
        print(f"deleting {kind} {name} ...", flush=True)
        try:
            delete()
        except Exception as err:  # noqa: BLE001 -- keep going, report at the end
            if _missing(err):
                continue
            print(f"  FAILED: {err}", flush=True)
            failed.append((kind, name, err))
    if failed:
        raise SystemExit(f"{len(failed)} deletion(s) failed; fix the cause and re-run (deleted items are skipped).")
    print("Done.")


if __name__ == "__main__":
    main()
