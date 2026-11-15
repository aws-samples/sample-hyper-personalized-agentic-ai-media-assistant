"""get_carousels — all of a profile's Home carousels in one call.

Queries every `carousel_*` item for the profile, hydrates all referenced
titles in a single OpenSearch request, and returns an ordered list of
{ key, displayTitle, movies }. Powers the entire Home row set.
"""

import os

import boto3
from boto3.dynamodb.conditions import Key
from ibc_common import build_pk, hydrate, parse_title_ids, respond

_table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

CAROUSEL_PREFIX = "carousel_"

# Preferred display order; anything not listed follows, alphabetically.
_ORDER = {"top_picks": 0, "because_you_watched": 1}


def _sort_key(carousel_key):
    return (_ORDER.get(carousel_key, 99), carousel_key)


def lambda_handler(event, context):
    try:
        params = event.get("queryStringParameters") or {}
        user_id = params.get("user_id")
        profile = params.get("profile")
        if not user_id or not profile:
            return respond(400, {"error": "user_id and profile are required"})

        pk = build_pk(user_id, profile)
        resp = _table.query(
            KeyConditionExpression=Key("user_id").eq(pk)
            & Key("data_type").begins_with(CAROUSEL_PREFIX)
        )
        items = resp.get("Items", [])
        if not items:
            return respond(200, {"carousels": []})

        # Parse each carousel + collect all unique title_ids for one hydrate call.
        parsed = []
        all_ids = []
        for item in items:
            key = item["data_type"][len(CAROUSEL_PREFIX):]
            ids, blurbs = parse_title_ids(item.get("title_ids", []))
            parsed.append({
                "key": key,
                "displayTitle": item.get("display_title", key),
                "ids": ids,
                "blurbs": blurbs,
            })
            all_ids.extend(ids)

        movies_by_id = hydrate(list(dict.fromkeys(all_ids)))

        carousels = []
        for c in sorted(parsed, key=lambda p: _sort_key(p["key"])):
            movies = []
            for tid in c["ids"]:
                base = movies_by_id.get(tid)
                if not base:
                    continue
                blurb = c["blurbs"].get(tid)
                movies.append({**base, "blurb": blurb} if blurb else base)
            if movies:
                carousels.append({
                    "key": c["key"],
                    "displayTitle": c["displayTitle"],
                    "movies": movies,
                })

        return respond(200, {"carousels": carousels})

    except Exception as exc:  # noqa: BLE001 - surface error for the demo
        return respond(500, {"error": str(exc)})
