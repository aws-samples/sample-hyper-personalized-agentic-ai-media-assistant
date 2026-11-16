"""get_top_picks — a profile's top-pick movies, hydrated + normalized.

Reads the per-profile top_picks list from the existing DynamoDB table
(populated by the agent) and hydrates each title_id into the normalized Movie
shape via the shared layer. Response contract is defined by `normalize_movie`
in the layer, not the OpenSearch schema.
"""

import os

import boto3
from ibc_common import build_pk, hydrate, respond  # provided by the shared layer

DATA_TYPE = "top_picks"

_table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


def lambda_handler(event, context):
    try:
        params = event.get("queryStringParameters") or {}
        user_id = params.get("user_id")
        profile = params.get("profile")
        if not user_id or not profile:
            return respond(400, {"error": "user_id and profile are required"})

        item = _table.get_item(
            Key={"user_id": build_pk(user_id, profile), "data_type": DATA_TYPE}
        ).get("Item", {})
        title_ids = item.get("top_picks", [])
        if not title_ids:
            return respond(200, {"movies": []})

        movies_by_id = hydrate(title_ids)
        movies = [movies_by_id[t] for t in title_ids if t in movies_by_id]
        return respond(200, {"movies": movies})

    except Exception as exc:  # noqa: BLE001 - surface error for the demo
        return respond(500, {"error": str(exc)})
