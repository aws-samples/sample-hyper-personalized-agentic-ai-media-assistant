"""clips_feed: POST /clips/session and POST /clips/refill for the clips feed.

The feed is built from the catalog, not from static files: microdramas
(title_type `micro-drama`, playable video from the title video mapping table)
and catalog titles shown as poster cards. Ranking is clips_policy (a port of
the Personalized-Feed-POC policy) over session interests and the member's L2
taste centroids.

Identity comes from the Cognito authorizer: `profile_id` must be a member of
the caller's household (404 otherwise).

POST /clips/session  {profile_id, session_id}
    -> {cards, keep: 0}. A new session_id starts a fresh SESSION#clips item.
POST /clips/refill   {profile_id, session_id, queued: [ids], impressions:
                      [{item_id, dwell_ms, feedback?}]}
    -> {cards, keep}. The client keeps the first `keep` of its queued cards and
    appends `cards`, so the queue is back to BATCH. Strong feedback (more /
    dislike) keeps only the next card and replaces the rest.

SESSION#clips (user-interactions table, 4 h TTL) holds facts only: the
session's impressions (dwell, feedback) and the ids issued. Interests are
rebuilt from them on every request. It is written here synchronously, apart
from SESSION#behavior (written by the stream processor a few seconds later),
so "more like this" shapes the very next refill. The same card reports also
go to POST /events, which feed L1/L2.
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr, Key

import clips_policy as policy
from ibc_common import _search, is_profile_id, resolve_profile_pk, respond

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SK = "SESSION#clips"
SESSION_TTL_S = 4 * 3600
ISSUED_MAX = 300
CACHE_TTL_S = 15 * 60
TITLE_CACHE_MAX = 3000
DISCOVERY_FETCH = 200
MD_FETCH = 100
CAST_SHOWN = 3
KIDS_MPAA_RATINGS = ["g", "tv-y", "tv-y7", "tv-g"]  # TV-PG is parental guidance, not kids
CARD_TYPES = ["movie", "micro-drama"]
FIELDS = ["title_id", "originalTitle", "title_type", "genres", "plot", "image", "imdb_rating",
          "premiere_date", "principalCastMembers", "mpaa_ratings", "embedding"]

_ddb = boto3.resource("dynamodb")
_s3 = boto3.client("s3", region_name=os.environ.get("MEDIA_REGION") or None)
_titles: dict = {}     # id -> (expires, title)
_pools: dict = {}      # name -> (expires, [title])
_videos: dict = {}     # id -> (expires, relative CDN path | None)


def _table(env):
    return _ddb.Table(os.environ[env])


def _native(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _native(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_native(x) for x in v]
    return v


def _dynamo(v):
    return json.loads(json.dumps(v), parse_float=Decimal)


# --- catalog ------------------------------------------------------------------

def to_title(src):
    premiere = str(src.get("premiere_date") or "")
    mpaa = src.get("mpaa_ratings") or []
    return {"id": src["title_id"], "title": src.get("originalTitle"),
            "kind": "microdrama" if src.get("title_type") == "micro-drama" else "title",
            "genres": src.get("genres") or [], "plot": src.get("plot"), "image": src.get("image"),
            "rating": src.get("imdb_rating"), "year": int(premiere[:4]) if premiere[:4].isdigit() else None,
            "cast": [str(c).split(" as (", 1)[0] for c in (src.get("principalCastMembers") or [])[:CAST_SHOWN]],
            "maturity": mpaa[0] if isinstance(mpaa, list) and mpaa else None,
            "vector": src.get("embedding")}


def _remember(titles):
    now = time.time()
    if len(_titles) > TITLE_CACHE_MAX:
        _titles.clear()
    for t in titles:
        _titles[t["id"]] = (now + CACHE_TTL_S, t)
    return titles


def _hits(body):
    return _remember([to_title(h["_source"]) for h in _search(body).get("hits", {}).get("hits", [])])


AWARDED = {"exists": {"field": "awards"}}


def filters(declared):
    """Hard limits for every card: playable card types with a poster, an award
    listing for catalog titles (the catalog has no vote counts, so this is the
    quality floor; microdramas are exempt), the member's excluded genres, and
    the kids limit."""
    fc = [{"terms": {"title_type": CARD_TYPES}}, {"exists": {"field": "image"}},
          {"bool": {"should": [AWARDED, {"term": {"title_type": "micro-drama"}}], "minimum_should_match": 1}}]
    banned = [{"match": {"genres": g}} for g in declared.get("excluded_genres") or [] if g]
    if banned:
        fc.append({"bool": {"must_not": banned}})
    if declared.get("kids_profile"):
        fc.append({"terms": {"mpaa_ratings": KIDS_MPAA_RATINGS}})
    return fc


def knn(vector, fc, k, exclude):
    """Exact cosine KNN within the filters (same query shape as the agent).

    Asks OpenSearch for ids only (a full hit carries a 1024-float embedding,
    so k=150 is ~2.3 MB) and fills the titles from the in-memory cache,
    fetching only the ones it has not seen yet."""
    inner = list(fc) + ([{"bool": {"must_not": [{"terms": {"title_id": sorted(exclude)}}]}}] if exclude else [])
    resp = _search({"_source": ["title_id"], "size": k, "query": {"script_score": {
        "query": {"bool": {"filter": inner}},
        "script": {"lang": "knn", "source": "knn_score",
                   "params": {"field": "embedding", "query_value": vector, "space_type": "cosinesimil"}}}}})
    ids = [h["_source"]["title_id"] for h in resp.get("hits", {}).get("hits", [])]
    found = titles_by_id(ids)
    return [found[i] for i in ids if i in found]


def _pool(name, body=None, fetch=None):
    hit = _pools.get(name)
    if hit and hit[0] > time.time():
        return hit[1]
    titles = fetch() if fetch else _hits(body)
    _pools[name] = (time.time() + CACHE_TTL_S, titles)
    return titles


def microdramas(fc):
    key = "md:" + json.dumps(fc, sort_keys=True)
    return _pool(key, {"_source": FIELDS, "size": MD_FETCH,
                       "query": {"bool": {"filter": fc + [{"term": {"title_type": "micro-drama"}}]}}})


def discovery(fc, priors=(), exclude=(), member=None):
    """The exploration pool (shuffled per batch by the policy).

    With taste: the award-listed titles nearest the member's L2 taste
    centroids (DISCOVERY_FETCH in total, split by centroid weight), so
    exploration varies but stays plausible. Without: the highest-rated
    award-listed titles. Awards stand in for popularity, which the catalog
    does not record.

    With `member`, the taste pool is cached per member for CACHE_TTL_S:
    only `exclude` (the stable watched list) is applied in the search, and
    the policy drops cards already issued this session, so each refill
    reuses it instead of re-running one KNN per centroid."""
    if priors:
        def fetch():
            out = {}
            for w, vector in priors:
                for t in knn(vector, fc + [AWARDED], max(30, round(DISCOVERY_FETCH * w)), exclude):
                    out.setdefault(t["id"], t)
            return list(out.values())
        if not member:
            return fetch()
        shape = [round(w, 3) for w, _ in priors] + sorted(exclude)
        return _pool(f"taste:{member}:" + json.dumps([fc, shape], sort_keys=True), fetch=fetch)
    key = "top:" + json.dumps(fc, sort_keys=True)
    return _pool(key, {"_source": FIELDS, "size": DISCOVERY_FETCH,
                       "query": {"bool": {"filter": fc + [AWARDED, {"exists": {"field": "imdb_rating"}}]}},
                       "sort": [{"imdb_rating": {"order": "desc"}}]})


def titles_by_id(ids):
    now = time.time()
    out = {i: _titles[i][1] for i in ids if i in _titles and _titles[i][0] > now}
    missing = [i for i in ids if i not in out]
    if missing:
        for t in _hits({"_source": FIELDS, "size": len(missing), "query": {"terms": {"title_id": missing}}}):
            out[t["id"]] = t
    return out


# --- media ----------------------------------------------------------------------

def video_source(title_id):
    """(CDN path, s3 URL) of a title's video from the title video mapping
    table, or (None, None). The path is set only for files under
    CLIP_MEDIA_PREFIX, which the media CDN serves."""
    hit = _videos.get(title_id)
    if hit and hit[0] > time.time():
        return hit[1]
    prefix = os.environ.get("CLIP_MEDIA_PREFIX", "")
    found = (None, None)
    try:
        item = _table("VIDEO_MAPPING_TABLE").get_item(Key={"title_id": title_id}).get("Item") or {}
        url = item.get("video_s3_url") or ""
        if url.startswith("s3://"):
            found = (url[len(prefix):] if prefix and url.startswith(prefix) else None, url)
    except Exception:  # noqa: BLE001 -- a card without video falls back to its poster
        logger.exception(f"video lookup failed for {title_id}")
    _videos[title_id] = (time.time() + CACHE_TTL_S, found)
    return found


def presign(url, expires=3600):
    if not isinstance(url, str) or not url.startswith("s3://"):
        return url
    bucket, _, key = url[5:].partition("/")
    try:
        return _s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires)
    except Exception:  # noqa: BLE001
        logger.exception("presign failed")
        return None


def card(title, reason, explanation):
    """A feed card. A microdrama with a mapped video plays it: `videoPath` on
    the media CDN (the frontend adds its token) when the deployment has one,
    else `videoUrl`, a presigned S3 URL. Every other title is a poster card."""
    path, s3_url = video_source(title["id"]) if title["kind"] == "microdrama" else (None, None)
    return {"id": title["id"], "kind": "microdrama" if s3_url else "title_card", "title": title["title"],
            "genres": title["genres"], "plot": title["plot"], "year": title["year"], "rating": title["rating"],
            "maturity": title["maturity"], "cast": title["cast"], "posterUrl": presign(title["image"]),
            "videoPath": path, "videoUrl": presign(s3_url), "reason": reason, "explanation": explanation}


# --- member context -------------------------------------------------------------

def member_context(profile_id):
    """(priors, declared, watched ids): L2 centroids, L3 limits, completed titles.
    The three reads are independent, so they run in parallel."""
    profiles = _table("PROFILES_TABLE")

    def get(data_type):
        return profiles.get_item(Key={"user_id": profile_id, "data_type": data_type}).get("Item") or {}

    def completed():
        watched, kwargs = [], {"KeyConditionExpression": Key("profile_id").eq(profile_id) & Key("sk").begins_with("TITLE#"),
                               "FilterExpression": Attr("completed").eq(True), "ProjectionExpression": "sk"}
        while True:
            resp = _table("INTERACTIONS_TABLE").query(**kwargs)
            watched += [i["sk"][len("TITLE#"):] for i in resp.get("Items", [])]
            if "LastEvaluatedKey" not in resp:
                return watched
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        aff_f, dec_f, watched_f = pool.submit(get, "AFFINITIES"), pool.submit(get, "L3#DECLARED"), pool.submit(completed)
        aff, declared, watched = aff_f.result(), _native(dec_f.result()), watched_f.result()
    return policy.decode_centroids(_native(aff.get("centroids"))), declared, watched


def load_session(profile_id, session_id):
    item = _native(_table("INTERACTIONS_TABLE").get_item(
        Key={"profile_id": profile_id, "sk": SK}, ConsistentRead=True).get("Item") or {})
    if item.get("session_id") != session_id:
        return {"session_id": session_id, "impressions": {}, "issued": []}
    return {"session_id": session_id, "impressions": item.get("impressions") or {}, "issued": item.get("issued") or []}


def save_session(profile_id, state, now):
    _table("INTERACTIONS_TABLE").put_item(Item=_dynamo({
        "profile_id": profile_id, "sk": SK, "session_id": state["session_id"],
        "impressions": state["impressions"], "issued": state["issued"][-ISSUED_MAX:],
        "updated_at": int(now), "expires_at": int(now + SESSION_TTL_S)}))


# --- feed -----------------------------------------------------------------------

def build(profile_id, session_id, queued=(), updates=(), now=None):
    """One feed response: {cards, keep}."""
    now = now or time.time()
    # The session item and the member's context are independent reads.
    with ThreadPoolExecutor(max_workers=2) as pool:
        state_f, ctx_f = pool.submit(load_session, profile_id, session_id), pool.submit(member_context, profile_id)
        state, (priors, declared, watched) = state_f.result(), ctx_f.result()
    state["impressions"] = policy.merge_impressions(state["impressions"], updates, now)
    queued = [q for q in queued if isinstance(q, str)]
    keep = policy.keep_count(queued, updates)
    kept, dropped = queued[:keep], set(queued[keep:])
    fc = filters(declared)

    seen = titles_by_id(list(state["impressions"]) + kept)
    positives, negatives = policy.rebuild(state["impressions"], seen, now)
    ints = policy.interests(positives, priors)
    exclude = (set(state["issued"]) - dropped) | set(watched) | set(state["impressions"]) | set(kept)

    with ThreadPoolExecutor(max_workers=6) as pool:
        searches = [pool.submit(knn, x["v"], fc, policy.PRIOR_SEARCH_K if x.get("prior")
                                else policy.SESSION_SEARCH_K, exclude) for x in ints]
        md_f, top_f = pool.submit(microdramas, fc), pool.submit(discovery, fc, priors, set(watched), profile_id)
        hits = [f.result() for f in searches]
        mds, top = md_f.result(), top_f.result()

    picks = policy.select(ints, negatives, hits, mds, top, policy.BATCH - len(kept), exclude,
                          anchors=[seen[i] for i in kept if i in seen],
                          seed=f"{session_id}:{len(state['issued'])}")
    cards = [card(t, reason, why) for t, reason, why in picks]
    state["issued"] = [i for i in state["issued"] if i not in dropped] + [c["id"] for c in cards]
    save_session(profile_id, state, now)
    logger.info(json.dumps({"event": "clips_feed", "profile": profile_id, "cards": len(cards), "keep": keep,
                            "interests": [round(x["w"], 3) for x in ints], "priors": len(priors),
                            "microdramas": sum(c["kind"] == "microdrama" for c in cards)}))
    return {"cards": cards, "keep": keep}


def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return respond(400, {"error": "invalid JSON"})
    profile_id, session_id = body.get("profile_id"), body.get("session_id")
    if not is_profile_id(profile_id) or not isinstance(session_id, str) or not 0 < len(session_id) <= 128:
        return respond(400, {"error": "profile_id and session_id are required"})
    if not resolve_profile_pk(event, None, profile_id):
        return respond(404, {"error": "profile not found"})
    path = event.get("resource") or event.get("path") or ""
    try:
        if path.endswith("/session"):
            return respond(200, build(profile_id, session_id))
        if path.endswith("/refill"):
            updates = [u for u in body.get("impressions") or [] if isinstance(u, dict)][:policy.IMPRESSIONS_MAX]
            return respond(200, build(profile_id, session_id, body.get("queued") or [], updates))
        return respond(404, {"error": "not found"})
    except Exception:  # noqa: BLE001
        logger.exception("clips feed failed")
        return respond(500, {"error": "internal error"})
