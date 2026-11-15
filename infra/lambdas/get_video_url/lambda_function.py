"""get_video_url — resolve a playable presigned S3 URL for a title_id.

Mirrors the agent's `tools/content_url.py`: looks up the S3 video location for
a title from the `title_video_mapping` DynamoDB table, then returns a short-
lived presigned GET URL the browser can stream directly.

This backs VOD playback for titles that reach the player WITHOUT a content URL
already attached (e.g. Home / top-picks rows, which come from the REST recs API
rather than the agent stream). Titles the agent recommends already carry a
presigned `contentUrl`, so this is the fallback resolver.
"""

import base64
import json
import math
import os
import struct

import boto3
from boto3.dynamodb.conditions import Key
from ibc_common import resolve_profile_pk, respond  # provided by the shared layer

REGION = os.environ.get("AWS_REGION", "us-east-1")
VIDEO_MAPPING_TABLE = os.environ.get("VIDEO_MAPPING_TABLE", "title_video_mapping")
URL_TTL_SECONDS = int(os.environ.get("VIDEO_URL_TTL_SECONDS", "3600"))

# Playback allowlist. Default "*": any title with a row in the video mapping
# table plays, so the table (micro-dramas, plus Tears of Steel when uploaded)
# is the allowlist. Set comma-separated title_ids to restrict it further; the
# micro-dramas always play.
_allowed_raw = os.environ.get("ALLOWED_TITLE_IDS", "*").strip()
ALLOW_ALL = _allowed_raw == "*"
ALLOWED_TITLE_IDS = {t.strip() for t in _allowed_raw.split(",") if t.strip()}

# Highlight reels (the detail-hero preview). `?highlight=1&profile_id=...`
# picks one of the title's clips for that profile: each clip carries a genre
# sentence in its S3 metadata (x-amz-meta-description, written by
# infra/deployment/annotate_highlights.py), the sentence is embedded, and the
# clip whose embedding has the highest cosine with the profile's taste vector
# wins. A title with one clip (a kids show) returns it without scoring. The
# server-side title allowlist keeps the client from naming arbitrary keys.
# Explained with a diagram in docs/how-personalization-works.md §5.5.
# VOD_HIGHLIGHTS (set by stacks.py; Tears of Steel by default) is a JSON object
# {title_id: clip file base name} and upload <base>_<variant>.mp4 under
# HIGHLIGHT_PREFIX{horizontal,vertical}/ to enable the feature for a title.
HIGHLIGHT_BUCKET = os.environ.get("HIGHLIGHT_BUCKET", "")
HIGHLIGHT_PREFIX = os.environ.get("HIGHLIGHT_PREFIX", "clips/highlights/vod/")
VOD_HIGHLIGHTS: dict[str, str] = json.loads(os.environ.get("VOD_HIGHLIGHTS") or "{}")
HIGHLIGHT_ORIENTATIONS = {"horizontal", "vertical"}
PROFILES_TABLE = os.environ.get("PROFILES_TABLE", "")
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "amazon.nova-2-multimodal-embeddings-v1:0")
EMBEDDING_REGION = os.environ.get("EMBEDDING_REGION", "us-east-1")

_dynamodb = boto3.resource("dynamodb", region_name=REGION)
_s3 = boto3.client("s3", region_name=os.environ.get("MEDIA_REGION", REGION))
_bedrock = boto3.client("bedrock-runtime", region_name=EMBEDDING_REGION)
_EMBED_CACHE: dict[str, list[float]] = {}  # description -> unit vector, per warm container


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def embed_description(text: str) -> list[float]:
    """Nova MME text embedding on the document side, like the catalog's titles."""
    if text not in _EMBED_CACHE:
        body = {"taskType": "SINGLE_EMBEDDING", "singleEmbeddingParams": {
            "embeddingPurpose": "GENERIC_INDEX", "embeddingDimension": 1024,
            "text": {"truncationMode": "END", "value": text}}}
        resp = _bedrock.invoke_model(modelId=EMBEDDING_MODEL_ID, body=json.dumps(body),
                                     accept="application/json", contentType="application/json")
        _EMBED_CACHE[text] = _unit(json.loads(resp["body"].read())["embeddings"][0]["embedding"])
    return _EMBED_CACHE[text]


def taste_vector(profile_pk: str) -> list[float] | None:
    """Weight-averaged L2 taste centroids of the profile, or None without any."""
    if not PROFILES_TABLE:
        return None
    item = _dynamodb.Table(PROFILES_TABLE).get_item(
        Key={"user_id": profile_pk, "data_type": "AFFINITIES"}).get("Item") or {}
    total = None
    for c in item.get("centroids") or []:
        try:
            raw = base64.b64decode(c["v"])
            vec = struct.unpack(f"<{len(raw) // 4}f", raw)
            w = float(c.get("w") or 1)
        except Exception:  # noqa: BLE001 -- a malformed centroid is skipped
            continue
        total = [w * x for x in vec] if total is None else [t + w * x for t, x in zip(total, vec)]
    return _unit(total) if total else None


def highlight_clips(base: str, orientation: str) -> list[dict]:
    """The title's clips in one orientation: [{key, description}], sorted by key."""
    prefix = f"{HIGHLIGHT_PREFIX}{orientation}/{base}_"
    resp = _s3.list_objects_v2(Bucket=HIGHLIGHT_BUCKET, Prefix=prefix)
    keys = sorted(o["Key"] for o in resp.get("Contents") or [] if o["Key"].endswith(".mp4"))
    if len(keys) <= 1:
        return [{"key": k, "description": ""} for k in keys]
    return [{"key": k, "description": (_s3.head_object(Bucket=HIGHLIGHT_BUCKET, Key=k).get("Metadata") or {})
             .get("description", "").strip()} for k in keys]


def choose_clip(clips: list[dict], taste: list[float] | None) -> tuple[dict, dict]:
    """Clip with the highest raw cosine between its description and the taste.

    Returns (clip, scores). One clip, no taste, or no descriptions -> the first
    clip unscored; a clip without a description is never preferred."""
    described = [c for c in clips if c["description"]]
    if len(clips) == 1 or not taste or not described:
        return clips[0], {}
    scores = {c["key"]: round(_dot(taste, embed_description(c["description"])), 4) for c in described}
    best = max(described, key=lambda c: scores[c["key"]])
    return best, scores


def highlight_response(event, params, title_id):
    orientation = (params.get("orientation") or "horizontal").strip()
    base = VOD_HIGHLIGHTS.get(title_id)
    if not base or not HIGHLIGHT_BUCKET or orientation not in HIGHLIGHT_ORIENTATIONS:
        return respond(404, {"error": "no highlight for this title"})
    clips = highlight_clips(base, orientation)
    if not clips:
        return respond(404, {"error": "no highlight for this title"})
    taste = None
    profile_id = (params.get("profile_id") or "").strip()
    if profile_id:
        pk = resolve_profile_pk(event, None, profile_id)
        if not pk:
            return respond(403, {"error": "profile not in this household"})
        taste = taste_vector(pk)
    clip, scores = choose_clip(clips, taste)
    name = clip["key"].rsplit("/", 1)[-1]
    print(json.dumps({"highlight": title_id, "profile": profile_id, "picked": name,
                      "scores": {k.rsplit("/", 1)[-1]: v for k, v in scores.items()}}))
    return respond(200, {"url": _presign(f"s3://{HIGHLIGHT_BUCKET}/{clip['key']}"), "clip": name})


def _lookup_s3_url(title_id: str) -> str | None:
    """Return the raw video location (s3://bucket/key or https URL), or None."""
    table = _dynamodb.Table(VIDEO_MAPPING_TABLE)
    resp = table.query(KeyConditionExpression=Key("title_id").eq(title_id), Limit=1)
    items = resp.get("Items") or []
    return items[0].get("video_s3_url") if items else None


def _presign(s3_url: str) -> str | None:
    """Presign an s3://bucket/key URL; pass through already-HTTPS URLs."""
    if not s3_url.startswith("s3://"):
        return s3_url  # already a fetchable URL
    parts = s3_url[5:].split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""
    if not key:
        return None
    return _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=URL_TTL_SECONDS,
    )


def lambda_handler(event, context):
    try:
        params = event.get("queryStringParameters") or {}
        title_id = (params.get("title_id") or "").strip()
        if not title_id:
            return respond(400, {"error": "title_id is required"})

        if (params.get("highlight") or "").strip():
            return highlight_response(event, params, title_id)

        # Titles outside the allowlist get no video; with the default "*" the
        # mapping lookup below decides (see ALLOWED_TITLE_IDS).
        if not ALLOW_ALL and title_id not in ALLOWED_TITLE_IDS and not title_id.startswith("md"):
            return respond(404, {"error": "no video available for this title"})

        s3_url = _lookup_s3_url(title_id)
        if not s3_url:
            return respond(404, {"error": "no video mapping for this title"})

        url = _presign(s3_url)
        if not url:
            return respond(404, {"error": "could not resolve a playable URL"})

        return respond(200, {"url": url})

    except Exception as exc:  # noqa: BLE001 - surface error for the demo
        return respond(500, {"error": str(exc)})
