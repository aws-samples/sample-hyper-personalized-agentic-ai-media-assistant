"""remove_from_list — remove a title from a profile's My List (watch_list).

POST body: { user_id, profile, title_id }. Removes the title_id from the
profile's `watch_list` if present (idempotent — a missing title is a no-op).
Returns the updated id list.
"""

import json
import os

import boto3

from ibc_common import resolve_profile_pk, respond

_table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])

DATA_TYPE = "watch_list"


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
        user_id = body.get("user_id")
        profile = body.get("profile")
        title_id = body.get("title_id")
        if not all([profile, title_id]):
            return respond(400, {"error": "profile and title_id are required"})

        pk = resolve_profile_pk(event, user_id, profile)
        if not pk:
            return respond(404, {"error": "profile not found"})
        key = {"user_id": pk, "data_type": DATA_TYPE}

        item = _table.get_item(Key=key).get("Item") or {}
        watch_list = item.get("watch_list", [])

        if title_id in watch_list:
            watch_list = [t for t in watch_list if t != title_id]
            _table.update_item(
                Key=key,
                UpdateExpression="SET watch_list = :wl",
                ExpressionAttributeValues={":wl": watch_list},
            )

        return respond(200, {"title_ids": watch_list})

    except Exception as exc:  # noqa: BLE001 - surface error for the demo
        return respond(500, {"error": str(exc)})
