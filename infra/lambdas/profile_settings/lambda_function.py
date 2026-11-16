"""profile_settings: GET / PUT /profile/declared?profile_id=<id> (design §12).

The only writer of a member's L3 (`L3#DECLARED`) after the household seed.

Identity comes from the Cognito authorizer's `sub`, never from the request:
the profile must be a member of the caller's household (404 otherwise).

GET  -> {profile_id, version, updated_at, updated_by, declared}. A member with
        no L3 item gets the defaults at version 0.
PUT  <- {version, source?, request_id?, declared}. Replaces every editable
        field (all are required). Conditional on `version` (optimistic lock):
        a stale version gets 409 with the current state, so two editors can
        never overwrite each other. `request_id` makes a retried PUT whose
        first attempt succeeded return 200 instead of 409.
        Booth-mode callers get 403: shared demo personas keep their rules.

GET /profile/entities?q=<text>&kind=<teams|players|drivers>
     -> {entities: [{id, name, kind, sport, team, number}]}: type-ahead over
        the sports registry for the onboarding follow input. Needs a signed-in
        caller but no profile; the answer is the same for everyone.

After a write, a `settings_changed` event (field names and the new version,
never values) goes to the profile-events stream for the S3 event log and any
downstream consumer. Readers do not depend on it: the agent and the event
ingest re-read L3 within a minute.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

import settings_core as core
from entity_resolver import KIND_BY_TYPE, SPORTS_KINDS, Registry
from ibc_common import caller_sub, is_profile_id, respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_BODY_BYTES = 32 * 1024
MAX_QUERY_CHARS = 64
GENRES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "genres.json")
CORS = {"Access-Control-Allow-Methods": "GET,PUT,OPTIONS"}


def _respond(status, payload):
    r = respond(status, payload)
    r["headers"].update(CORS)
    return r


def _claims(event):
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}


def is_booth(claims):
    groups = claims.get("cognito:groups") or ""
    if isinstance(groups, list):
        return "booth" in groups
    return "booth" in str(groups).strip("[]").replace(",", " ").split()


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _native(v):
    from decimal import Decimal
    if isinstance(v, Decimal):
        return int(v) if v == int(v) else float(v)
    if isinstance(v, list):
        return [_native(x) for x in v]
    if isinstance(v, dict):
        return {k: _native(x) for k, x in v.items()}
    return v


def load_genres(path=GENRES_FILE):
    """[{id, label}] from the static genre list, ids lowercase."""
    with open(path, encoding="utf-8") as f:
        return [{"id": g["id"].strip().lower(), "label": g["label"]} for g in json.load(f)["genres"]]


GENRES = load_genres()
GENRE_VOCAB = frozenset(g["id"] for g in GENRES)
GENRE_LABELS = {g["id"]: g["label"] for g in GENRES}
# Followed teams/players/drivers -> canonical ids (entity_resolver.py), loaded once.
ENTITIES = Registry.load()


class Conflict(Exception):
    """The stored version is not the one the caller read."""


class Store:
    """All AWS access. Tests replace the module-level instance."""

    def __init__(self):
        ddb = boto3.resource("dynamodb")
        self._households = ddb.Table(os.environ["HOUSEHOLDS_TABLE"])
        self._profiles = ddb.Table(os.environ["PROFILES_TABLE"])
        self._kinesis = boto3.client("kinesis")

    def is_member(self, account_id, profile_id):
        item = self._households.get_item(
            Key={"account_id": account_id, "sk": "MEMBER#" + profile_id},
            ProjectionExpression="profile_id, removed_at").get("Item")
        return bool(item) and not item.get("removed_at")  # removed members are not editable

    def get(self, profile_id):
        item = self._profiles.get_item(Key={"user_id": profile_id, "data_type": core.DECLARED_SK},
                                       ConsistentRead=True).get("Item")
        return _native(item) if item else None

    def put(self, item, expected_version):
        from decimal import Decimal
        kwargs = {"Item": json.loads(json.dumps(item), parse_float=Decimal)}
        if expected_version == 0:
            kwargs["ConditionExpression"] = "attribute_not_exists(user_id) OR attribute_not_exists(version)"
        else:
            kwargs["ConditionExpression"] = "version = :v"
            kwargs["ExpressionAttributeValues"] = {":v": expected_version}
        try:
            self._profiles.put_item(**kwargs)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise Conflict() from e
            raise

    def genre_vocab(self):
        """Genre ids a viewer may exclude, from the static `genres.json`
        shipped with this function (no catalog query)."""
        return GENRE_VOCAB

    def emit_changed(self, account_id, profile_id, version, fields, source):
        record = {"event_id": str(uuid.uuid4()), "schema_version": 1, "event_type": "settings_changed",
                  "profile_id": profile_id, "account_id": account_id, "server_ts": _now_iso(),
                  "producer": "profile_settings",
                  "payload": {"version": version, "fields": fields, "updated_by": source}}
        self._kinesis.put_record(StreamName=os.environ["PROFILE_EVENTS_STREAM"],
                                 Data=json.dumps(record).encode(), PartitionKey=profile_id)

    def emit_loved(self, account_id, profile_id, genres):
        """Server-side `genres_loved` notice: the stream processor gives L2 a weak,
        fading start for each genre. Clients cannot send this type to /events."""
        record = {"event_id": str(uuid.uuid4()), "schema_version": 1, "event_type": "genres_loved",
                  "profile_id": profile_id, "account_id": account_id, "server_ts": _now_iso(),
                  "producer": "profile_settings",
                  "payload": {"genres": [{"id": g, "label": GENRE_LABELS.get(g, g)} for g in genres]}}
        self._kinesis.put_record(StreamName=os.environ["PROFILE_EVENTS_STREAM"],
                                 Data=json.dumps(record).encode(), PartitionKey=profile_id)


_store = None


def get_store():
    global _store
    if _store is None:
        _store = Store()
    return _store


def _resolved(view):
    """Entries saved before the resolver existed (or before the registry knew
    them) are resolved when read; the stored item changes on the next save."""
    view["declared"]["followed"] = ENTITIES.resolve_followed(view["declared"]["followed"])
    return view


def get_declared(store, profile_id):
    return {"profile_id": profile_id, **_resolved(core.from_item(store.get(profile_id))),
            "options": {"genres": GENRES}}


def _send_loved(store, account_id, profile_id, loved):
    if not loved:
        return
    try:
        store.emit_loved(account_id, profile_id, loved)
    except Exception:  # noqa: BLE001 -- a lost head start only costs a few views of learning
        logger.exception("genres_loved emit failed")


def put_declared(store, account_id, profile_id, body):
    """Returns (status, payload)."""
    expected, source, request_id, declared = core.parse_put(body)
    new = core.validate(declared, store.genre_vocab())
    new["followed"] = ENTITIES.resolve_followed(new["followed"])
    loved = core.parse_loved(body, store.genre_vocab(), new["excluded_genres"])
    existing = store.get(profile_id)
    current = core.from_item(existing)
    if current["version"] != expected:
        if core.is_retry_of_last_write(existing, expected, request_id):
            return 200, {"profile_id": profile_id, **current}
        return 409, {"error": "version_conflict", "current": {"profile_id": profile_id, **current}}
    fields = core.changed_fields(current, new)
    if not fields:
        _send_loved(store, account_id, profile_id, loved)
        return 200, {"profile_id": profile_id, **current}   # nothing to write
    version = expected + 1
    item = core.build_item(profile_id, existing, new, version, source, request_id, _now_iso())
    try:
        store.put(item, expected)
    except Conflict:
        latest = core.from_item(store.get(profile_id))
        return 409, {"error": "version_conflict", "current": {"profile_id": profile_id, **latest}}
    logger.info(json.dumps({"event": "declared_updated", "profile_id": profile_id, "version": version,
                            "fields": fields, "updated_by": source}))
    try:
        store.emit_changed(account_id, profile_id, version, fields, source)
    except Exception:  # noqa: BLE001 -- the write is committed; readers re-read L3 on their own
        logger.exception("settings_changed emit failed")
    _send_loved(store, account_id, profile_id, loved)
    return 200, get_declared(store, profile_id)


def suggest_entities(params):
    """GET /profile/entities?q=&kind=: type-ahead over the sports registry.
    Read-only and the same for every caller, so it needs no profile."""
    q = (params.get("q") or "")[:MAX_QUERY_CHARS]
    kind = params.get("kind") if params.get("kind") in SPORTS_KINDS else None
    return {"entities": [{"id": e["id"], "name": e["name"], "kind": KIND_BY_TYPE[e["type"]],
                          "sport": e["sport"], "team": ENTITIES.by_id[e["team"]]["name"] if e.get("team") else None,
                          "number": e.get("number")}
                         for e in ENTITIES.suggest(q, kind)]}


def lambda_handler(event, context):
    account_id = caller_sub(event)
    if not account_id:
        return _respond(401, {"error": "unauthenticated"})
    method = event.get("httpMethod") or ""
    params = event.get("queryStringParameters") or {}
    if (event.get("resource") or event.get("path") or "").rstrip("/").endswith("/entities"):
        if method != "GET":
            return _respond(405, {"error": "method not allowed"})
        return _respond(200, suggest_entities(params))
    profile_id = params.get("profile_id")
    if not is_profile_id(profile_id):
        return _respond(400, {"error": "profile_id is required"})
    try:
        store = get_store()
        if not store.is_member(account_id, profile_id):
            return _respond(404, {"error": "profile not found"})
        if method == "GET":
            return _respond(200, get_declared(store, profile_id))
        if method == "PUT":
            if is_booth(_claims(event)):
                return _respond(403, {"error": "settings are read-only in booth mode"})
            raw = event.get("body") or ""
            if len(raw) > MAX_BODY_BYTES:
                return _respond(413, {"error": "body too large"})
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                return _respond(400, {"error": "invalid", "details": [{"field": "body", "reason": "not JSON"}]})
            status, payload = put_declared(store, account_id, profile_id, body)
            return _respond(status, payload)
        return _respond(405, {"error": "method not allowed"})
    except core.Invalid as e:
        return _respond(400, {"error": "invalid", "details": e.details})
    except Exception:  # noqa: BLE001
        logger.exception("profile settings request failed")
        return _respond(500, {"error": "internal error"})
