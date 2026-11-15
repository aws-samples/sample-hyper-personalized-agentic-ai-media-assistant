"""get_list — a profile's "My List" (watch_list), hydrated to full movies.

Reads the single `watch_list` item for the profile (a list of title_ids),
hydrates them in one OpenSearch request, and returns them in stored order as
{ movies: [...] }. Mirrors get_carousels' hydrate path.
"""

import os

import boto3

from ibc_common import resolve_profile_pk, hydrate, parse_title_ids, respond

_table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

DATA_TYPE = "watch_list"


def lambda_handler(event, context):
    try:
        params = event.get("queryStringParameters") or {}
        user_id = params.get("user_id")
        profile = params.get("profile")
        if not profile:
            return respond(400, {"error": "profile is required"})

        pk = resolve_profile_pk(event, user_id, profile)
        if not pk:
            return respond(404, {"error": "profile not found"})
        resp = _table.get_item(Key={"user_id": pk, "data_type": DATA_TYPE})
        item = resp.get("Item") or {}
        ids, blurbs = parse_title_ids(item.get("watch_list", []))
        if not ids:
            return respond(200, {"movies": []})

        movies_by_id = hydrate(list(dict.fromkeys(ids)))

        movies = []
        for tid in ids:
            base = movies_by_id.get(tid)
            if not base:
                continue
            blurb = blurbs.get(tid)
            movies.append({**base, "blurb": blurb} if blurb else base)

        return respond(200, {"movies": movies})

    except Exception as exc:  # noqa: BLE001 - surface error for the demo
        return respond(500, {"error": str(exc)})
