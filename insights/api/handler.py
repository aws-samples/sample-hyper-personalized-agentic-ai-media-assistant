"""Insights API: read-only views of the live recommendation system.

GET /layout              presigned URL of the 3D catalog map (layout.json)
GET /personas            every household member with an L2 profile
GET /taste?profile_id=   a member's L2 taste vectors placed on the map, entity
                         scores, time-of-day taste, watch state, L3 limits and
                         the exact taste text the agent reads
GET /title?id=           a title's details and its true nearest neighbours

Every route is behind the demo's Cognito user pool (imported, not modified) and
an allowlist of usernames (INSIGHTS_USERS). Nothing here writes to the demo.
"""

import hashlib
import json
import os
import time
import urllib.request
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config

import profile as prof

REGION = os.environ.get("AWS_REGION", "us-west-2")
DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
LAYOUT_KEY = os.environ.get("LAYOUT_KEY", "layout/layout.json")
ALLOWED = {u.strip() for u in os.environ.get("INSIGHTS_USERS", "").split(",") if u.strip()}
KNN_K = 12

_session = boto3.Session()
_ddb = boto3.resource("dynamodb", region_name=REGION)
# Regional endpoint + SigV4: a presigned URL on the global s3.amazonaws.com host
# answers with a 307 redirect, which the browser refuses on a CORS request.
_s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                   config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
_media_s3 = boto3.client("s3", region_name=os.environ.get("MEDIA_REGION", REGION))
HOUSEHOLDS = _ddb.Table(os.environ.get("HOUSEHOLDS_TABLE", ""))
PROFILES = _ddb.Table(os.environ.get("PROFILES_TABLE", ""))
INTERACTIONS = _ddb.Table(os.environ.get("INTERACTIONS_TABLE", ""))

HEADERS = {"Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
           "Access-Control-Allow-Headers": "Content-Type,Authorization",
           "Access-Control-Allow-Methods": "GET,OPTIONS", "Content-Type": "application/json",
           "Cache-Control": "no-store"}


def _json(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    raise TypeError(type(v))


def respond(status, body):
    return {"statusCode": status, "headers": HEADERS, "body": json.dumps(body, default=_json)}


# --- catalog (OpenSearch Serverless) -------------------------------------------

def search(body):
    url = f'https://{os.environ["OPENSEARCH_HOST"]}/{os.environ.get("OPENSEARCH_INDEX", "catalog")}/_search'
    data = json.dumps(body).encode()
    req = AWSRequest(method="POST", url=url, data=data, headers={
        "Content-Type": "application/json", "X-Amz-Content-SHA256": hashlib.sha256(data).hexdigest()})
    SigV4Auth(_session.get_credentials(), "aoss", REGION).add_auth(req)
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=dict(req.headers), method="POST"),
                                timeout=20) as resp:
        return json.loads(resp.read())


def knn(vector, k=KNN_K, exclude=None):
    """[(title_id, cosine)] nearest to `vector` in the full 1024-d space."""
    hits = search({"size": k + (1 if exclude else 0), "_source": ["title_id"],
                   "query": {"knn": {"embedding": {"vector": vector, "k": k + 1}}}})["hits"]["hits"]
    # faiss cosinesimil scores are (1 + cos) / 2.
    out = [(h["_source"]["title_id"], round(2 * h["_score"] - 1, 4)) for h in hits]
    return [x for x in out if x[0] != exclude][:k]


def embeddings(title_ids):
    """Catalog vectors for title_ids, in the given order (missing ones dropped)."""
    if not title_ids:
        return []
    hits = search({"size": len(title_ids), "_source": ["title_id", "embedding"],
                   "query": {"terms": {"title_id": list(title_ids)}}})["hits"]["hits"]
    found = {h["_source"]["title_id"]: h["_source"].get("embedding") for h in hits}
    return [found[t] for t in title_ids if found.get(t)]


# --- layout cache -------------------------------------------------------------

_layout = {"etag": None, "checked": 0.0, "coords": {}, "names": {}, "built_at": None, "count": 0}


def layout():
    """Title coordinates from layout.json, re-read when its ETag changes (checked every 60 s)."""
    now = time.time()
    if now - _layout["checked"] < 60 and _layout["coords"]:
        return _layout
    head = _s3.head_object(Bucket=DATA_BUCKET, Key=LAYOUT_KEY)
    _layout["checked"] = now
    if head["ETag"] != _layout["etag"]:
        data = json.loads(_s3.get_object(Bucket=DATA_BUCKET, Key=LAYOUT_KEY)["Body"].read())
        _layout.update(etag=head["ETag"], built_at=data.get("built_at"), count=data.get("count"),
                       coords={r[0]: r[6:9] for r in data["titles"]},
                       names={r[0]: r[1] for r in data["titles"]})
    return _layout


def named(neighbors):
    names = layout()["names"]
    return [{"id": t, "name": names.get(t, t), "cosine": c} for t, c in neighbors]


# --- routes ---------------------------------------------------------------------

def route_layout(_params):
    head = _s3.head_object(Bucket=DATA_BUCKET, Key=LAYOUT_KEY)
    url = _s3.generate_presigned_url("get_object", Params={"Bucket": DATA_BUCKET, "Key": LAYOUT_KEY}, ExpiresIn=300)
    return 200, {"url": url, "fingerprint": head.get("Metadata", {}).get("fingerprint"),
                 "updated_at": head["LastModified"].isoformat()}


def route_personas(_params):
    members, start = [], None
    while True:
        kw = {"ExclusiveStartKey": start} if start else {}
        page = HOUSEHOLDS.scan(**kw)
        members += [i for i in page["Items"] if str(i.get("sk", "")).startswith("MEMBER#")]
        start = page.get("LastEvaluatedKey")
        if not start:
            break
    out = []
    for m in members:
        aff = PROFILES.get_item(Key={"user_id": m["profile_id"], "data_type": "AFFINITIES"}).get("Item")
        out.append({"profile_id": m["profile_id"], "name": m.get("display_name") or m["profile_id"],
                    "household": m.get("account_id"), "has_profile": bool(aff),
                    "version": int(prof.num(aff.get("version"))) if aff else 0,
                    "updated_at": aff.get("updated_at") if aff else None})
    out.sort(key=lambda m: (m["household"] or "", m["name"]))
    return 200, {"personas": out}


def _member_items(profile_id):
    items, start = [], None
    while True:
        kw = {"ExclusiveStartKey": start} if start else {}
        page = INTERACTIONS.query(KeyConditionExpression=Key("profile_id").eq(profile_id), **kw)
        items += page["Items"]
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def route_taste(params):
    pid = (params.get("profile_id") or "").strip()
    if not pid.startswith("p-"):
        return 400, {"error": "profile_id is required"}
    rows = PROFILES.query(KeyConditionExpression=Key("user_id").eq(pid))["Items"]
    by_type = {r["data_type"]: r for r in rows}
    aff = by_type.get("AFFINITIES") or {}
    declared = by_type.get("L3#DECLARED") or {}
    now = time.time()
    coords = layout()["coords"]

    names = layout()["names"]
    items = _member_items(pid)
    watched = []
    for it in items:
        sk = str(it.get("sk", ""))
        if sk.startswith("TITLE#"):
            tid = sk.split("#", 1)[1]
            watched.append({"id": tid, "name": names.get(tid, tid), "position": coords.get(tid),
                            "content_type": it.get("content_type"), "completed": bool(it.get("completed")),
                            "completion": prof.num(it.get("completion")), "last_watched": it.get("last_watched")})
    watched.sort(key=lambda w: w.get("last_watched") or "", reverse=True)
    for w, url in zip(watched, posters([w["id"] for w in watched])):
        w["poster"] = url
    stored = prof.decode_centroids(aff.get("centroids"))
    source = "stored"
    if not stored:
        # Profile seeded before taste vectors existed: preview them from its
        # finished titles with the same grouping L2 uses. Not written anywhere.
        source = "preview"
        done = [w["id"] for w in reversed(watched) if w["completed"] or w["completion"] >= 0.9]
        done = done or [w["id"] for w in reversed(watched)]
        stored = prof.taste_centroids([v for v in embeddings(done) if v])

    taste = []
    for i, c in enumerate(stored):
        nb = knn(c["vector"])
        taste.append({"index": i, "weight": c["weight"], "count": c["count"],
                      "position": prof.place(nb, coords), "neighbors": named(nb)})

    excluded = list(declared.get("excluded_genres") or [])
    return 200, {
        "profile_id": pid, "version": int(prof.num(aff.get("version"))), "updated_at": aff.get("updated_at"),
        "taste_vectors": taste, "taste_source": source, "watched": watched,
        "entities": prof.entity_rows(aff.get("entities"), now)[:40],
        "contexts": prof.contexts_summary(aff.get("contexts"), now),
        "current_bucket": prof.time_bucket(now, aff.get("tz_offset_min")),
        "agent_taste_text": prof.taste_lines(aff, excluded, now),
        "declared": {k: declared.get(k) for k in ["excluded_genres", "avoid_themes", "languages",
                                                   "max_length_minutes", "followed", "subtitles"] if k in declared},
    }


_poster_cache = {}  # s3 uri -> (url, signed_at); reused so polling doesn't reload images


def _poster(image):
    if isinstance(image, str) and image.startswith("s3://"):
        hit = _poster_cache.get(image)
        if hit and time.time() - hit[1] < 3000:
            return hit[0]
        bucket, _, key = image[5:].partition("/")
        url = _media_s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)
        _poster_cache[image] = (url, time.time())
        return url
    return image if isinstance(image, str) and image.startswith("https://") else None


def posters(title_ids):
    """Poster URL per title_id (same order; None when the catalog has none)."""
    if not title_ids:
        return []
    hits = search({"size": len(title_ids), "_source": ["title_id", "image"],
                   "query": {"terms": {"title_id": list(title_ids)}}})["hits"]["hits"]
    found = {h["_source"]["title_id"]: h["_source"].get("image") for h in hits}
    return [_poster(found.get(t)) for t in title_ids]


def route_title(params):
    tid = (params.get("id") or "").strip()
    if not tid:
        return 400, {"error": "id is required"}
    hits = search({"size": 1, "query": {"term": {"title_id": tid}},
                   "_source": ["title_id", "originalTitle", "title_type", "plot", "genres", "imdb_rating",
                               "premiere_year", "principalCastMembers", "principalCrewMembers", "image",
                               "awards", "embedding"]})["hits"]["hits"]
    if not hits:
        return 404, {"error": "unknown title"}
    s = hits[0]["_source"]
    nb = knn(s["embedding"], exclude=tid) if s.get("embedding") else []
    crew = [c for c in s.get("principalCrewMembers") or [] if "(director)" in c]
    return 200, {"id": tid, "name": s.get("originalTitle"), "type": s.get("title_type"), "plot": s.get("plot"),
                 "genres": s.get("genres"), "rating": s.get("imdb_rating"), "year": s.get("premiere_year"),
                 "cast": [c.split(" as ")[0] for c in (s.get("principalCastMembers") or [])[:5]],
                 "directors": [c.split(" as ")[0] for c in crew[:2]],
                 "awards": len(s.get("awards") or []), "poster": _poster(s.get("image")),
                 "neighbors": named(nb)}


ROUTES = {"layout": route_layout, "personas": route_personas, "taste": route_taste, "title": route_title,
          "events": None}


def route_events(params):
    """Progress events of a what-if run after sequence `after` (the console polls this)."""
    run_id = (params.get("run_id") or "").strip()
    if not run_id.isalnum() or len(run_id) != 32:
        return 400, {"error": "run_id is required"}
    after = int(params.get("after") or 0)
    resp = RUNS.query(KeyConditionExpression=Key("run_id").eq(run_id) & Key("seq").gt(after), Limit=200)
    events = [{"seq": int(i["seq"]), "type": i["type"], "side": i.get("side") or None, "t": float(i.get("t") or 0),
               "data": json.loads(i.get("data") or "{}")} for i in resp["Items"]]
    return 200, {"events": events}


ROUTES["events"] = route_events
RUNS = _ddb.Table(os.environ.get("RUNS_TABLE", "unset"))


def authorized(event):
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    user = claims.get("cognito:username") or claims.get("username")
    return bool(user) and ("*" in ALLOWED or user in ALLOWED)


def lambda_handler(event, context):
    route = (event.get("resource") or event.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
    if route not in ROUTES:
        return respond(404, {"error": "not found"})
    if not authorized(event):
        return respond(403, {"error": "not allowed"})
    try:
        status, body = ROUTES[route](event.get("queryStringParameters") or {})
        return respond(status, body)
    except Exception as exc:  # noqa: BLE001 - surface the error to the console
        print(f"[insights] {route} failed: {exc!r}")
        return respond(500, {"error": "internal error"})
