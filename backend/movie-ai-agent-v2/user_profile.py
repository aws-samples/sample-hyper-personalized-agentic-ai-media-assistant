"""Member profile reader (movie-agent-user-profiles DynamoDB).

The taste block comes from the member's L2 `AFFINITIES` item, which the
stream processor updates from viewing, rendered with fixed templates (design
§6.1, no LLM). A member without one has no taste block. Also reads L3
rules, taste centroids and watch state.

Cached on the memory object and re-read every PROFILE_TTL_S (see
AgentCoreMemory._stale) — this module does no caching of its own beyond the
boto3 table singleton.
"""

import logging
from decimal import Decimal

import boto3

from config import AWS_REGION, DYNAMODB_TABLE

logger = logging.getLogger(__name__)

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(DYNAMODB_TABLE)
    return _table


def _to_native(v):
    if isinstance(v, Decimal):
        return int(v) if v == int(v) else float(v)
    if isinstance(v, list):
        return [_to_native(x) for x in v]
    if isinstance(v, dict):
        return {k: _to_native(x) for k, x in v.items()}
    return v


def is_member(account_id: str, profile_id: str) -> bool:
    """True when profile_id is an active member of the household owned by account_id
    (Cognito sub). A removed member (removed_at set) keeps its data but is not usable."""
    from config import HOUSEHOLDS_TABLE
    if not (HOUSEHOLDS_TABLE and account_id and profile_id):
        return False
    try:
        table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(HOUSEHOLDS_TABLE)
        item = table.get_item(Key={"account_id": account_id, "sk": "MEMBER#" + profile_id}).get("Item")
        return bool(item) and not item.get("removed_at")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"household lookup failed: {e}")
        return False


def get_watched_ids(profile_id: str) -> list[str]:
    """Titles this member finished recently, newest first (for exclusion).

    Reads TITLE# watch-state items from user-interactions: completed (>= ~90%),
    last watched within WATCHED_EXCLUDE_DAYS, capped at WATCHED_EXCLUDE_MAX.
    """
    from datetime import datetime, timedelta, timezone
    from boto3.dynamodb.conditions import Attr, Key
    from config import INTERACTIONS_TABLE, WATCHED_EXCLUDE_DAYS, WATCHED_EXCLUDE_MAX
    if not (INTERACTIONS_TABLE and profile_id):
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WATCHED_EXCLUDE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    table = boto3.resource("dynamodb", region_name=AWS_REGION).Table(INTERACTIONS_TABLE)
    kwargs = {
        "KeyConditionExpression": Key("profile_id").eq(profile_id) & Key("sk").begins_with("TITLE#"),
        "FilterExpression": Attr("completed").eq(True) & Attr("last_watched").gte(cutoff),
        "ProjectionExpression": "sk, last_watched",
    }
    items: list[dict] = []
    try:
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"watched lookup failed for {profile_id}: {e}")
        return []
    items.sort(key=lambda i: i.get("last_watched", ""), reverse=True)
    return [i["sk"][len("TITLE#"):] for i in items[:WATCHED_EXCLUDE_MAX]]


def get_taste_vectors(profile_id: str, watched_ids: list[str]) -> list[tuple[float, list[float]]]:
    """[(weight, unit vector)] for the member: stored AFFINITIES.centroids, else
    derived from the watched titles' catalog embeddings (not persisted -- only
    the household Lambda / stream processor write L2)."""
    from taste import decode_stored, taste_centroids
    try:
        item = _get_table().get_item(Key={"user_id": profile_id, "data_type": "AFFINITIES"}).get("Item")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"affinities fetch failed for {profile_id}: {e}")
        item = None
    stored = decode_stored((item or {}).get("centroids"))
    if stored:
        return stored
    if not watched_ids:
        return []
    try:
        from retrieval import fetch_embeddings
        emb = fetch_embeddings(watched_ids)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"embedding fetch failed for {profile_id}: {e}")
        return []
    vectors = [emb[t] for t in reversed(watched_ids) if t in emb]  # oldest first
    return [(c["weight"], c["vector"]) for c in taste_centroids(vectors)]


def get_declared(profile_id: str) -> tuple[list[dict], dict]:
    """L3#DECLARED split into (preference rules, limits).

    Preferences (the member's own taste rules) can be overridden by an explicit
    request for the current conversation only; limits (guardrails such as a kids
    profile) never can and are never shown to a model. Values stay free text.
    """
    try:
        item = _get_table().get_item(Key={"user_id": profile_id, "data_type": "L3#DECLARED"}).get("Item")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"declared fetch failed for {profile_id}: {e}")
        item = None
    item = {k: _to_native(v) for k, v in (item or {}).items()}
    prefs: list[dict] = []
    for g in item.get("excluded_genres") or []:
        g = str(g).strip().lower()
        if g:
            prefs.append({"kind": "genre", "value": g, "text": f"avoid genre: {g}"})
    for t in item.get("avoid_themes") or []:
        t = str(t).strip()
        if t:
            prefs.append({"kind": "theme", "value": t, "text": f"avoid theme: {t}"})
    limits = {"kids": bool(item.get("kids_profile")), "genres": []}
    return prefs, limits


TASTE_MIN_N = 3                  # entities seen fewer times are not enough evidence
TASTE_TOP_POSITIVE = 8
TASTE_TOP_NEGATIVE = 3
_HALF_LIFE_DAYS = {"genre": 30, "person": 60}   # same as the stream processor (l2.py)
_KIND_LABEL = {"genre": "genre", "person": "person"}


def _decayed(entity: dict, now: float) -> float:
    """Score faded to `now` with the processor's half-lives; seeded entries (no `last`) do not fade."""
    import math
    s = float(entity.get("s") or 0.0)
    last = entity.get("last")
    if last is None or now <= float(last):
        return s
    kind = str(entity.get("_key", "")).split(":", 1)[0]
    return s * math.pow(2.0, -(now - float(last)) / (_HALF_LIFE_DAYS.get(kind, 30) * 86400))


def render_affinities(aff: dict, excluded_genres=(), now: float | None = None,
                      tz_offset_min=None) -> list[str]:
    """L2 AFFINITIES -> taste lines (design §6.1 step 1, templates, no LLM).

    Top 8 positive and top 3 negative entities with at least TASTE_MIN_N
    signals: s >= 0.6 "Strongly enjoys", 0.3 <= s < 0.6 "Enjoys",
    s <= -0.3 "Tends to skip". Genres the member excluded are never shown
    (declared limits are rules, applied elsewhere). Then one line for the
    viewer's current time of day (AFFINITIES.contexts), when it has enough
    viewing, and the seed's free-text notes under their own label.
    """
    import time as _time
    now = now or _time.time()
    tz = tz_offset_min if tz_offset_min is not None else aff.get("tz_offset_min")
    bucket = time_bucket(now, tz)
    banned = {str(g).strip().lower() for g in excluded_genres or []}
    scored = []
    for key, e in (aff.get("entities") or {}).items():
        if int(e.get("n") or 0) < TASTE_MIN_N:
            continue
        kind = key.split(":", 1)[0]
        label = str(e.get("label") or key.split(":", 1)[-1]).strip()
        if kind == "genre" and label.lower() in banned:
            continue
        scored.append((_decayed({**e, "_key": key}, now), label, kind))
    pos = sorted([x for x in scored if x[0] >= 0.3], key=lambda x: -x[0])[:TASTE_TOP_POSITIVE]
    neg = sorted([x for x in scored if x[0] <= -0.3], key=lambda x: x[0])[:TASTE_TOP_NEGATIVE]

    def names(items):
        return ", ".join(f"{label} ({_KIND_LABEL.get(kind, kind)})" for _, label, kind in items)

    lines = []
    strong = [x for x in pos if x[0] >= 0.6]
    mild = [x for x in pos if x[0] < 0.6]
    if strong:
        lines.append(f"- Strongly enjoys: {names(strong)}")
    if mild:
        lines.append(f"- Enjoys: {names(mild)}")
    if neg:
        lines.append(f"- Tends to skip: {names(neg)}")
    bucket_line = render_bucket(aff, bucket, banned, now)
    if bucket_line:
        lines.append(bucket_line)
    notes = [str(n).strip() for n in aff.get("notes") or [] if str(n).strip()]
    if notes:
        lines.append(f"- Seeded persona notes (not from viewing): {'; '.join(notes)}")
    return lines


DAY_START_H = 5      # same as the stream processor (l1.time_bucket)
EVENING_H = 17
BUCKET_MIN_VIEWS = 3
_BUCKET_NAMES = {"weekday|daytime": "Weekday daytime", "weekday|evening": "Weekday evenings",
                 "weekend|daytime": "Weekend daytime", "weekend|evening": "Weekend evenings"}


def time_bucket(epoch: float, tz_offset_min) -> str | None:
    """Local weekday|weekend x daytime|evening; after midnight counts as the
    previous evening. Must match infra/lambdas/profile_events/l1.time_bucket."""
    from datetime import datetime, timezone
    if tz_offset_min is None:
        return None
    local = datetime.fromtimestamp(epoch + int(tz_offset_min) * 60 - DAY_START_H * 3600, timezone.utc)
    day = "weekend" if local.weekday() >= 5 else "weekday"
    part = "daytime" if local.hour < EVENING_H - DAY_START_H else "evening"
    return f"{day}|{part}"


def render_bucket(aff: dict, bucket: str | None, banned=frozenset(), now: float = 0.0) -> str:
    """One line for the viewer's current time of day, from AFFINITIES.contexts,
    once that bucket has BUCKET_MIN_VIEWS scored views. '' otherwise."""
    ctx = (aff.get("contexts") or {}).get(bucket or "")
    if not ctx or int(ctx.get("views") or 0) < BUCKET_MIN_VIEWS:
        return ""
    liked = []
    for key, e in (ctx.get("entities") or {}).items():
        label = str(e.get("label") or key.split(":", 1)[-1]).strip()
        if int(e.get("n") or 0) < TASTE_MIN_N or (key.startswith("genre:") and label.lower() in banned):
            continue
        s = _decayed({**e, "_key": key}, now)
        if s >= 0.3:
            liked.append((s, label))
    parts = []
    if liked:
        parts.append("mostly " + ", ".join(label for _, label in sorted(liked, key=lambda x: -x[0])[:4]))
    short, long_ = float(ctx.get("short_min") or 0), float(ctx.get("long_min") or 0)
    if short + long_ > 0:
        if long_ / (short + long_) >= 0.7:
            parts.append("full-length titles")
        elif short / (short + long_) >= 0.7:
            parts.append("short clips")
    if not parts:
        return ""
    return f"- {_BUCKET_NAMES.get(bucket, bucket)} (now): {'; '.join(parts)}"


def get_profile_block(user_id: str, tz_offset_min=None) -> str:
    """The member's taste as a soft-guidance block, or '' if none.

    Household members: rendered from L2 AFFINITIES, which the stream processor
    keeps updated from viewing. `tz_offset_min` (this session's, from L1)
    picks the current time-of-day line; without it the member's last known
    offset is used. A member with no AFFINITIES item (or none worth showing)
    gets ''.
    """
    try:
        aff = _get_table().get_item(Key={"user_id": user_id, "data_type": "AFFINITIES"}).get("Item")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"affinities fetch failed for {user_id}: {e}")
        aff = None
    if aff:
        try:
            declared = _get_table().get_item(Key={"user_id": user_id, "data_type": "L3#DECLARED"}).get("Item") or {}
        except Exception as e:  # noqa: BLE001
            logger.debug(f"declared fetch failed for {user_id}: {e}")
            declared = {}
        lines = render_affinities(_to_native(aff), declared.get("excluded_genres") or [],
                                  tz_offset_min=tz_offset_min)
        if lines:
            return "\n".join(["Viewer taste (from viewing history; soft guidance, never override the request):", *lines])
    return ""
