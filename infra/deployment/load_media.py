"""Upload the demo media shipped in this repository into your own account.

    .venv/bin/python infra/deployment/load_media.py --config infra/environments/<name>.json
    .venv/bin/python infra/deployment/load_media.py --config ... --only catalog --only clips

Parts (all by default, or pick with --only):
  catalog  data/catalog/posters.zip         -> s3://<mediaBucket>/posters/<file>.jpg
  clips    data/media/microdramas-*.zip     -> s3://<mediaBucket>/clips/microdramas/<name>.mp4
  sports   data/media/football*.zip         -> s3://<Frontend MediaBucket>/media/soccer/{horizontal,vertical}/football.mp4

`mediaBucket` (config) is the private content bucket the agents and Lambdas
read; it is created here when missing (Block Public Access, SSE-S3, TLS only,
config region). The sports video goes to the Frontend stack's own media bucket,
served by CloudFront under /media/* to viewers holding the signed cookies, so
`sports` runs after the frontend phase. Safe to re-run: objects whose content
is unchanged (sha256 metadata) are skipped.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

from config import DEFAULT_CONFIG, ROOT, load_config, stack_name
from deploy import aws_environment

PARTS = ("catalog", "clips", "sports")
EXPECTED = {"catalog": "data/catalog/posters.zip", "clips": "data/media/microdramas-*.zip",
            "sports": "data/media/football*.zip"}
CONTENT_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".mp4": "video/mp4"}


def sources(root=ROOT):
    """part -> (zip files, [(member regex, key template)]). The first matching rule wins."""
    return {
        "catalog": (sorted((root / "data/catalog").glob("posters*.zip")),
                    [(r"posters/([^/]+\.(?:jpe?g|png))", "posters/{0}")]),
        "clips": (sorted((root / "data/media").glob("microdramas*.zip")),
                  [(r"microdramas/([^/]+\.mp4)", "clips/microdramas/{0}")]),
        "sports": (sorted((root / "data/media").glob("football*.zip")),
                   [(r"football/(horizontal|vertical)/([^/]+\.mp4)", "media/soccer/{0}/{1}")]),
    }


def plan(zips, rules):
    """[(zip path, member name, S3 key)] for every member a rule maps. Junk
    entries (directories, __MACOSX, dot files) are ignored."""
    found = {}
    for path in zips:
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if member.endswith("/") or "__MACOSX" in member or Path(member).name.startswith("."):
                    continue
                for pattern, template in rules:
                    match = re.fullmatch(pattern, member)
                    if match:
                        key = template.format(*match.groups())
                        if key in found:
                            raise ValueError(f"{key} is in both {found[key][0].name} and {path.name}")
                        found[key] = (path, member, key)
                        break
    return sorted(found.values(), key=lambda item: item[2])


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_type(key):
    return CONTENT_TYPES.get(Path(key).suffix.lower(), "application/octet-stream")


def ensure_bucket(s3, name, region, tags):
    """Create the private content bucket when missing; refuse one we cannot use."""
    from botocore.exceptions import ClientError
    try:
        s3.head_bucket(Bucket=name)
        print(f"Bucket s3://{name} exists", flush=True)
        return
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise SystemExit(f"s3://{name} exists but is not accessible from this account ({code}); "
                             "choose another mediaBucket in the config") from error
    print(f"Creating private bucket s3://{name} in {region}", flush=True)
    args = {"Bucket": name}
    if region != "us-east-1":
        args["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**args)
    s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    s3.put_bucket_encryption(Bucket=name, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_bucket_policy(Bucket=name, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
        "Sid": "TlsOnly", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
        "Resource": [f"arn:aws:s3:::{name}", f"arn:aws:s3:::{name}/*"],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}))
    if tags:
        s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]})


def upload(s3, bucket, items, workdir):
    """Extract each member and upload it unless the object already has the same sha256."""
    from botocore.exceptions import ClientError
    counts = {"uploaded": 0, "unchanged": 0}
    for zip_path, member, key in items:
        with zipfile.ZipFile(zip_path) as archive:
            # Extract by name to a flat temp file: member paths never touch the filesystem.
            local = Path(workdir) / hashlib.sha1(member.encode()).hexdigest()
            with archive.open(member) as src, open(local, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
        digest = sha256(local)
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            if head.get("Metadata", {}).get("sha256") == digest and head["ContentLength"] == local.stat().st_size:
                counts["unchanged"] += 1
                local.unlink()
                continue
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
                raise
        print(f"  upload s3://{bucket}/{key}", flush=True)
        s3.upload_file(str(local), bucket, key, ExtraArgs={
            "ContentType": content_type(key), "Metadata": {"sha256": digest}})
        counts["uploaded"] += 1
        local.unlink()
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--only", action="append", choices=PARTS, help="Upload only this part (repeatable)")
    args = parser.parse_args()
    parts = args.only or list(PARTS)
    cfg = load_config(args.config)

    work = {}
    for part in parts:
        zips, rules = sources()[part]
        if not zips:
            raise SystemExit(f"No zip files for '{part}': expected {EXPECTED[part]} in the repository")
        work[part] = plan(zips, rules)
        if not work[part]:
            raise SystemExit(f"No media entries found for '{part}' in {', '.join(z.name for z in zips)}")

    env = aws_environment(cfg["region"])
    os.environ.clear()
    os.environ.update(env)
    import boto3
    import botocore.exceptions
    session = boto3.Session(region_name=cfg["region"])
    if session.client("sts").get_caller_identity()["Account"] != cfg["account"]:
        raise SystemExit("Wrong account for this config")
    s3 = session.client("s3")

    targets = {}
    if {"catalog", "clips"} & set(work):
        ensure_bucket(s3, cfg["mediaBucket"], cfg["region"], cfg["tags"])
        targets.update(catalog=cfg["mediaBucket"], clips=cfg["mediaBucket"])
    if "sports" in work:
        cfn = session.client("cloudformation")
        try:
            stack = cfn.describe_stacks(StackName=stack_name(cfg, "Frontend"))["Stacks"][0]
        except botocore.exceptions.ClientError:
            if args.only:
                raise SystemExit(f"Stack {stack_name(cfg, 'Frontend')} not found: deploy the frontend phase first")
            print(f"Skipping sports: stack {stack_name(cfg, 'Frontend')} not deployed yet "
                  "(re-run with --only sports after the frontend phase)", file=sys.stderr)
            work.pop("sports")
        else:
            targets["sports"] = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}["MediaBucket"]

    with tempfile.TemporaryDirectory(prefix="hpm-media-") as workdir:
        for part, items in work.items():
            print(f"{part}: {len(items)} objects -> s3://{targets[part]}/", flush=True)
            counts = upload(s3, targets[part], items, workdir)
            print(f"{part}: {counts['uploaded']} uploaded, {counts['unchanged']} unchanged", flush=True)
    print("Done")


if __name__ == "__main__":
    main()
