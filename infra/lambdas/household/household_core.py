"""Pure household logic: no AWS calls, so it is unit-testable without mocks.

Turns the committed default-household seed into the items a new household
needs across four tables, and shapes the API responses.

Item layout (PK / SK):
  households            account_id = Cognito sub / HOUSEHOLD (marker), MEMBER#<profile_id>
  profiles              user_id = profile_id   / L3#DECLARED, AFFINITIES, watch_list
  recommendation-cache  profile_id             / HERO#home, ROWS#home#<rail_key>
  user-interactions     profile_id             / TITLE#<title_id>, EVT#<iso_ts>#<event_id>

Rows are written in the same shape the offline Personalize job will write, so
readers cannot tell seeded rows from generated ones (only `source` differs).

Personalize rows live beside them under their own prefix
(P13N#HERO#home, P13N#ROWS#home#<rail_key>), written only by
infra/deployment/personalize_rails.py, so seed and cold-start rows are never
overwritten. GET /home shows them only when HOME_RAILS_SOURCE=personalize;
switching back to `seed` restores the old Home without touching any data.
Connect Predictive Insights rows work the same way under CONN# (written by
infra/deployment/connect_rails.py, served when HOME_RAILS_SOURCE=connect).
"""

import base64
import math
import re
import struct
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

MARKER_SK = "HOUSEHOLD"
MEMBER_PREFIX = "MEMBER#"
HERO_SK = "HERO#home"
ROWS_PREFIX = "ROWS#home#"
P13N_PREFIX = "P13N#"         # Personalize rows: P13N# + HERO_SK / ROWS_PREFIX + rail_key
CONN_PREFIX = "CONN#"         # Connect Predictive Insights rows, same layout
PERSONALIZE_SOURCE = "personalize"
CONNECT_SOURCE = "connect"
SOURCE_PREFIXES = {PERSONALIZE_SOURCE: P13N_PREFIX, CONNECT_SOURCE: CONN_PREFIX}
GENERATED_PREFIXES = tuple(SOURCE_PREFIXES.values())
RAILS_SOURCES = ("seed", *SOURCE_PREFIXES)  # HOME_RAILS_SOURCE values; anything else means seed
TITLE_PREFIX = "TITLE#"
EVT_PREFIX = "EVT#"

SEED_SOURCE = "seed"
EVENT_TTL_DAYS = 180          # curated EVT# items expire (design §5.3)
SEEDED_WATCH_SPACING = timedelta(days=1)
SEEDED_VIEW_ENGAGEMENT = 0.8  # completed long-form view (design §7.4)
MAX_TRANSACT_ITEMS = 100      # DynamoDB TransactWriteItems limit

PROFILE_ID_RE = re.compile(r"^p-[0-9a-f]{32}$")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# L2 taste centroids (design §7.4). Keep in sync with
# backend/movie-ai-agent-v2/taste.py; test_taste.py checks both agree.
TASTE_JOIN_COSINE = 0.55
TASTE_MAX_CENTROIDS = 3
TASTE_EVIDENCE_K = 2.0
TASTE_TITLE_E = 0.8
CENTROID_RATE = 0.1


class SeedError(ValueError):
    """The seed file is malformed; provisioning must not write partial data."""


def _unit(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


def taste_centroids(vectors):
    """Group liked-title embeddings (oldest first) into up to 3 unit-length centroids.

    Each title counts TASTE_TITLE_E of evidence. A title joins the most similar
    centroid when cosine >= TASTE_JOIN_COSINE and moves it by
    max(CENTROID_RATE * e, e / (E + e)) -- an evidence-weighted running mean while
    the centroid is young -- otherwise starts a new one; once the cap is reached it
    nudges the nearest by CENTROID_RATE / 2 * e. Weight = E / (E + TASTE_EVIDENCE_K).
    Returns [{"vector", "weight", "count", "evidence"}], most evidence first.
    """
    groups = []  # {"vector", "count", "evidence"}
    for v in vectors:
        if not v:
            continue
        u, e = _unit(v), TASTE_TITLE_E
        best, best_sim = None, -2.0
        for g in groups:
            sim = sum(a * b for a, b in zip(u, g["vector"]))
            if sim > best_sim:
                best, best_sim = g, sim
        if best is not None and best_sim >= TASTE_JOIN_COSINE:
            step = max(CENTROID_RATE * e, e / (best["evidence"] + e))
        elif len(groups) < TASTE_MAX_CENTROIDS:
            groups.append({"vector": u, "count": 1, "evidence": e})
            continue
        else:
            step = CENTROID_RATE / 2 * e
        best["vector"] = _unit([(1 - step) * a + step * b for a, b in zip(best["vector"], u)])
        best["evidence"] = best["evidence"] + e
        best["count"] += 1
    out = [{"vector": g["vector"], "weight": round(g["evidence"] / (g["evidence"] + TASTE_EVIDENCE_K), 4),
            "count": g["count"], "evidence": round(g["evidence"], 4)} for g in groups]
    return sorted(out, key=lambda c: -c["evidence"])


def encode_vector(v):
    """float32 little-endian, base64 (~5.5 KB for 1024 dims)."""
    return base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode("ascii")


def decode_vector(s):
    raw = base64.b64decode(s)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def stored_centroids(vectors):
    """taste_centroids() in the AFFINITIES.centroids storage shape."""
    return [{"v": encode_vector(c["vector"]), "w": c["weight"], "n": c["count"], "e": c["evidence"]}
            for c in taste_centroids(vectors)]


def iso(ts: datetime) -> str:
    """UTC ISO-8601 with a Z suffix. One format everywhere keeps EVT# keys sortable as text."""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_profile_id() -> str:
    return "p-" + uuid.uuid4().hex


def is_profile_id(value) -> bool:
    return isinstance(value, str) and bool(PROFILE_ID_RE.match(value))


def to_dynamo(value):
    """boto3's resource layer rejects floats; store them as Decimal."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, list):
        return [to_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    return value


def from_dynamo(value):
    if isinstance(value, Decimal):
        return int(value) if value == int(value) else float(value)
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    return value


def validate_seed(seed: dict) -> None:
    members = seed.get("members")
    if not isinstance(members, list) or not members:
        raise SeedError("seed must contain at least one member")
    keys = set()
    for m in members:
        key = m.get("member_key")
        if not isinstance(key, str) or not _KEY_RE.match(key) or key in keys:
            raise SeedError(f"invalid or duplicate member_key: {key!r}")
        keys.add(key)
        if not m.get("display_name"):
            raise SeedError(f"{key}: display_name is required")
        if not m.get("hero"):
            raise SeedError(f"{key}: hero title_id is required")
        rails = m.get("rails") or []
        if not rails:
            raise SeedError(f"{key}: at least one rail is required")
        rail_keys = [r.get("rail_key") for r in rails]
        if any(not isinstance(k, str) or not _KEY_RE.match(k) for k in rail_keys) or len(set(rail_keys)) != len(rail_keys):
            raise SeedError(f"{key}: invalid or duplicate rail_key")
        for r in rails:
            if not r.get("items") or any(not i.get("title_id") for i in r["items"]):
                raise SeedError(f"{key}/{r['rail_key']}: rail items need a title_id")


def watched_title_ids(seed: dict) -> list:
    """Every watched title across the seed's members (to fetch their embeddings)."""
    return list(dict.fromkeys(w["title_id"] for m in seed.get("members") or [] for w in m.get("watched") or []))


def build_household(account_id: str, seed: dict, now: datetime, new_id=new_profile_id,
                    embeddings: dict | None = None) -> dict:
    """Return {table_key: [items]} for one new household. Raises SeedError on a bad seed.

    `embeddings` maps title_id -> catalog vector. When given, each member's
    AFFINITIES gets taste centroids built from its watched titles; when not,
    the agent derives them from the watched titles at session start.
    """
    validate_seed(seed)
    stamp = iso(now)
    out = {"households": [{"account_id": account_id, "sk": MARKER_SK, "created_at": stamp,
                           "seed_version": seed_version(seed)}],
           "profiles": [], "cache": [], "interactions": []}
    for rank, m in enumerate(seed["members"], 1):
        _add_seed_member(out, account_id, m, rank, new_id(), now, embeddings)
    return _finish(out)


def seed_version(seed: dict) -> int:
    return int(seed.get("version", 1))


def _finish(out: dict) -> dict:
    total = sum(len(v) for v in out.values())
    if total > MAX_TRANSACT_ITEMS:
        raise SeedError(f"household needs {total} writes; one transaction allows {MAX_TRANSACT_ITEMS}")
    return {k: [to_dynamo(i) for i in v] for k, v in out.items()}


def _add_seed_member(out: dict, account_id: str, m: dict, rank: int, pid: str, now: datetime,
                     embeddings: dict | None) -> None:
    """Append one seed member's items (all four tables) to `out`."""
    stamp = iso(now)
    expires = int((now + timedelta(days=EVENT_TTL_DAYS)).timestamp())
    out["households"].append({
        "account_id": account_id, "sk": MEMBER_PREFIX + pid, "profile_id": pid,
        "display_name": m["display_name"], "avatar": m.get("avatar") or {},
        "rank": rank, "seed_member_key": m["member_key"], "created_at": stamp})

    # recommendation-cache: hero + one item per rail (Personalize overwrites these).
    out["cache"].append({"profile_id": pid, "sk": HERO_SK, "title_id": m["hero"],
                         "source": SEED_SOURCE, "generated_at": stamp})
    for r_rank, rail in enumerate(m["rails"], 1):
        out["cache"].append({
            "profile_id": pid, "sk": ROWS_PREFIX + rail["rail_key"], "rank": r_rank,
            "display_title": rail["display_title"],
            "items": [{k: v for k, v in i.items() if k in ("title_id", "blurb")} for i in rail["items"]],
            "source": SEED_SOURCE, "generated_at": stamp})

    # profiles: L3 declared, L2 affinities, My List.
    declared = {**(m.get("declared") or {}), "version": 1, "updated_at": stamp, "updated_by": SEED_SOURCE}
    out["profiles"].append({"user_id": pid, "data_type": "L3#DECLARED", **declared})
    affinities = {**(m.get("affinities") or {}), "version": 1, "updated_at": stamp}
    # Seed lists watched titles newest first; centroids are built oldest first.
    vectors = [(embeddings or {}).get(w["title_id"]) for w in reversed(m.get("watched") or [])]
    if any(vectors):
        affinities["centroids"] = stored_centroids([v for v in vectors if v])
    out["profiles"].append({"user_id": pid, "data_type": "AFFINITIES", **affinities})
    out["profiles"].append({"user_id": pid, "data_type": "watch_list", "watch_list": list(m.get("my_list") or [])})

    # user-interactions: watched titles, most recent first, one day apart.
    for i, w in enumerate(m.get("watched") or []):
        ts = now - SEEDED_WATCH_SPACING * (i + 1)
        content_type = w.get("content_type", "movie")
        out["interactions"].append({
            "profile_id": pid, "sk": TITLE_PREFIX + w["title_id"], "content_type": content_type,
            "completion": 1.0, "completed": True, "times_watched": 1, "last_watched": iso(ts)})
        out["interactions"].append({
            "profile_id": pid, "sk": f"{EVT_PREFIX}{iso(ts)}#{uuid.uuid4().hex}", "event_type": "view",
            "item_id": w["title_id"], "content_type": content_type,
            "engagement": SEEDED_VIEW_ENGAGEMENT, "source": SEED_SOURCE, "expires_at": expires})


def seed_members_to_add(seed: dict, marker: dict | None, members: list) -> list:
    """Seed members an existing household is missing, after a seed upgrade.

    Runs only when the household was created from an older seed version, so a
    member that exists in both versions is never re-added. Members that do not
    fit under MAX_MEMBERS are skipped, never forced in.
    """
    if not marker or int(from_dynamo(marker.get("seed_version", 1))) >= seed_version(seed):
        return []
    have = {m.get("seed_member_key") for m in members}  # removed members count: never re-added
    room = MAX_MEMBERS - sum(1 for m in members if is_active(m))
    return [m for m in seed["members"] if m["member_key"] not in have][:max(room, 0)]


def build_seed_upgrade(account_id: str, seed: dict, members: list, to_add: list, now: datetime,
                       new_id=new_profile_id, embeddings: dict | None = None) -> dict:
    """Items for `to_add` seed members joining an existing household (ranked after the others)."""
    validate_seed(seed)
    out = {"households": [], "profiles": [], "cache": [], "interactions": []}
    rank = max((int(from_dynamo(m.get("rank", 0))) for m in members), default=0)
    for m in to_add:
        rank += 1
        _add_seed_member(out, account_id, m, rank, new_id(), now, embeddings)
    return _finish(out)


# --- adding a member (POST /household/members) -----------------------------

MAX_MEMBERS = 5                 # per household, like the large streaming services
MAX_NAME_LEN = 20
NEW_MEMBER_SOURCE = "household"
# Avatar choices for added members, picked round-robin by rank.
AVATARS = [{"hue": 265, "icon": "🦊"}, {"hue": 45, "icon": "🌟"}, {"hue": 190, "icon": "🐳"},
           {"hue": 330, "icon": "🌸"}, {"hue": 95, "icon": "🌵"}]


class MemberError(ValueError):
    """A new-member request the caller must fix (400), or a household limit (409)."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def clean_name(value):
    """Trimmed, whitespace-collapsed NFC display name, or None when unusable."""
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
        return None
    value = " ".join(value.split())
    return value if 0 < len(value) <= MAX_NAME_LEN else None


def build_member(account_id: str, body: dict, existing: list, now: datetime, new_id=new_profile_id) -> dict:
    """Return {"member": item, "profiles": [items]} for one new member. Raises MemberError.

    A new member starts empty: no rows, no L2, no watch history. Only a kids
    profile gets an L3 item (the hard limit); everyone else has no L3 item, so
    the Settings API serves the defaults at version 0 until onboarding saves.
    """
    if not isinstance(body, dict):
        raise MemberError(400, "body must be a JSON object")
    name = clean_name(body.get("display_name"))
    if not name:
        raise MemberError(400, f"display_name must be 1-{MAX_NAME_LEN} characters")
    kids = body.get("kids_profile", False)
    if not isinstance(kids, bool):
        raise MemberError(400, "kids_profile must be true or false")
    members = [m for m in existing if m.get("sk", "").startswith(MEMBER_PREFIX)]
    active = [m for m in members if is_active(m)]
    if len(active) >= MAX_MEMBERS:
        raise MemberError(409, f"a household can have at most {MAX_MEMBERS} profiles")
    if any(m.get("display_name", "").casefold() == name.casefold() for m in active):
        raise MemberError(409, "a profile with that name already exists")

    stamp = iso(now)
    pid = new_id()
    # Rank counts removed members too, so a restored profile never shares one.
    rank = max((int(from_dynamo(m.get("rank", 0))) for m in members), default=0) + 1
    avatar = dict(AVATARS[(rank - 1) % len(AVATARS)])
    if kids:
        avatar["kids"] = True
    member = {"account_id": account_id, "sk": MEMBER_PREFIX + pid, "profile_id": pid,
              "display_name": name, "avatar": avatar, "rank": rank, "created_at": stamp,
              "created_by": NEW_MEMBER_SOURCE}
    profiles = [{"user_id": pid, "data_type": "watch_list", "watch_list": []}]
    if kids:
        profiles.append({"user_id": pid, "data_type": "L3#DECLARED", "kids_profile": True,
                         "version": 1, "updated_at": stamp, "updated_by": NEW_MEMBER_SOURCE})
    return {"member": to_dynamo(member), "profiles": [to_dynamo(p) for p in profiles]}


def member_view(item: dict) -> dict:
    view = {"profileId": item["profile_id"], "displayName": item["display_name"],
            "memberKey": item.get("seed_member_key"),
            "avatar": from_dynamo(item.get("avatar") or {}), "rank": from_dynamo(item.get("rank", 0))}
    if item.get("removed_at"):
        view["removedAt"] = item["removed_at"]
    return view


def members_response(items: list) -> dict:
    """{members: active members, removed: removed ones}, each in rank order.
    Removed members keep all their data; they are listed so they can be restored."""
    views = [member_view(i) for i in items if i.get("sk", "").startswith(MEMBER_PREFIX)]
    order = lambda m: (m["rank"], m["displayName"])  # noqa: E731
    return {"members": sorted((m for m in views if "removedAt" not in m), key=order),
            "removed": sorted((m for m in views if "removedAt" in m), key=order)}


# --- removing and restoring a member ----------------------------------------
# Removal is soft: the member item gets `removed_at` and nothing else changes,
# so its settings, taste, history and My List are all there when it is
# restored. A removed member is hidden from the picker, does not count toward
# MAX_MEMBERS, and every membership check treats it as absent.

def is_active(item: dict | None) -> bool:
    """True for a member item that exists and has not been removed."""
    return bool(item) and not item.get("removed_at")


def check_remove(members: list, profile_id: str) -> dict:
    """The member to remove. Raises MemberError (404 unknown, 409 last profile)."""
    target = next((m for m in members if m.get("profile_id") == profile_id), None)
    if not is_active(target):
        raise MemberError(404, "profile not found")
    if sum(1 for m in members if is_active(m)) <= 1:
        raise MemberError(409, "a household needs at least one profile")
    return target


def check_restore(members: list, profile_id: str) -> dict:
    """The removed member to restore. Raises MemberError (404, or 409 at the
    member limit or when an active profile now has the same name)."""
    target = next((m for m in members if m.get("profile_id") == profile_id), None)
    if not target or is_active(target):
        raise MemberError(404, "no removed profile with that id")
    active = [m for m in members if is_active(m)]
    if len(active) >= MAX_MEMBERS:
        raise MemberError(409, f"a household can have at most {MAX_MEMBERS} profiles; remove one first")
    name = target.get("display_name", "").casefold()
    if any(m.get("display_name", "").casefold() == name for m in active):
        raise MemberError(409, "another profile already uses that name; rename it first")
    return target


def split_rows(cache_items: list, prefix: str = P13N_PREFIX) -> tuple[list, list]:
    """(rows under `prefix` with it stripped from `sk`, rows under no generated prefix).

    Rows under the other generated prefixes (P13N#, CONN#) are in neither
    list, so cold start never sees or deletes them. Stripped rows have the
    seed shape, so home_title_ids / home_response read either set the same way.
    """
    generated, other = [], []
    for item in cache_items:
        if item["sk"].startswith(prefix):
            generated.append({**item, "sk": item["sk"][len(prefix):]})
        elif not item["sk"].startswith(GENERATED_PREFIXES):
            other.append(item)
    return generated, other


def home_title_ids(cache_items: list) -> list:
    ids = []
    for item in cache_items:
        if item["sk"] == HERO_SK:
            ids.append(item["title_id"])
        elif item["sk"].startswith(ROWS_PREFIX):
            ids.extend(i["title_id"] for i in item.get("items", []))
    return list(dict.fromkeys(ids))


def home_response(profile_id: str, cache_items: list, movies_by_id: dict) -> dict:
    """{profileId, hero, rails}. Titles missing from the catalog are dropped, as get_carousels does."""
    hero = None
    rails = []
    for item in cache_items:
        if item["sk"] == HERO_SK:
            hero = movies_by_id.get(item["title_id"])
        elif item["sk"].startswith(ROWS_PREFIX):
            movies = []
            for entry in item.get("items", []):
                movie = movies_by_id.get(entry["title_id"])
                if movie:
                    movies.append({**movie, "blurb": entry["blurb"]} if entry.get("blurb") else movie)
            if movies:
                rails.append({"key": item["sk"][len(ROWS_PREFIX):], "displayTitle": item.get("display_title"),
                              "rank": from_dynamo(item.get("rank", 99)), "movies": movies})
    rails.sort(key=lambda r: (r["rank"], r["key"]))
    for r in rails:
        r.pop("rank")
    return {"profileId": profile_id, "hero": hero, "rails": rails}
