"""Kinesis profile-events -> L1 `SESSION#behavior`, L2 `AFFINITIES` and TITLE# watch state.

docs/unified-profile-design.md §7.4 steps 1-6 (no curated EVT# items, no
Personalize forwarding, no row-cache invalidation yet; there is no nightly
rebuild -- the raw S3 event log is the fallback for replaying history).

Per batch, grouped by profile_id: read the L1 item, fold the events (l1.fold),
write it back conditional on the `seq` that was read (optimistic lock, retried).
Then compare L1 before and after (l2.signals): each newly finished view
upserts TITLE#<item>, and each scored view, agent pick opened, My List add or
feed-card feedback updates AFFINITIES (conditional on its `version`). Booth
events skip L2.
Duplicate deliveries are skipped through the item's `seen` event ids, so a
retried batch is applied once. A profile whose L1 write keeps failing reports
its earliest record as a batch item failure; Kinesis retries from there.
"""

import base64
import json
import logging
import os
import time
from collections import defaultdict
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

import l1
import l2
from contract import EVENT_TYPES

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SK = "SESSION#behavior"
WRITE_ATTEMPTS = 3
CATALOG_TTL_S = 15 * 60
CATALOG_MAX = 10_000
SEED_TITLES_MAX = 50

_table = None
_profiles = None
_catalog: dict = {}   # item_id -> (expires, {title, genres, cast} | None)


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["INTERACTIONS_TABLE"])
    return _table


def _to_native(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _to_native(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_to_native(x) for x in v]
    return v


def _to_dynamo(v):
    return json.loads(json.dumps(v), parse_float=Decimal)


def _hydrate(ids):
    from ibc_common import hydrate  # Lambda layer; imported lazily for unit tests
    return hydrate(ids)


def prefetch_catalog(item_ids, fetch=_hydrate, now=None):
    """Warm the in-memory catalog cache for `item_ids` with one search."""
    now = now or time.time()
    missing = [i for i in dict.fromkeys(item_ids) if i and not (i in _catalog and _catalog[i][0] > now)]
    if missing:
        try:
            found = fetch(missing)
        except Exception:  # noqa: BLE001 -- enrichment is optional; facts still land
            logger.exception("catalog lookup failed")
            found = {}
        if len(_catalog) + len(missing) > CATALOG_MAX:
            _catalog.clear()
        for i in missing:
            m = found.get(i)
            _catalog[i] = (now + CATALOG_TTL_S,
                           {"title": m.get("title"), "genres": m.get("genres") or [], "cast": m.get("cast") or []}
                           if m else None)


def catalog(item_id):
    hit = _catalog.get(item_id)
    return hit[1] if hit else None


def decode(records):
    """Kinesis records -> [(sequence_number, event)]; undecodable ones are logged and skipped."""
    out = []
    for r in records:
        seq = r["kinesis"]["sequenceNumber"]
        try:
            out.append((seq, json.loads(base64.b64decode(r["kinesis"]["data"]))))
        except (ValueError, KeyError):
            logger.warning(json.dumps({"event": "bad_record", "sequence": seq}))
    return out


def update_profile(profile_id, events, table=None, profiles=None):
    """Fold `events` into L1 (optimistic lock on `seq`), then apply what the
    fold newly recorded to L2 and watch state. Returns the L1 applied count."""
    table = table or _get_table()
    for _attempt in range(WRITE_ATTEMPTS):
        item = table.get_item(Key={"profile_id": profile_id, "sk": SK}, ConsistentRead=True).get("Item")
        state = _to_native(item) if item else None
        old_seq = state.get("seq") if state else None
        if state:
            state.pop("profile_id", None)
            state.pop("sk", None)
        before = json.loads(json.dumps(state)) if state else None
        new, applied = l1.fold(state, events, catalog)
        if not applied:
            return 0
        kwargs = {"Item": _to_dynamo({"profile_id": profile_id, "sk": SK, **new})}
        if old_seq is None:
            kwargs["ConditionExpression"] = "attribute_not_exists(seq)"
        else:
            kwargs["ConditionExpression"] = "seq = :old"
            kwargs["ExpressionAttributeValues"] = {":old": old_seq}
        try:
            table.put_item(**kwargs)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            continue
        # L1 is committed. A failure below loses this batch's L2 update rather
        # than failing the batch: a retry would find the events already in
        # `seen`, so L2 could never be applied twice -- or at all -- by retrying.
        if any(ev.get("booth") for ev in events):
            return applied   # shared booth personas: sessions adapt, long-term taste does not
        try:
            update_long_term(profile_id, l2.signals(before, new), table=table, profiles=profiles,
                             tz_offset_min=new.get("tz_offset_min"))
        except Exception:  # noqa: BLE001
            logger.exception(f"L2 update failed for {profile_id}")
        return applied
    raise RuntimeError(f"seq conflict persisted for {profile_id}")


# --- L2 + watch state ---------------------------------------------------------

def _get_profiles():
    global _profiles
    if _profiles is None:
        _profiles = boto3.resource("dynamodb").Table(os.environ["PROFILES_TABLE"])
    return _profiles


def _fetch_embeddings(ids):
    from ibc_common import fetch_embeddings  # Lambda layer; imported lazily for unit tests
    return fetch_embeddings(ids)


def _tappable():
    import tappable  # Lambda layer; imported lazily for unit tests
    return tappable


def _declared(profiles, profile_id):
    """L3#DECLARED limits for option selection; {} when absent or unreadable."""
    try:
        return _to_native(profiles.get_item(Key={"user_id": profile_id, "data_type": "L3#DECLARED"})
                          .get("Item") or {})
    except Exception:  # noqa: BLE001 -- GET /home re-applies the limits at read time
        logger.exception("L3 read failed; choosing options without limits")
        return {}


def set_options(aff, cents, declared, loved_genres=()):
    """Refresh AFFINITIES.tappable_options from the taste being written.
    Leaves the stored options alone when there is no taste to choose from."""
    try:
        opts = _tappable().from_taste(cents, loved_genres, declared)
    except Exception:  # noqa: BLE001 -- options are optional; the taste update still lands
        logger.exception("tappable options failed")
        return
    if opts:
        aff["tappable_options"] = opts


def _labels(item_id, view):
    """(entity labels, alpha) for a signal: a clip's entities come from its parent at alpha/2."""
    parent = (view or {}).get("parent")
    meta = catalog(parent or item_id)
    return l1.entities_for(meta), (l2.ALPHA * l2.PARENT_FACTOR if parent else l2.ALPHA)


def upsert_watch_state(table, profile_id, view):
    """TITLE#<item>: last_watched, completion, and `completed` once a view reaches 90%.
    The agent's long-term watched exclusion reads `completed` titles."""
    if catalog(view["item"]) is None:
        return  # not a catalog title (e.g. a clip); nothing for the agent to exclude
    if view.get("type") == "title_card":
        return  # a poster card in the clips feed previews a title; it is not a viewing of it
    c = view.get("completion") or 0
    sets = ["last_watched = :ts", "completion = :c", "content_type = :t"]
    values = {":ts": l1.iso(view["ts"]), ":c": Decimal(str(c)), ":t": view.get("type") or "movie"}
    update = "SET " + ", ".join(sets)
    if c >= 0.9:
        update += ", completed = :yes ADD times_watched :one"
        values.update({":yes": True, ":one": 1})
    else:
        update += ", completed = if_not_exists(completed, :no)"
        values[":no"] = False
    table.update_item(Key={"profile_id": profile_id, "sk": f"TITLE#{view['item']}"},
                      UpdateExpression=update, ExpressionAttributeValues=values)


def _seed_centroids(table, profile_id, exclude=()):
    """Centroids from the member's completed titles, oldest first -- the same
    vectors the agent derives when none are stored, so the first stored update
    extends that taste instead of replacing it with a single title. `exclude` =
    titles this batch is about to score, so a title finished just now is not
    counted twice."""
    from boto3.dynamodb.conditions import Attr, Key
    items, kwargs = [], {"KeyConditionExpression": Key("profile_id").eq(profile_id) & Key("sk").begins_with("TITLE#"),
                         "FilterExpression": Attr("completed").eq(True), "ProjectionExpression": "sk, last_watched"}
    while True:
        resp = table.query(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    ids = [i["sk"][len("TITLE#"):] for i in sorted(items, key=lambda i: i.get("last_watched", ""))]
    ids = [t for t in ids if t not in set(exclude)][-SEED_TITLES_MAX:]
    emb = _fetch_embeddings(ids) if ids else {}
    return l2.taste_centroids([emb[t] for t in ids if t in emb])


def update_long_term(profile_id, sigs, table=None, profiles=None, tz_offset_min=None):
    """Apply new L1 signals to AFFINITIES (optimistic lock on `version`) and TITLE#."""
    if not sigs:
        return 0
    table = table or _get_table()
    for kind, item, _e, view in sigs:
        if kind == "view":
            upsert_watch_state(table, profile_id, view)
    scored = [s for s in sigs if s[2] is not None]
    if not scored:
        return 0
    profiles = profiles or _get_profiles()
    liked = [(view or {}).get("parent") or item for kind, item, e, view in scored
             if e > 0 and kind != "agent_accept"]
    vectors = _fetch_embeddings(list(dict.fromkeys(liked))) if liked else {}
    declared = _declared(profiles, profile_id) if vectors else None
    seeded = None
    for _attempt in range(WRITE_ATTEMPTS):
        key = {"user_id": profile_id, "data_type": "AFFINITIES"}
        current = _to_native(profiles.get_item(Key=key, ConsistentRead=True).get("Item") or {})
        old_version = current.get("version")
        aff = {**current, **key}
        entities = dict(aff.get("entities") or {})
        contexts = dict(aff.get("contexts") or {})
        contribs = dict(aff.get("taste_contrib") or {})
        cents = l2.decode_centroids(aff.get("centroids"))
        moved = False
        for kind, item, e, view in scored:
            labels, alpha = _labels(item, view)
            ts = (view or {}).get("ts") or time.time()
            if kind == "agent_accept":
                # Interest, not taste: a small favorites step, no evidence, no centroid.
                l2.update_entities(entities, labels, e, ts, alpha, count=False)
                continue
            if kind == "view" and view.get("reason") != "marked":
                # A mark says nothing about when it was watched, so no time-of-day context.
                l2.update_context(contexts, view, labels, e, alpha)
            step = l2.fold_contribution(contribs, l2.contribution_key(item, view), kind, e, ts)
            if step["entities"] or step["first"]:
                l2.update_entities(entities, labels, step["entities"], ts, alpha, count=step["first"])
            v = vectors.get((view or {}).get("parent") or item)
            if step["centroid"] > 0 and v:
                if not cents:
                    if seeded is None:
                        seeded = _seed_centroids(table, profile_id, set(liked))
                    cents = [dict(c) for c in seeded]
                cents = l2.update_centroids(cents, v, step["centroid"], new_title=step["first_centroid"])
                moved = True
        aff["entities"] = entities
        aff["taste_contrib"] = l2.prune_contributions(contribs, time.time())
        if contexts:
            aff["contexts"] = contexts
        if tz_offset_min is not None:
            aff["tz_offset_min"] = tz_offset_min   # lets the agent pick the current bucket
        if cents:
            aff["centroids"] = l2.encode_centroids(cents)
            if declared is not None and moved:   # the taste vectors moved this run
                set_options(aff, cents, declared)
        aff["version"] = int(old_version or 0) + 1
        aff["updated_at"] = l1.iso(time.time())
        kwargs = {"Item": _to_dynamo(aff)}
        if old_version is None:
            kwargs["ConditionExpression"] = "attribute_not_exists(version)"
        else:
            kwargs["ConditionExpression"] = "version = :old"
            kwargs["ExpressionAttributeValues"] = {":old": old_version}
        try:
            profiles.put_item(**kwargs)
            logger.info(json.dumps({"event": "l2", "profile": profile_id, "signals": len(scored),
                                    "version": aff["version"], "centroids": len(cents),
                                    "options": len(aff.get("tappable_options") or [])}))
            return len(scored)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise RuntimeError(f"AFFINITIES version conflict persisted for {profile_id}")


LOVED_EVENT = "genres_loved"
SERVER_PRODUCERS = {"profile_settings"}


def apply_loved(profile_id, genres, profiles=None, now=None):
    """Give each loved genre a weak, fading start in AFFINITIES (optimistic lock
    on `version`). Repeats are harmless: a genre already at that strength is left."""
    profiles = profiles or _get_profiles()
    now = now or time.time()
    declared = None
    for _attempt in range(WRITE_ATTEMPTS):
        key = {"user_id": profile_id, "data_type": "AFFINITIES"}
        current = _to_native(profiles.get_item(Key=key, ConsistentRead=True).get("Item") or {})
        old_version = current.get("version")
        aff = {**current, **key}
        entities = dict(aff.get("entities") or {})
        changed = l2.seed_loved(entities, genres, now)
        if not changed:
            return 0
        aff["entities"] = entities
        if not l2.decode_centroids(aff.get("centroids")):
            # No viewing taste yet: the genres loved in onboarding choose the options.
            if declared is None:
                declared = _declared(profiles, profile_id)
            set_options(aff, [], declared, [g["id"] for g in genres])
        aff["version"] = int(old_version or 0) + 1
        aff["updated_at"] = l1.iso(now)
        kwargs = {"Item": _to_dynamo(aff)}
        if old_version is None:
            kwargs["ConditionExpression"] = "attribute_not_exists(version)"
        else:
            kwargs["ConditionExpression"] = "version = :old"
            kwargs["ExpressionAttributeValues"] = {":old": old_version}
        try:
            profiles.put_item(**kwargs)
            logger.info(json.dumps({"event": "l2_loved", "profile": profile_id, "genres": changed,
                                    "version": aff["version"]}))
            return len(changed)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    raise RuntimeError(f"AFFINITIES version conflict persisted for {profile_id}")


def _loved_notices(decoded):
    """`genres_loved` records written by the Settings API. Clients cannot send
    this type: /events accepts only the contract's behavior event types."""
    return [(seq, ev) for seq, ev in decoded
            if ev.get("event_type") == LOVED_EVENT and ev.get("producer") in SERVER_PRODUCERS]


def lambda_handler(event, context):
    decoded = decode(event.get("Records") or [])
    loved_failures = []
    for seq, ev in _loved_notices(decoded):
        genres = [g for g in ((ev.get("payload") or {}).get("genres") or []) if isinstance(g, dict) and g.get("id")]
        if not ev.get("profile_id") or not genres:
            continue
        try:
            apply_loved(ev["profile_id"], genres)
        except Exception:  # noqa: BLE001
            logger.exception(f"loved-genre update failed for {ev.get('profile_id')}")
            loved_failures.append(seq)
    # The stream also carries server-side notices (e.g. `settings_changed` from
    # the Settings API) for the S3 event log; only viewer behavior folds into
    # L1/L2 -- a notice must not start or advance a session.
    behavior = [(seq, ev) for seq, ev in decoded if ev.get("event_type") in EVENT_TYPES]
    if len(behavior) < len(decoded):
        logger.info(json.dumps({"event": "non_behavior_skipped", "count": len(decoded) - len(behavior)}))
    decoded = behavior
    by_profile = defaultdict(list)
    for seq, ev in decoded:
        by_profile[ev.get("profile_id")].append((seq, ev))
    item_ids = []
    for pairs in by_profile.values():
        for _, ev in pairs:
            p = ev.get("payload") or {}
            item_ids += [p.get("parent_item_id") or p.get("item_id")]
    prefetch_catalog([i for i in item_ids if i])

    failures, applied = [], 0
    for profile_id, pairs in by_profile.items():
        if not profile_id:
            continue
        try:
            applied += update_profile(profile_id, [ev for _, ev in pairs])
        except Exception:  # noqa: BLE001
            logger.exception(f"L1 update failed for {profile_id}")
            failures.append(min(seq for seq, _ in pairs))
    logger.info(json.dumps({"event": "batch", "records": len(decoded), "profiles": len(by_profile),
                            "applied": applied, "failed_profiles": len(failures)}))
    failures += loved_failures
    if failures:
        # Kinesis retries from the earliest failed sequence number.
        return {"batchItemFailures": [{"itemIdentifier": min(failures, key=int)}]}
    return {"batchItemFailures": []}
