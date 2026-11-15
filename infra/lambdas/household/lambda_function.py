"""household: GET /household and GET /home for the signed-in Cognito user.

Identity always comes from the Cognito authorizer's `sub` claim, never from
the query string, so a caller can only see their own household and rows.

GET /household
    Returns the caller's members. The first call for a new account creates
    the default household from the committed seed, in a single DynamoDB
    transaction guarded by a HOUSEHOLD marker, so concurrent first calls
    cannot create two households. A household created from an older seed
    version gets the seed members it is missing (e.g. Sports Fan in v2) on
    its next call, once, within the member limit.

GET /home?profile_id=<id>
    Returns {profileId, hero, rails} for one member of the caller's household,
    read from the recommendation cache and filled in from the catalog. A
    profile_id outside the caller's household gets 404. A member with no
    seeded or Personalize rows (an added profile) gets cold-start rows built
    from its liked genres and L3 limits (cold_start.py), stored and reused
    until its inputs change or they expire.
    HOME_RAILS_SOURCE=personalize serves a member's P13N# rows (written by
    infra/deployment/personalize_rails.py) ahead of all of those, and
    HOME_RAILS_SOURCE=connect its CONN# rows (infra/deployment/connect_rails.py);
    the default, `seed`, ignores both, so reverting is a config change.
    `&part=options` instead returns {profileId, tappableOptions}: the
    idle-search starter texts (tappable.py), without building or hydrating
    rows -- the Search screen's call.

POST /household/members  {display_name, kids_profile?}
    Adds a member (201 with its view). Names are unique per household and a
    household holds at most core.MAX_MEMBERS. The marker's member_seq is an
    optimistic lock, so concurrent adds cannot break either rule (409).
    Booth sessions get 403.

POST /household/members/{profile_id}/remove
    Removes a member, softly: only `removed_at` is set on the member item, so
    its settings, taste, history and My List are kept for a later restore. A
    removed member leaves the picker and the member limit, and every API that
    checks membership treats it as absent. The last profile cannot be removed.

POST /household/members/{profile_id}/restore
    Brings a removed member back with all its data, within the member limit
    and if no active profile has taken its name (409 otherwise).
    GET /household lists removed members under `removed` for this.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import cold_start
import household_core as core
import tappable
from ibc_common import _search as catalog_search
from ibc_common import fetch_embeddings, hydrate, respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_ENV = {"households": "HOUSEHOLDS_TABLE", "profiles": "PROFILES_TABLE",
             "cache": "REC_CACHE_TABLE", "interactions": "INTERACTIONS_TABLE"}


class AlreadyProvisioned(Exception):
    """Another request created this account's household first."""


class Store:
    """All DynamoDB and S3 access. Tests replace the module-level instance."""

    def __init__(self):
        self._dynamodb = boto3.resource("dynamodb")
        self._client = self._dynamodb.meta.client
        self._names = {k: os.environ[v] for k, v in TABLE_ENV.items()}
        self._seed = None

    def _table(self, key):
        return self._dynamodb.Table(self._names[key])

    def members(self, account_id):
        resp = self._table("households").query(ConsistentRead=True,
            KeyConditionExpression=Key("account_id").eq(account_id) & Key("sk").begins_with(core.MEMBER_PREFIX))
        return resp.get("Items", [])

    def member(self, account_id, profile_id):
        resp = self._table("households").get_item(
            Key={"account_id": account_id, "sk": core.MEMBER_PREFIX + profile_id})
        return resp.get("Item")

    def rows(self, profile_id):
        resp = self._table("cache").query(KeyConditionExpression=Key("profile_id").eq(profile_id))
        return resp.get("Items", [])

    def taste(self, profile_id):
        """(L3#DECLARED, AFFINITIES) items, each {} when absent."""
        resp = self._dynamodb.batch_get_item(RequestItems={self._names["profiles"]: {"Keys": [
            {"user_id": profile_id, "data_type": t} for t in ("L3#DECLARED", "AFFINITIES")]}})
        found = {i["data_type"]: i for i in resp.get("Responses", {}).get(self._names["profiles"], [])}
        return found.get("L3#DECLARED") or {}, found.get("AFFINITIES") or {}

    def backfill_options(self, profile_id, version, options):
        """Store options for a taste written before options existed. Conditional on
        the taste version read, so a concurrent processor update always wins."""
        try:
            self._table("profiles").update_item(
                Key={"user_id": profile_id, "data_type": "AFFINITIES"},
                UpdateExpression="SET tappable_options = :o",
                ConditionExpression="version = :v AND attribute_not_exists(tappable_options)",
                ExpressionAttributeValues={":o": core.to_dynamo(options), ":v": version})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def write_rows(self, rows, stale_sks):
        """Replace a member's cold-start rows: put the new ones, delete the dropped ones."""
        with self._table("cache").batch_writer() as batch:
            for row in rows:
                batch.put_item(Item=core.to_dynamo(row))
            for sk in stale_sks:
                batch.delete_item(Key={"profile_id": rows[0]["profile_id"], "sk": sk})

    def seed(self):
        if self._seed is None:
            obj = boto3.client("s3").get_object(Bucket=os.environ["SEED_BUCKET"], Key=os.environ["SEED_KEY"])
            self._seed = json.loads(obj["Body"].read())
        return self._seed

    def provision(self, items_by_table):
        # The resource-level client serializes plain Python values itself;
        # passing pre-serialized {"S": ...} attributes would double-encode keys.
        writes = []
        for key, items in items_by_table.items():
            for item in items:
                put = {"TableName": self._names[key], "Item": item}
                if key == "households" and item["sk"] == core.MARKER_SK:
                    put["ConditionExpression"] = "attribute_not_exists(account_id)"
                writes.append({"Put": put})
        # Marker first, so a lost race is identified by cancellation reason 0.
        writes.sort(key=lambda w: 0 if "ConditionExpression" in w["Put"] else 1)
        try:
            self._client.transact_write_items(TransactItems=writes)
        except ClientError as exc:
            reasons = exc.response.get("CancellationReasons") or []
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                raise AlreadyProvisioned() from exc
            raise

    def marker(self, account_id):
        resp = self._table("households").get_item(
            Key={"account_id": account_id, "sk": core.MARKER_SK}, ConsistentRead=True)
        return resp.get("Item")

    def upgrade_seed(self, account_id, seen_seq, seen_version, version, items_by_table):
        """Add seed members to an existing household and record the new seed
        version, atomically. member_seq is the same optimistic lock add_member
        uses, so an upgrade and a concurrent add cannot both pass the member
        limit; seed_version stops a second concurrent upgrade adding twice."""
        cond_seq = "member_seq = :seen" if seen_seq else "attribute_not_exists(member_seq)"
        cond_ver = "seed_version = :seenv" if seen_version is not None else "attribute_not_exists(seed_version)"
        values = {":next": seen_seq + 1, ":v": version}
        if seen_seq:
            values[":seen"] = seen_seq
        if seen_version is not None:
            values[":seenv"] = seen_version
        writes = [{"Update": {
            "TableName": self._names["households"], "Key": {"account_id": account_id, "sk": core.MARKER_SK},
            "UpdateExpression": "SET member_seq = :next, seed_version = :v",
            "ConditionExpression": f"attribute_exists(account_id) AND {cond_seq} AND {cond_ver}",
            "ExpressionAttributeValues": values}}]
        for key, items in items_by_table.items():
            for item in items:
                put = {"TableName": self._names[key], "Item": item}
                if key == "households":
                    put["ConditionExpression"] = "attribute_not_exists(account_id)"
                writes.append({"Put": put})
        try:
            self._client.transact_write_items(TransactItems=writes)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                return False  # someone else changed the household first; it is read as is
            raise

    def add_member(self, account_id, seen_seq, items):
        """Write one new member atomically. The marker's member_seq is an optimistic
        lock: two concurrent adds cannot both pass the member limit or the name check."""
        cond_seq = "member_seq = :seen" if seen_seq else "attribute_not_exists(member_seq)"
        writes = [{"Update": {
            "TableName": self._names["households"], "Key": {"account_id": account_id, "sk": core.MARKER_SK},
            "UpdateExpression": "SET member_seq = :next",
            "ConditionExpression": f"attribute_exists(account_id) AND {cond_seq}",
            "ExpressionAttributeValues": {":next": seen_seq + 1, **({":seen": seen_seq} if seen_seq else {})}}},
            {"Put": {"TableName": self._names["households"], "Item": items["member"],
                     "ConditionExpression": "attribute_not_exists(account_id)"}}]
        writes += [{"Put": {"TableName": self._names["profiles"], "Item": p}} for p in items["profiles"]]
        try:
            self._client.transact_write_items(TransactItems=writes)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise core.MemberError(409, "the household changed; try again") from exc
            raise

    def set_removed(self, account_id, seen_seq, profile_id, removed_at):
        """Remove (removed_at = timestamp) or restore (removed_at = None) one member.

        Only the member item's `removed_at` changes. The marker's member_seq is
        the same optimistic lock add_member uses, so a restore and a concurrent
        add cannot both pass the member limit, and two removes cannot leave the
        household with no profile."""
        cond_seq = "member_seq = :seen" if seen_seq else "attribute_not_exists(member_seq)"
        key = {"account_id": account_id, "sk": core.MEMBER_PREFIX + profile_id}
        if removed_at:
            member = {"UpdateExpression": "SET removed_at = :t", "ExpressionAttributeValues": {":t": removed_at},
                      "ConditionExpression": "attribute_exists(account_id) AND attribute_not_exists(removed_at)"}
        else:
            member = {"UpdateExpression": "REMOVE removed_at",
                      "ConditionExpression": "attribute_exists(account_id) AND attribute_exists(removed_at)"}
        writes = [{"Update": {
            "TableName": self._names["households"], "Key": {"account_id": account_id, "sk": core.MARKER_SK},
            "UpdateExpression": "SET member_seq = :next",
            "ConditionExpression": f"attribute_exists(account_id) AND {cond_seq}",
            "ExpressionAttributeValues": {":next": seen_seq + 1, **({":seen": seen_seq} if seen_seq else {})}}},
            {"Update": {"TableName": self._names["households"], "Key": key, **member}}]
        try:
            self._client.transact_write_items(TransactItems=writes)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise core.MemberError(409, "the household changed; try again") from exc
            raise


_store = None


def get_store():
    global _store
    if _store is None:
        _store = Store()
    return _store


def _claims(event):
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}


def _caller(event):
    return _claims(event).get("sub")


def is_booth(claims):
    """Booth (shared demo) sessions cannot change the household. Same check as profile_settings."""
    groups = claims.get("cognito:groups") or ""
    if isinstance(groups, list):
        return "booth" in groups
    return "booth" in str(groups).strip("[]").replace(",", " ").split()


def _embeddings(title_ids):
    try:
        return fetch_embeddings(title_ids)
    except Exception:  # noqa: BLE001 -- centroids are optional; the agent derives them if absent
        logger.exception("embedding fetch failed; seeding without taste centroids")
        return None


def upgrade_household(store, account_id, members, now=None):
    """Bring a household created from an older seed up to date: add the seed
    members it is missing (e.g. Sports Fan, seed v2). Returns True when the
    household changed. A household already on the current seed costs one read."""
    seed = store.seed()
    marker = store.marker(account_id)
    if not marker or int(core.from_dynamo(marker.get("seed_version", 1))) >= core.seed_version(seed):
        return False
    to_add = core.seed_members_to_add(seed, marker, members)
    embeddings = _embeddings(core.watched_title_ids({"members": to_add})) if to_add else None
    items = core.build_seed_upgrade(account_id, seed, members, to_add, now or datetime.now(timezone.utc),
                                    embeddings=embeddings)
    seen_version = marker.get("seed_version")
    changed = store.upgrade_seed(account_id, int(marker.get("member_seq") or 0),
                                 None if seen_version is None else int(seen_version),
                                 core.seed_version(seed), items)
    if changed:
        logger.info(json.dumps({"event": "household_seed_upgraded", "added": len(to_add),
                                "seed_version": core.seed_version(seed)}))
    return changed and bool(to_add)


def get_household(store, account_id, now=None):
    members = store.members(account_id)
    if members:
        try:
            if upgrade_household(store, account_id, members, now):
                members = store.members(account_id)
        except Exception:  # noqa: BLE001 -- an upgrade failure must not hide the household
            logger.exception("household seed upgrade failed")
        return core.members_response(members)
    seed = store.seed()
    embeddings = _embeddings(core.watched_title_ids(seed))
    items = core.build_household(account_id, seed, now or datetime.now(timezone.utc), embeddings=embeddings)
    try:
        store.provision(items)
        logger.info(json.dumps({"event": "household_created", "members": len(items["households"]) - 1}))
    except AlreadyProvisioned:
        pass  # A concurrent first call won; read what it wrote.
    return core.members_response(store.members(account_id))


SEARCH_CACHE_TTL_S = 15 * 60
SEARCH_CACHE_MAX = 500
_search_cache = {}  # query json -> (expires, response)


def cached_search(body):
    """Catalog search, cached per container. Cold-start queries depend only on
    a member's limits and genres, so most members share the same few results."""
    key = json.dumps(body, sort_keys=True)
    hit = _search_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    if len(_search_cache) >= SEARCH_CACHE_MAX:
        _search_cache.clear()
    resp = catalog_search(body)
    _search_cache[key] = (time.time() + SEARCH_CACHE_TTL_S, resp)
    return resp


def rails_source():
    """HOME_RAILS_SOURCE, read per call so a config change applies at once."""
    value = os.environ.get("HOME_RAILS_SOURCE", "seed")
    return value if value in core.RAILS_SOURCES else "seed"


def home_rows(store, profile_id, now=None):
    """The member's cache rows. With HOME_RAILS_SOURCE=personalize (or
    connect), the member's P13N# (or CONN#) rows win when it has any.
    Otherwise seeded rows win; with none, cold-start rows are (re)built when
    missing, stale or expired. Cold start never sees, and so never deletes,
    P13N# or CONN# rows."""
    prefix = core.SOURCE_PREFIXES.get(rails_source())
    generated, cache_items = core.split_rows(store.rows(profile_id), prefix or core.P13N_PREFIX)
    if generated and prefix:
        return generated
    other = [i for i in cache_items if i.get("source") != cold_start.SOURCE]
    if other:
        return other
    now = now or datetime.now(timezone.utc)
    declared, affinities = store.taste(profile_id)
    basis, specs = cold_start.plan(core.from_dynamo(declared), core.from_dynamo(affinities), now)
    if cold_start.is_fresh(cache_items, basis, now):
        return cache_items
    try:
        rows = cold_start.build_rows(profile_id, basis, specs, cached_search, now)
    except Exception:  # noqa: BLE001 -- serve what is stored rather than fail Home
        logger.exception("cold-start rows failed")
        return cache_items
    if not rows:
        return cache_items
    keep = {r["sk"] for r in rows}
    store.write_rows(rows, [i["sk"] for i in cache_items if i["sk"] not in keep])
    logger.info(json.dumps({"event": "cold_start_rows", "rails": len(rows) - 1, "rebuilt": bool(cache_items)}))
    return rows


def tappable_options(store, profile_id):
    """Idle-search starter texts. Stored options come from the taste profile
    (written by the events processor); a profile without them gets generic
    ones at random. L3 limits are re-applied here, since rules can change
    after the options were chosen. A taste written before options existed
    (e.g. a seeded member) gets them computed once from its centroids."""
    declared, affinities = store.taste(profile_id)
    declared, affinities = core.from_dynamo(declared), core.from_dynamo(affinities)
    stored = affinities.get("tappable_options")
    if stored is None and affinities.get("version") is not None:
        try:
            cents = l2_centroids(affinities.get("centroids"))
            fresh = tappable.from_taste(cents, (), declared) if cents else []
            if fresh:
                store.backfill_options(profile_id, affinities["version"], fresh)
                stored = fresh
        except Exception:  # noqa: BLE001 -- generic options still show
            logger.exception("tappable options backfill failed")
    return tappable.visible(stored, declared)


def l2_centroids(stored):
    """AFFINITIES.centroids ({"v", "w", "n"}) -> [{"vector", "weight"}]."""
    out = []
    for c in stored or []:
        try:
            out.append({"vector": core.decode_vector(c["v"]), "weight": float(c.get("w") or 0)})
        except Exception:  # noqa: BLE001
            continue
    return out


def get_home(store, account_id, profile_id, part=None):
    if not core.is_profile_id(profile_id) or not core.is_active(store.member(account_id, profile_id)):
        return None
    if part == "options":
        return {"profileId": profile_id, "tappableOptions": tappable_options(store, profile_id)}
    cache_items = home_rows(store, profile_id)
    movies = hydrate(core.home_title_ids(cache_items))
    return core.home_response(profile_id, cache_items, movies)


def add_member(store, account_id, body, now=None):
    """POST /household/members -> the new member's view. Raises core.MemberError."""
    marker = store.marker(account_id)
    if not marker:
        raise core.MemberError(404, "household not found")
    items = core.build_member(account_id, body, store.members(account_id), now or datetime.now(timezone.utc))
    store.add_member(account_id, int(marker.get("member_seq") or 0), items)
    logger.info(json.dumps({"event": "member_added", "kids": bool(items["member"]["avatar"].get("kids"))}))
    return core.member_view(items["member"])


def set_member_removed(store, account_id, profile_id, remove, now=None):
    """Remove or restore a member -> its updated view. Raises core.MemberError."""
    if not core.is_profile_id(profile_id):
        raise core.MemberError(404, "profile not found")
    marker = store.marker(account_id)
    if not marker:
        raise core.MemberError(404, "household not found")
    members = store.members(account_id)
    target = (core.check_remove if remove else core.check_restore)(members, profile_id)
    stamp = core.iso(now or datetime.now(timezone.utc)) if remove else None
    store.set_removed(account_id, int(marker.get("member_seq") or 0), profile_id, stamp)
    logger.info(json.dumps({"event": "member_removed" if remove else "member_restored"}))
    updated = {k: v for k, v in target.items() if k != "removed_at"}
    if stamp:
        updated["removed_at"] = stamp
    return core.member_view(updated)


def _body(event):
    try:
        return json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        return None


def lambda_handler(event, context):
    account_id = _caller(event)
    if not account_id:
        return respond(401, {"error": "unauthenticated"})
    resource = event.get("resource") or event.get("path") or ""
    method = (event.get("httpMethod") or "GET").upper()
    try:
        store = get_store()
        member_path = (event.get("pathParameters") or {}).get("profile_id")
        if member_path is not None and "/household/members/" in resource:
            action = resource.rsplit("/", 1)[-1]
            if method != "POST" or action not in ("remove", "restore"):
                return respond(404, {"error": "not found"})
            if is_booth(_claims(event)):
                return respond(403, {"error": "read-only in booth mode"})
            return respond(200, set_member_removed(store, account_id, member_path, remove=action == "remove"))
        if resource.endswith("/household/members") and method == "POST":
            if is_booth(_claims(event)):
                return respond(403, {"error": "read-only in booth mode"})
            return respond(201, add_member(store, account_id, _body(event)))
        if resource.endswith("/household"):
            return respond(200, get_household(store, account_id))
        if resource.endswith("/home"):
            params = event.get("queryStringParameters") or {}
            body = get_home(store, account_id, params.get("profile_id"), params.get("part"))
            if body is None:
                return respond(404, {"error": "profile not found"})
            return respond(200, body)
        return respond(404, {"error": "not found"})
    except core.MemberError as exc:
        return respond(exc.status, {"error": exc.reason})
    except core.SeedError:
        logger.exception("invalid household seed")
        return respond(500, {"error": "household seed is invalid"})
    except Exception:  # noqa: BLE001
        logger.exception("household request failed")
        return respond(500, {"error": "internal error"})
