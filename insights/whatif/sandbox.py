"""Sandbox profile: the member's live profile copied into memory, then changed
by hypothetical activity through the REAL stream-processor code.

The processor module (infra/lambdas/profile_events/processor.py, bundled
unchanged at build time) is called with in-memory stand-ins for its two
DynamoDB tables, so `update_long_term` runs exactly as in production but every
read comes from the sandbox and every write lands back in the sandbox. Nothing
reaches the demo's tables.
"""

import copy
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

import processor  # bundled copy of infra/lambdas/profile_events/processor.py
import l2

FEEDBACK_VALUES = ("like", "more", "dislike")
LONG_VIEW_S = 2 * 3600        # a hypothetical full viewing of a film
MICRODRAMA_S = 90             # and of a microdrama card


def native(v):
    return processor._to_native(v)


class SandboxProfiles:
    """Stand-in for the profiles table: get_item / put_item on the sandbox."""

    def __init__(self, items):
        self.items = items     # data_type -> native item

    def get_item(self, Key, **_kw):
        item = self.items.get(Key["data_type"])
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, **_kw):
        self.items[Item["data_type"]] = native(Item)
        return {}


class SandboxInteractions:
    """Stand-in for user-interactions: TITLE# watch state in memory."""

    def __init__(self, titles):
        self.titles = titles   # title_id -> native TITLE# item

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, **_kw):
        tid = Key["sk"].split("#", 1)[1]
        cur = dict(self.titles.get(tid) or {"profile_id": Key["profile_id"], "sk": Key["sk"]})
        v = native(ExpressionAttributeValues)
        cur.update(last_watched=v[":ts"], completion=v[":c"], content_type=v[":t"])
        if ":yes" in v:
            cur["completed"] = True
            cur["times_watched"] = int(cur.get("times_watched") or 0) + 1
        else:
            cur.setdefault("completed", False)
        self.titles[tid] = cur
        return {}

    def query(self, **_kw):
        # Only _seed_centroids queries: completed TITLE# items.
        items = [{"sk": f"TITLE#{t}", "last_watched": i.get("last_watched", "")}
                 for t, i in self.titles.items() if i.get("completed")]
        return {"Items": items}


class Sandbox:
    def __init__(self, profile_id, profiles_items, titles):
        self.profile_id = profile_id
        self.profiles = SandboxProfiles(profiles_items)
        self.interactions = SandboxInteractions(titles)

    @classmethod
    def load(cls, profile_id, profiles_table, interactions_table):
        """Read the live profile once (read-only)."""
        ddb = boto3.resource("dynamodb")
        rows = ddb.Table(profiles_table).query(KeyConditionExpression=Key("user_id").eq(profile_id))["Items"]
        items = {r["data_type"]: native(r) for r in rows}
        titles, start = {}, None
        while True:
            kw = {"KeyConditionExpression": Key("profile_id").eq(profile_id) & Key("sk").begins_with("TITLE#")}
            if start:
                kw["ExclusiveStartKey"] = start
            page = ddb.Table(interactions_table).query(**kw)
            for i in page["Items"]:
                titles[i["sk"].split("#", 1)[1]] = native(i)
            start = page.get("LastEvaluatedKey")
            if not start:
                break
        return cls(profile_id, items, titles)

    def clone(self):
        return Sandbox(self.profile_id, copy.deepcopy(self.profiles.items), copy.deepcopy(self.interactions.titles))

    # --- views of the sandbox -------------------------------------------------
    @property
    def affinities(self):
        return self.profiles.items.get("AFFINITIES") or {}

    @property
    def declared(self):
        return self.profiles.items.get("L3#DECLARED") or {}

    def completed(self):
        """Completed titles, newest first (what the agent and the feed exclude)."""
        done = [(i.get("last_watched") or "", t) for t, i in self.interactions.titles.items() if i.get("completed")]
        return [t for _, t in sorted(done, reverse=True)]

    def centroids(self):
        return l2.decode_centroids(self.affinities.get("centroids"))

    def set_declared(self, excluded_genres=None, avoid_themes=None):
        d = dict(self.declared or {"user_id": self.profile_id, "data_type": "L3#DECLARED"})
        if excluded_genres is not None:
            d["excluded_genres"] = list(dict.fromkeys(excluded_genres))
        if avoid_themes is not None:
            d["avoid_themes"] = list(dict.fromkeys(avoid_themes))
        self.profiles.items["L3#DECLARED"] = d

    # --- hypothetical activity --------------------------------------------------
    def apply(self, kind, item, value=None, ts=None, bucket=None, is_microdrama=False):
        """One hypothetical signal through processor.update_long_term.
        Returns (e, the signal) -- e is None when the signal is not scored."""
        ts = ts or time.time()
        if kind == "view":
            seconds = MICRODRAMA_S if is_microdrama else LONG_VIEW_S
            view = {"item": item, "type": "microdrama" if is_microdrama else "movie", "watched_s": seconds,
                    "duration_s": seconds, "completion": 1.0, "reason": "ended", "ts": ts, "plays": 1}
            if bucket:
                view["bucket"] = bucket
            sig = ("view", item, l2.view_engagement(view), view)
        elif kind == "feedback" and value in FEEDBACK_VALUES:
            sig = ("feedback", item, l2.FEEDBACK_E[value], {"item": item, "value": value, "ts": ts})
        else:
            raise ValueError(f"unknown signal {kind}/{value}")
        processor.prefetch_catalog([item])
        processor.update_long_term(self.profile_id, [sig], table=self.interactions, profiles=self.profiles,
                                   tz_offset_min=self.affinities.get("tz_offset_min"))
        return sig[2], sig


def decimal_free(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: decimal_free(x) for k, x in v.items()}
    if isinstance(v, list):
        return [decimal_free(x) for x in v]
    return v
