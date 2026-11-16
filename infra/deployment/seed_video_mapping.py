"""Load seed/title_video_mapping.json into this deployment's video mapping table.

    .venv/bin/python infra/deployment/seed_video_mapping.py --config infra/environments/dev.json
    .venv/bin/python infra/deployment/seed_video_mapping.py --dry-run   # print the items, no AWS

Fills {mediaBucket} in each video_s3_url from the environment config, then
writes one item per title (put-item, so re-running is safe). The table is the
Application stack output VideosTableName. Run after load_media.py has uploaded
the micro-drama videos to s3://<mediaBucket>/clips/microdramas/.
"""
import argparse
import json
import os
from pathlib import Path

from config import DEFAULT_CONFIG, load_config, stack_name

SEED = Path(__file__).resolve().parent / "seed/title_video_mapping.json"


def items(media_bucket, path=SEED):
    """Seed rows with the bucket placeholder filled in. Rows marked
    "optional" keep the marker so main() can skip them when their video
    has not been uploaded."""
    rows = json.loads(Path(path).read_text())["items"]
    return [{k: v.replace("{mediaBucket}", media_bucket) if isinstance(v, str) else v for k, v in row.items()}
            for row in rows]


def _exists(s3, url):
    bucket, key = url[len("s3://"):].split("/", 1)
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 -- missing or unreadable: treat as absent
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true", help="Print the items and exit; no AWS calls")
    args = parser.parse_args()
    cfg = load_config(args.config)
    rows = items(cfg["mediaBucket"])
    if args.dry_run:
        for row in rows:
            print(json.dumps(row))
        return
    from deploy import aws_environment
    env = aws_environment(cfg["region"])
    os.environ.clear()
    os.environ.update(env)
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    if session.client("sts").get_caller_identity()["Account"] != cfg["account"]:
        raise SystemExit("Wrong deployment account")
    stack = session.client("cloudformation").describe_stacks(StackName=stack_name(cfg, "Application"))["Stacks"][0]
    table_name = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}["VideosTableName"]
    table = session.resource("dynamodb").Table(table_name)
    s3 = session.client("s3")
    seeded = 0
    for row in rows:
        if row.pop("optional", False):
            if not _exists(s3, row["video_s3_url"]):
                print(f'Skipping {row["title_id"]}: {row["video_s3_url"]} not uploaded (optional)')
                continue
        table.put_item(Item=row)
        seeded += 1
    print(f"Seeded {seeded} video mapping(s) into {table_name}")


if __name__ == "__main__":
    main()
