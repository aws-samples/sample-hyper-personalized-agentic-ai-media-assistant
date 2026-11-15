#!/usr/bin/env python3
"""Front-load the WebVTT broadcast commentary into DynamoDB.

Mirrors load_events_dynamodb.py, but for the commentary track. Each WebVTT cue
becomes one item keyed by its start time on the media timeline (the same clock
as the request's `video_s` playhead), so the service can query "all commentary
up to the current moment" and stay spoiler-safe. Idempotent -- safe to re-run.

    Table: <name>  (default hpm-demo-commentary)
      PK  match_id       (S)   e.g. "SYN-MAT-0001"
      SK  commentary_sk  (S)   "<15-digit media-ms>#<seq>" (unique, time-ordered)
      attrs: start_ms (N), end_ms (N), start_s (N), end_s (N), text (S), seq (N)

Usage (from the repository root):
    .venv/bin/python backend/live-sports-agent/load_commentary_dynamodb.py --config infra/environments/<name>.json
    .venv/bin/python backend/live-sports-agent/load_commentary_dynamodb.py --table <prefix>-commentary --region <region>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from commentary_store import parse_vtt, sk_for  # noqa: E402

DEFAULT_TABLE = "hpm-demo-commentary"
VTT = HERE / "data/match_commentary.vtt"
FEED = HERE / "data/match_events.json"


def ensure_table(client, table: str) -> None:
    if table in client.list_tables()["TableNames"]:
        print(f"table '{table}' already exists")
        return
    print(f"creating table '{table}' (on-demand)...")
    client.create_table(
        TableName=table,
        AttributeDefinitions=[
            {"AttributeName": "match_id", "AttributeType": "S"},
            {"AttributeName": "commentary_sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "match_id", "KeyType": "HASH"},
            {"AttributeName": "commentary_sk", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=table)
    print("table active")


def item_for(cue: dict, match_id: str) -> dict:
    return {
        "match_id": match_id,
        "commentary_sk": sk_for(cue["start_ms"], cue["seq"]),
        "start_ms": cue["start_ms"],
        "end_ms": cue["end_ms"],
        "start_s": Decimal(str(cue["start_s"])),
        "end_s": Decimal(str(cue["end_s"])),
        "seq": cue["seq"],
        "text": cue["text"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="Environment file; sets the table and region from it")
    ap.add_argument("--table", default=os.environ.get("COMMENTARY_TABLE", DEFAULT_TABLE))
    ap.add_argument("--vtt", default=str(VTT))
    ap.add_argument("--offset-ms", type=int,
                    default=int(os.environ.get("COMMENTARY_OFFSET_MS", "0")),
                    help="shift cue times onto the video_s timeline (default 0)")
    ap.add_argument("--match-id", default=None,
                    help="defaults to the events feed's match_id")
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or None)
    args = ap.parse_args()
    account = None
    if args.config:
        from load_events_dynamodb import from_config  # noqa: E402
        args.table, args.region, account = from_config(args.config, "commentary")

    match_id = args.match_id or json.loads(FEED.read_text())["match"]["match_id"]
    cues = parse_vtt(args.vtt, offset_ms=args.offset_ms)
    print(f"match {match_id}: {len(cues)} commentary cues to load "
          f"(offset_ms={args.offset_ms})")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if account and session.client("sts").get_caller_identity()["Account"] != account:
        raise SystemExit(f"Credentials are not for account {account} in {args.config}")
    client = session.client("dynamodb")
    ensure_table(client, args.table)

    table = session.resource("dynamodb").Table(args.table)
    with table.batch_writer() as bw:
        for cue in cues:
            bw.put_item(Item=item_for(cue, match_id))
    print(f"wrote {len(cues)} items to '{args.table}'")

    from boto3.dynamodb.conditions import Key
    resp = table.query(KeyConditionExpression=Key("match_id").eq(match_id), Select="COUNT")
    print(f"verify: table now has {resp.get('Count', 0)} items for {match_id}")


if __name__ == "__main__":
    main()
