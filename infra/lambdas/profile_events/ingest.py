"""POST /events: validate client events and put them on the profile-events stream.

docs/unified-profile-design.md §7.2. Identity comes from the Cognito
authorizer (`sub` claim), never from the body:
- each event's `profile_id` must be a member of the caller's household,
  otherwise the event is rejected;
- events of a member whose consent `personalization_from_history` is false
  are dropped (accepted, not stored);
- `server_ts`, `account_id` and `booth` are set here.

Response: {"accepted": n, "dropped": n, "rejected": [{"index", "reason"}]}.
A batch with any accepted event returns 200; partial rejection is reported,
not retried, because a malformed event will not become valid on retry.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3

from contract import MAX_BATCH, validate

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
}
MAX_BODY_BYTES = 64 * 1024
CACHE_TTL_S = 60

_ddb = None
_kinesis = None
_cache: dict = {}   # (account_id, profile_id) -> (expires, member: bool, consent: bool)


def _clients():
    global _ddb, _kinesis
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
        _kinesis = boto3.client("kinesis")
    return _ddb, _kinesis


def respond(status, payload):
    return {"statusCode": status, "headers": {**CORS_HEADERS, "Content-Type": "application/json"},
            "body": json.dumps(payload)}


def _claims(event):
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}


def is_booth(claims):
    groups = claims.get("cognito:groups") or ""
    if isinstance(groups, list):
        return "booth" in groups
    return "booth" in [g.strip() for g in str(groups).strip("[]").replace(",", " ").split()]


def member_and_consent(account_id, profile_id):
    """(is_member, consents) for a profile, cached per container for a minute."""
    key = (account_id, profile_id)
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1], hit[2]
    ddb, _ = _clients()
    item = ddb.Table(os.environ["HOUSEHOLDS_TABLE"]).get_item(
        Key={"account_id": account_id, "sk": "MEMBER#" + profile_id}).get("Item")
    member = bool(item) and not item.get("removed_at")  # a removed member records nothing
    consent = True
    if member:
        declared = ddb.Table(os.environ["PROFILES_TABLE"]).get_item(
            Key={"user_id": profile_id, "data_type": "L3#DECLARED"},
            ProjectionExpression="consent").get("Item") or {}
        consent = (declared.get("consent") or {}).get("personalization_from_history", True) is not False
    _cache[key] = (time.time() + CACHE_TTL_S, member, consent)
    return member, consent


def stamp(event, account_id, booth, now=None):
    now = now or datetime.now(timezone.utc)
    return {**event, "account_id": account_id, "booth": booth,
            "server_ts": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"}


def process(body, account_id, booth, lookup=member_and_consent):
    """Split a request body into (records_to_put, dropped, rejected)."""
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list) or not events:
        raise ValueError("body must be {\"events\": [...]}")
    if len(events) > MAX_BATCH:
        raise ValueError(f"at most {MAX_BATCH} events per request")
    records, dropped, rejected = [], 0, []
    for i, ev in enumerate(events):
        reason = validate(ev)
        if reason:
            rejected.append({"index": i, "reason": reason})
            continue
        member, consent = lookup(account_id, ev["profile_id"])
        if not member:
            rejected.append({"index": i, "reason": "profile_id not in household"})
            continue
        if not consent:
            dropped += 1
            continue
        records.append(stamp(ev, account_id, booth))
    return records, dropped, rejected


def put_records(records):
    """PutRecords with one retry of throttled entries; returns how many failed."""
    _, kinesis = _clients()
    pending = records
    for _attempt in range(2):
        if not pending:
            return 0
        resp = kinesis.put_records(StreamName=os.environ["PROFILE_EVENTS_STREAM"], Records=[
            {"Data": json.dumps(r).encode(), "PartitionKey": r["profile_id"]} for r in pending])
        if not resp.get("FailedRecordCount"):
            return 0
        pending = [r for r, res in zip(pending, resp["Records"]) if res.get("ErrorCode")]
    return len(pending)


def lambda_handler(event, context):
    account_id = _claims(event).get("sub")
    if not account_id:
        return respond(401, {"error": "unauthenticated"})
    raw = event.get("body") or ""
    if len(raw) > MAX_BODY_BYTES:
        return respond(413, {"error": "body too large"})
    try:
        body = json.loads(raw)
        records, dropped, rejected = process(body, account_id, is_booth(_claims(event)))
    except (ValueError, json.JSONDecodeError) as e:
        return respond(400, {"error": str(e)})
    try:
        failed = put_records(records)
    except Exception:  # noqa: BLE001
        logger.exception("put_records failed")
        return respond(503, {"error": "event stream unavailable"})
    if failed:
        logger.warning(json.dumps({"event": "put_records_partial", "failed": failed}))
    if rejected:
        logger.info(json.dumps({"event": "events_rejected", "count": len(rejected),
                                "reasons": sorted({r["reason"] for r in rejected})}))
    return respond(200, {"accepted": len(records) - failed, "dropped": dropped, "rejected": rejected})
