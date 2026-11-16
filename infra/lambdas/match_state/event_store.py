#!/usr/bin/env python3
"""DynamoDB-backed event store for the live match-state engine.

The match's events are front-loaded into a DynamoDB table keyed by the event's
real wall-clock time. Reads are bounded by the current playhead:

    events_upto(now_ms) -> Query(match_id = MID AND event_sk < bound(now_ms))

This mirrors how a production live-ingestion pipeline would work -- events are
stored by when they occurred, and consumers query everything up to "now" -- while
staying spoiler-safe (the query never returns events past the playhead).

Sort key `event_sk` is a composite "<15-digit epoch-ms>#<seq>" string: epoch-ms
alone is not unique (two events can land on the same millisecond), so we append a
chronological sequence number. Fixed-width zero-padding makes lexicographic order
equal numeric time order, so a simple `< bound` range query works.

Each item stores the full normalized event JSON in a `payload` attribute so the
match-state engine receives byte-identical event dicts regardless of backend.

One extra item per match, `event_sk = "META"`, holds the match structure the
engine needs before any event: the match ids, both teams, the clock (frame rate
and per-half anchors) and the optional player -> face id map for avatars.
"META" sorts above every "<digits>#<seq>" key, so the playhead-bounded event
query never returns it.
"""
from __future__ import annotations

import json

from boto3.dynamodb.conditions import Key

META_SK = "META"
# Every event key starts with a digit; "A" sorts above all of them and below META.
_EVENTS_END = "A"


def sk_for(event_ms: int, seq: int) -> str:
    """Composite, time-ordered, unique sort key."""
    return f"{int(event_ms):015d}#{seq:04d}"


def sk_bound(bound_ms: int) -> str:
    """Exclusive upper bound as a bare padded ms string. Since event_sk is
    '<15-digit ms>#<seq>', any event with ms < bound_ms sorts strictly below
    this (equal-ms events have the '#seq' suffix, so they sort above)."""
    return f"{int(bound_ms):015d}"


class DynamoEventStore:
    def __init__(self, table_name: str, match_id: str, session):
        self.table = session.resource("dynamodb").Table(table_name)
        self.match_id = match_id

    def events_upto(self, bound_ms: int) -> list[dict]:
        """All events with event_time < bound_ms, in chronological order.
        (bound_ms is an exclusive upper bound; the caller sets it to the current
        playhead's frame boundary.)

        DynamoDB returns range-key results ascending by default, so the list is
        already time-ordered (which the state engine relies on for recent_events).
        """
        cond = Key("match_id").eq(self.match_id) & Key("event_sk").lt(sk_bound(bound_ms))
        items: list[dict] = []
        resp = self.table.query(KeyConditionExpression=cond)
        items.extend(resp["Items"])
        while "LastEvaluatedKey" in resp:
            resp = self.table.query(KeyConditionExpression=cond,
                                    ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp["Items"])
        return [json.loads(it["payload"]) for it in items]

    def meta(self) -> dict:
        """The match structure item (match, teams, clock, players). Raises if the
        table has not been loaded for this match."""
        item = self.table.get_item(Key={"match_id": self.match_id, "event_sk": META_SK}).get("Item")
        if not item:
            raise RuntimeError(f"no META item for {self.match_id} in {self.table.name}; "
                               "load it with backend/live-sports-agent/load_events_dynamodb.py")
        return json.loads(item["payload"])

    def count(self) -> int:
        """Number of events stored for this match (cheap startup probe)."""
        cond = Key("match_id").eq(self.match_id) & Key("event_sk").lt(_EVENTS_END)
        resp = self.table.query(KeyConditionExpression=cond, Select="COUNT")
        total = resp.get("Count", 0)
        while "LastEvaluatedKey" in resp:
            resp = self.table.query(KeyConditionExpression=cond, Select="COUNT",
                                    ExclusiveStartKey=resp["LastEvaluatedKey"])
            total += resp.get("Count", 0)
        return total
