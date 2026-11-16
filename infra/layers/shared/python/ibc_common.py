"""Shared helpers for the IBC 2026 recommendation Lambdas.

Packaged as a Lambda layer so single-purpose functions stay DRY without
duplicating the OpenSearch hydration + response-normalization logic.

No third-party dependencies: boto3 + botocore are in the Lambda runtime.
"""

import hashlib
import json
import os
import re
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ.get("AWS_REGION", "us-east-1")

# Raw OpenSearch fields we request from the index.
_SOURCE_FIELDS = [
    "title_id", "originalTitle", "image", "imdb_rating",
    "premiere_date", "principalCastMembers", "genres", "plot",
    "mpaa_ratings",
]

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
}

_session = boto3.Session()


def respond(status, payload):
    """Build an API Gateway proxy response with CORS + JSON body."""
    return {
        "statusCode": status,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def build_pk(user_id, profile):
    """Partition key: {user_id}-{profile_slug} (lowercase, spaces -> underscores)."""
    return f"{user_id}-{profile.lower().replace(' ', '_')}"


_PROFILE_ID_RE = re.compile(r"^p-[0-9a-f]{32}$")


def is_profile_id(value):
    """True for an opaque household-member profile_id (see infra/lambdas/household)."""
    return isinstance(value, str) and bool(_PROFILE_ID_RE.match(value))


def caller_sub(event):
    """Cognito `sub` of the caller, from the API Gateway Cognito authorizer."""
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    return claims.get("sub")


def resolve_profile_pk(event, user_id, profile):
    """Profile-table partition key for a request, or None when not allowed.

    A household member's profile_id is used as-is, but only when it belongs to
    the caller's household (identity from the token, never the request). Legacy
    persona slugs (still used by the static Sports Fan card) keep the
    {user_id}-{slug} key.
    """
    if is_profile_id(profile):
        sub = caller_sub(event)
        table = os.environ.get("HOUSEHOLDS_TABLE")
        if not sub or not table:
            return None
        item = _session.resource("dynamodb").Table(table).get_item(
            Key={"account_id": sub, "sk": "MEMBER#" + profile}).get("Item")
        # A removed member (removed_at set) keeps its data but is not usable.
        return profile if item and not item.get("removed_at") else None
    if not user_id or not profile:
        return None
    return build_pk(user_id, profile)


_s3_client = None


def poster_url(image, expires=3600):
    """Browser-loadable poster URL: an s3://bucket/key catalog image is presigned
    (the posters live in the deployment's private media bucket); anything else
    passes through. A failed presign drops the poster rather than the title."""
    global _s3_client
    if not isinstance(image, str) or not image.startswith("s3://"):
        return image
    bucket, _, key = image[5:].partition("/")
    if not bucket or not key:
        return None
    try:
        if _s3_client is None:
            _s3_client = _session.client("s3", region_name=os.environ.get("MEDIA_REGION") or REGION)
        return _s3_client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key},
                                                 ExpiresIn=expires)
    except Exception:  # noqa: BLE001
        return None


def normalize_movie(src, blurb=None):
    """Map a raw OpenSearch document into the stable frontend Movie shape.

    Keeps the API contract independent of the OpenSearch index schema.
    """
    premiere = str(src.get("premiere_date") or "")
    year = int(premiere[:4]) if premiere[:4].isdigit() else None
    maturity = src.get("mpaa_ratings") or []
    movie = {
        "id": src.get("title_id"),
        "title": src.get("originalTitle"),
        "posterUrl": poster_url(src.get("image")),
        "rating": src.get("imdb_rating"),
        "genres": src.get("genres") or [],
        "plot": src.get("plot"),
        "cast": src.get("principalCastMembers") or [],
        "maturity": maturity[0] if isinstance(maturity, list) and maturity else None,
        "year": year,
    }
    if blurb:
        movie["blurb"] = blurb
    return movie


def parse_title_ids(raw):
    """Normalize a stored title_ids list into (ordered_ids, {id: blurb}).

    Items may be plain title_id strings or {title_id, blurb} objects.
    """
    ids = []
    blurbs = {}
    for entry in raw or []:
        if isinstance(entry, dict):
            tid = entry.get("title_id")
            if not tid:
                continue
            ids.append(tid)
            if entry.get("blurb"):
                blurbs[tid] = entry["blurb"]
        else:
            ids.append(entry)
    return ids, blurbs


def _search(body):
    """POST a SigV4-signed _search to the catalog index; returns the parsed response."""
    host = os.environ["OPENSEARCH_HOST"]
    index = os.environ["OPENSEARCH_INDEX"]
    url = f"https://{host}/{index}/_search"
    data = json.dumps(body).encode("utf-8")

    payload_hash = hashlib.sha256(data).hexdigest()
    aws_req = AWSRequest(
        method="POST",
        url=url,
        data=data,
        headers={"Content-Type": "application/json", "X-Amz-Content-SHA256": payload_hash},
    )
    SigV4Auth(_session.get_credentials(), "aoss", REGION).add_auth(aws_req)

    http_req = urllib.request.Request(url, data=data, headers=dict(aws_req.headers), method="POST")
    with urllib.request.urlopen(http_req, timeout=25) as resp:
        return json.loads(resp.read())


def hydrate(title_ids):
    """Fetch + normalize movies for title_ids, preserving input order.

    Returns a dict {title_id: normalized_movie} for the caller to order.
    """
    if not title_ids:
        return {}
    data = _search({
        "_source": _SOURCE_FIELDS,
        "query": {"terms": {"title_id": title_ids}},
        "size": len(title_ids),
    })
    out = {}
    for hit in data.get("hits", {}).get("hits", []):
        src = hit["_source"]
        out[src["title_id"]] = normalize_movie(src)
    return out


def fetch_embeddings(title_ids):
    """{title_id: catalog embedding} for the titles that exist in the catalog."""
    if not title_ids:
        return {}
    data = _search({
        "_source": ["title_id", "embedding"],
        "query": {"terms": {"title_id": list(title_ids)}},
        "size": len(title_ids),
    })
    return {h["_source"]["title_id"]: h["_source"]["embedding"]
            for h in data.get("hits", {}).get("hits", []) if h["_source"].get("embedding")}
