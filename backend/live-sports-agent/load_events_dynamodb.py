#!/usr/bin/env python3
"""Front-load the normalized match events into DynamoDB.

Creates (if needed) an on-demand table and writes one item per event, keyed by
the event's real wall-clock time so the service can query "everything up to the
current playhead". Idempotent -- safe to re-run.

    Table: <name>  (default hpm-demo-events)
      PK  match_id    (S)   e.g. "SYN-MAT-0001"
      SK  event_sk    (S)   "<15-digit epoch-ms>#<seq>" (unique, time-ordered)
      attrs: event_time (N epoch ms), event_time_utc, minute_label, type,
             subtype, player_name, team_side, is_highlight, highlight_type,
             xg, video_s, frame_id, payload (full normalized event JSON)

Usage (from the repository root):
    .venv/bin/python backend/live-sports-agent/load_events_dynamodb.py --config infra/environments/<name>.json
    .venv/bin/python backend/live-sports-agent/load_events_dynamodb.py --table <prefix>-events --region <region>
"""
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path

import boto3

import sys
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from match_state import iso_to_ms  # noqa: E402
from event_store import META_SK, sk_for  # noqa: E402

DEFAULT_TABLE = "hpm-demo-events"
FEED = HERE / "data/match_events.json"
ID_MAP = HERE / "data/match_meta.json"


def ensure_table(client, table: str) -> None:
    if table in client.list_tables()["TableNames"]:
        print(f"table '{table}' already exists")
        return
    print(f"creating table '{table}' (on-demand)...")
    client.create_table(
        TableName=table,
        AttributeDefinitions=[
            {"AttributeName": "match_id", "AttributeType": "S"},
            {"AttributeName": "event_sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "match_id", "KeyType": "HASH"},
            {"AttributeName": "event_sk", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=table)
    print("table active")


def item_for(event: dict, match_id: str, seq: int) -> dict:
    """Build a DynamoDB item. Top-level attrs are for readability/console demo;
    `payload` is the source of truth the state engine deserializes."""
    ems = iso_to_ms(event["event_time_utc"])
    item = {
        "match_id": match_id,
        "event_sk": sk_for(ems, seq),
        "event_time": ems,
        "event_time_utc": event["event_time_utc"],
        "minute_label": event.get("minute_label"),
        "type": event.get("type"),
        "subtype": event.get("subtype"),
        "player_name": event.get("player_name"),
        "team_side": event.get("team_side"),
        "is_highlight": bool(event.get("is_highlight")),
        "highlight_type": event.get("highlight_type"),
        "frame_id": event.get("frame_id"),
        "video_s": event.get("video_s"),
        "xg": event.get("xg"),
        "payload": json.dumps(event, ensure_ascii=False),
    }
    # DynamoDB (resource API) rejects Python None and float; coerce/drop.
    clean = {}
    for k, v in item.items():
        if v is None:
            continue  # omit missing attrs (still present inside payload)
        if isinstance(v, float):
            v = Decimal(str(v))
        clean[k] = v
    return clean


def meta_item(feed: dict, id_map: dict, match_id: str) -> dict:
    """The match structure the engine needs before any event (see event_store)."""
    meta = {
        "match": feed["match"],
        "teams": feed["teams"],
        "clock": {"hz": id_map["clock"]["hz"], "halves": id_map["clock"]["halves"]},
        # Only players with a face id need to be in META (face avatars).
        "players": [{"player_id": p["player_id"], "face_id": p["face_id"]}
                    for p in id_map.get("players", [])
                    if p.get("player_id") and p.get("face_id")],
    }
    return {"match_id": match_id, "event_sk": META_SK,
            "payload": json.dumps(meta, ensure_ascii=False)}


def from_config(path: str, table_suffix: str) -> tuple[str, str, str]:
    """(table, region, account) from an environment file in infra/environments/."""
    sys.path.insert(0, str(HERE.parents[1] / "infra/deployment"))
    from config import load_config  # noqa: E402
    cfg = load_config(path)
    return f'{cfg["prefix"]}-{table_suffix}', cfg["region"], cfg["account"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="Environment file; sets the table and region from it")
    ap.add_argument("--table", default=os.environ.get("EVENTS_TABLE", DEFAULT_TABLE))
    ap.add_argument("--feed", default=str(FEED))
    ap.add_argument("--meta", "--id-map", dest="id_map", default=str(ID_MAP))
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or None)
    args = ap.parse_args()
    account = None
    if args.config:
        args.table, args.region, account = from_config(args.config, "events")

    feed = json.loads(Path(args.feed).read_text())
    id_map = json.loads(Path(args.id_map).read_text())
    match_id = feed["match"]["match_id"]
    events = [e for e in feed["events"]
              if e.get("frame_id") is not None and e.get("event_time_utc")]
    # chronological order -> stable seq for the composite sort key
    events.sort(key=lambda e: (iso_to_ms(e["event_time_utc"]), e.get("frame_id", 0)))
    print(f"match {match_id}: {len(events)} events to load")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if account and session.client("sts").get_caller_identity()["Account"] != account:
        raise SystemExit(f"Credentials are not for account {account} in {args.config}")
    client = session.client("dynamodb")
    ensure_table(client, args.table)

    table = session.resource("dynamodb").Table(args.table)
    with table.batch_writer() as bw:
        for seq, e in enumerate(events):
            bw.put_item(Item=item_for(e, match_id, seq))
        bw.put_item(Item=meta_item(feed, id_map, match_id))
    print(f"wrote {len(events)} events + META to '{args.table}'")

    # verify
    from boto3.dynamodb.conditions import Key
    resp = table.query(KeyConditionExpression=Key("match_id").eq(match_id), Select="COUNT")
    print(f"verify: table now has {resp.get('Count', 0)} items for {match_id}")


if __name__ == "__main__":
    main()
