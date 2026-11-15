"""Write each highlight clip's genre sentence into its S3 object metadata.

    .venv/bin/python infra/deployment/annotate_highlights.py --config infra/environments/<name>.json

Reads seed/highlight_descriptions.json ({file name: sentence}) and sets
`x-amz-meta-description` on that file under both orientations of the highlight
prefix in cfg["mediaBucket"]. S3 metadata can only change by copying the object
onto itself, so the bytes and content type are kept and only the metadata is
replaced. Safe to re-run: a clip whose description already matches is skipped.
get_video_url reads the sentence back with HeadObject when it picks a clip.
The Tears of Steel notebook uploads its clips with these descriptions already
set; run this after changing a sentence or adding clips of your own.
"""
import argparse
import json
import os
from pathlib import Path

from config import DEFAULT_CONFIG, load_config
from deploy import aws_environment

SEED = Path(__file__).resolve().parent / "seed" / "highlight_descriptions.json"
PREFIX = "clips/highlights/vod/"  # matches stacks.py HIGHLIGHT_PREFIX
ORIENTATIONS = ("horizontal", "vertical")
META_KEY = "description"  # stored as x-amz-meta-description


def load_descriptions(path=SEED):
    data = {k: v for k, v in json.loads(Path(path).read_text()).items() if not k.startswith("_")}
    for name, text in data.items():
        if not text.isascii():  # S3 user metadata is US-ASCII only
            raise SystemExit(f"{name}: description must be plain ASCII")
    return data


def annotate(s3, bucket, descriptions, prefix=PREFIX):
    """Set the description on every listed clip; returns [(key, action)]."""
    done = []
    for name, text in descriptions.items():
        for orientation in ORIENTATIONS:
            key = f"{prefix}{orientation}/{name}"
            try:
                head = s3.head_object(Bucket=bucket, Key=key)
            except s3.exceptions.ClientError:
                done.append((key, "missing"))
                continue
            meta = head.get("Metadata") or {}
            if meta.get(META_KEY) == text:
                done.append((key, "unchanged"))
                continue
            s3.copy_object(Bucket=bucket, Key=key, CopySource={"Bucket": bucket, "Key": key},
                           MetadataDirective="REPLACE", ContentType=head.get("ContentType") or "video/mp4",
                           Metadata={**meta, META_KEY: text})
            done.append((key, "written"))
    return done


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    cfg = load_config(args.config)
    env = aws_environment(cfg["region"])
    os.environ.clear()
    os.environ.update(env)
    import boto3
    descriptions = load_descriptions()
    if not descriptions:
        print("No highlight descriptions in", SEED.name)
        return
    session = boto3.Session(region_name=cfg["region"])
    if session.client("sts").get_caller_identity()["Account"] != cfg["account"]:
        raise SystemExit("Wrong deployment account")
    for key, action in annotate(session.client("s3"), cfg["mediaBucket"], descriptions):
        print(f"{action:9} s3://{cfg['mediaBucket']}/{key}")


if __name__ == "__main__":
    main()
