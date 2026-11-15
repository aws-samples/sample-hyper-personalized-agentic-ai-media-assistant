"""match_state — playhead-bounded live match state for the sports player.

One Lambda, four GET routes (mirrors the sports-agent's local service.py):

    GET /state?video_s=       -> scoreboard + team/player stats at the playhead
    GET /meta                 -> teams, clock hz, per-half video_s ranges (UI setup)
    GET /highlights           -> full-match goal list (jump-to-goal rail)
    GET /reel?video_s=&types= -> merged "catch me up" highlight segments up to now

Every /state and /reel query is bounded by the caller's `video_s` (the VOD
playhead, in seconds) -> frame_id, so the response only ever reflects events up
to the current moment on screen (spoiler-safe).

Facts come from the DynamoDB event store (`<prefix>-events`): the match
structure from the match's META item and the events queried bounded by
event_time <= playhead. No feed file is bundled or read.
"""

import json
import os

import boto3

from match_state import DEFAULT_MATCH_ID, load_dynamodb
import build_reel

EVENTS_TABLE = os.environ.get("EVENTS_TABLE", "hpm-demo-events")
MATCH_ID = os.environ.get("MATCH_ID", DEFAULT_MATCH_ID)

# Optional player-face avatars (circle crops) in the deployer's own bucket,
# keyed by the roster's face_id: <FACES_PREFIX><face_id>-circle.png. The demo
# ships no faces, so with FACES_BUCKET unset every player card falls back to a
# team-color initial. Presigned per request; requires s3:GetObject on the prefix.
FACES_BUCKET = os.environ.get("FACES_BUCKET", "").strip()
FACES_PREFIX = os.environ.get("FACES_PREFIX", "sports/faces/")
FACE_URL_TTL_S = int(os.environ.get("FACE_URL_TTL_S", "3600"))
# Key-moment thumbnails: small JPG stills cut from the match clip by
# backend/live-sports-agent/cut_highlight_thumbs.py and served with the
# frontend as <SOCCER_THUMB_BASE_URL><frame_id>.jpg. Only moments inside the
# broadcast clip (highlights with a `clip` window) have a still.
SOCCER_THUMB_BASE_URL = os.environ.get("SOCCER_THUMB_BASE_URL", "/sports/highlights/horizontal/")
_s3 = boto3.client("s3", region_name=os.environ.get("MEDIA_REGION", os.environ.get("AWS_REGION", "us-east-1"))) \
    if FACES_BUCKET else None

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,OPTIONS",
}

# Built once per container from the table's META item. A table that has not
# been loaded fails here, so the error shows up in the first request's logs.
STATE = load_dynamodb(EVENTS_TABLE, MATCH_ID, boto3.Session())


def _respond(status, payload):
    return {
        "statusCode": status,
        "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
        # ensure_ascii=False keeps non-ASCII names intact (UTF-8 body).
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _resolve_frame(params):
    """Playhead frame from either `frame` or `video_s` (seconds)."""
    if params.get("frame") is not None:
        return int(params["frame"])
    if params.get("video_s") is not None:
        return STATE.frame_for_video_s(float(params["video_s"]))
    raise ValueError("provide either video_s or frame")


def _meta():
    halves = {}
    for name, h in STATE.halves.items():
        start = h["anchor_video_s"]
        end = round(start + (h["last_frame_id"] - h["first_frame_id"]) / STATE.hz, 2)
        halves[name] = {
            "start_video_s": start,
            "end_video_s": end,
            "first_frame_id": h["first_frame_id"],
        }
    return {"teams": STATE.teams, "hz": STATE.hz, "halves": halves}


def _attach_face_urls(players):
    """Fill each player's `face_url` with a presigned circle-avatar URL when a
    faces bucket is configured and the player has a face id. Everyone else keeps
    face_url=None so the UI falls back to a team-color initial. Presign failures
    are non-fatal."""
    for p in players or []:
        fid = p.get("face_id")
        p["face_url"] = None
        if not fid or _s3 is None:
            continue
        try:
            p["face_url"] = _s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": FACES_BUCKET, "Key": f"{FACES_PREFIX}{fid}-circle.png"},
                ExpiresIn=FACE_URL_TTL_S,
            )
        except Exception as exc:  # noqa: BLE001 - a missing face must not break /state
            print(f"[match_state] face presign failed for {fid}: {exc}")


def _attach_thumbnail_urls(highlights):
    """Fill each highlight's `thumbnail_url` with its static still
    (<SOCCER_THUMB_BASE_URL><frame_id>.jpg). Only moments inside the broadcast
    clip have one; the rest (clip=None, before the broadcast started) keep
    thumbnail_url=None and the UI shows a gradient poster."""
    for h in highlights or []:
        fid = h.get("frame_id")
        h["thumbnail_url"] = (f"{SOCCER_THUMB_BASE_URL}{fid}.jpg"
                              if SOCCER_THUMB_BASE_URL and h.get("clip") and fid is not None else None)


def _route(event):
    # REST proxy integration exposes the matched resource path (e.g. "/state").
    path = event.get("resource") or event.get("path") or ""
    return path.rstrip("/").rsplit("/", 1)[-1]


def lambda_handler(event, context):
    params = event.get("queryStringParameters") or {}
    route = _route(event)
    try:
        if route == "state":
            snap = STATE.at(_resolve_frame(params))
            _attach_face_urls(snap.get("players"))
            return _respond(200, snap)

        if route == "meta":
            return _respond(200, _meta())

        if route == "highlights":
            # Full-match goal list for the jump-to-goal rail (booth intentionally
            # exposes the whole list; ordering is chronological).
            end = STATE.halves["secondHalf"]["last_frame_id"]
            hls = STATE.at(end)["highlights"]
            _attach_thumbnail_urls(hls)
            return _respond(200, {"highlights": hls})

        if route == "reel":
            frame = _resolve_frame(params)
            types = set((params.get("types") or "goal,save,shot,penalty").split(","))
            min_xg = float(params["min_xg"]) if params.get("min_xg") else None
            merge_gap_s = float(params.get("merge_gap_s", 2.0))
            return _respond(
                200,
                build_reel.build(STATE, frame, types, min_xg, merge_gap_s),
            )

        return _respond(404, {"error": f"unknown route: {route}"})

    except ValueError as exc:
        return _respond(422, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - surface error for the demo
        return _respond(500, {"error": str(exc)})
