"""POST /whatif: replay hypothetical activity on a sandbox copy of a member's
profile and show how the agent and the clips feed decide, before and after.

Body: {profile_id, watched: [title_id], reactions: [{id, value: like|more|dislike}],
       excluded_genres?: [..], avoid_themes?: [..], bucket?: "weekday|evening", query?: str}

Everything runs on in-memory copies: the demo's tables, memory and event
stream are only read. The agent's two LLM stages call Bedrock, so their
wording and order can vary between runs, exactly as they do live.
"""

import hashlib
import json
import math
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config

import agent_run
import clips_run
import ibc_common
import l1
import processor
import user_profile
from sandbox import Sandbox, decimal_free

REGION = os.environ.get("AWS_REGION", "us-west-2")
DATA_BUCKET = os.environ.get("DATA_BUCKET", "")
LAYOUT_KEY = os.environ.get("LAYOUT_KEY", "layout/layout.json")
ALLOWED = {u.strip() for u in os.environ.get("INSIGHTS_USERS", "").split(",") if u.strip()}
RUNS_TABLE = os.environ.get("RUNS_TABLE", "")
MAX_INPUTS = 10
DEFAULT_QUERY = "Recommend something for me to watch tonight"
BUCKETS = {"weekday|daytime", "weekday|evening", "weekend|daytime", "weekend|evening"}

_s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                   config=Config(signature_version="s3v4"))
HEADERS = {"Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
           "Access-Control-Allow-Headers": "Content-Type,Authorization",
           "Access-Control-Allow-Methods": "POST,OPTIONS", "Content-Type": "application/json",
           "Cache-Control": "no-store"}


def respond(status, body):
    return {"statusCode": status, "headers": HEADERS, "body": json.dumps(decimal_free(body))}


# --- map placement (same rule as the insights API) ------------------------------

_layout = {"etag": None, "coords": {}, "names": {}, "checked": 0.0}


def coords():
    if time.time() - _layout["checked"] > 60 or not _layout["coords"]:
        head = _s3.head_object(Bucket=DATA_BUCKET, Key=LAYOUT_KEY)
        if head["ETag"] != _layout["etag"]:
            data = json.loads(_s3.get_object(Bucket=DATA_BUCKET, Key=LAYOUT_KEY)["Body"].read())
            _layout.update(etag=head["ETag"], coords={r[0]: r[6:9] for r in data["titles"]},
                           names={r[0]: r[1] for r in data["titles"]})
        _layout["checked"] = time.time()
    return _layout["coords"]


_placed = {}


def place(vector, k=12):
    """(position, nearest [(id, cosine)]) for a vector; memoized per vector."""
    key = hashlib.sha1(json.dumps([round(x, 5) for x in vector]).encode()).hexdigest()
    if key in _placed:
        return _placed[key]
    hits = ibc_common._search({"size": k, "_source": ["title_id"],
                               "query": {"knn": {"embedding": {"vector": vector, "k": k}}}})["hits"]["hits"]
    nb = [(h["_source"]["title_id"], round(2 * h["_score"] - 1, 4)) for h in hits]
    pts = [(coords()[t], c) for t, c in nb if t in coords()]
    pos = None
    if pts:
        top = max(c for _, c in pts)
        ws = [math.exp(25 * (c - top)) for _, c in pts]
        pos = [round(sum(p[i] * w for (p, _), w in zip(pts, ws)) / sum(ws), 2) for i in range(3)]
    _placed[key] = (pos, nb)
    return _placed[key]


def taste_view(sandbox):
    out = []
    for i, c in enumerate(sandbox.centroids()):
        pos, nb = place(c["vector"])
        out.append({"index": i, "weight": c["weight"], "count": c["count"], "position": pos,
                    "neighbors": [{"id": t, "cosine": s} for t, s in nb]})
    return out


# --- time of day -------------------------------------------------------------------

def moment_in(bucket, tz):
    """A recent timestamp that falls in `bucket` for the member's UTC offset (now when it already does)."""
    now = time.time()
    if not bucket or tz is None:
        return now
    for h in range(0, 24 * 8):
        ts = now - h * 3600
        if user_profile.time_bucket(ts, tz) == bucket:
            return ts
    return now


# --- the run ---------------------------------------------------------------------

def _entities(item):
    meta = processor.catalog(item)
    return l1.entities_for(meta) if meta else {}


def simulate(body, emitter=None):
    pid = body.get("profile_id") or ""
    if not pid.startswith("p-"):
        return 400, {"error": "profile_id is required"}
    watched = [w for w in (body.get("watched") or []) if isinstance(w, str)][:MAX_INPUTS]
    reactions = [(r.get("id"), r.get("value")) for r in (body.get("reactions") or [])
                 if isinstance(r, dict) and r.get("value") in ("like", "more", "dislike")][:MAX_INPUTS]
    bucket = body.get("bucket") if body.get("bucket") in BUCKETS else None
    query = (body.get("query") or DEFAULT_QUERY).strip()[:300]

    base = Sandbox.load(pid, os.environ["PROFILES_TABLE"], os.environ["INTERACTIONS_TABLE"])
    what = base.clone()
    if body.get("excluded_genres") is not None or body.get("avoid_themes") is not None:
        what.set_declared(body.get("excluded_genres"), body.get("avoid_themes"))
    tz = base.affinities.get("tz_offset_min")
    now = moment_in(bucket, tz)
    processor.prefetch_catalog(watched + [r[0] for r in reactions])
    kinds = {t: t.startswith("md") for t in watched}

    steps = []
    plan = [("view", t, None) for t in watched] + [("feedback", t, v) for t, v in reactions]
    for i, (kind, item, value) in enumerate(plan):
        before_ent = {k: dict(v) for k, v in (what.affinities.get("entities") or {}).items()}
        before_cents = [(c["count"], c["vector"]) for c in what.centroids()]
        e, _ = what.apply(kind, item, value, ts=now + i, bucket=bucket or user_profile.time_bucket(now, tz),
                          is_microdrama=kinds.get(item, False))
        after = what.affinities.get("entities") or {}
        labels = _entities(item)
        deltas = [{"key": k, "label": lab, "before": round(float((before_ent.get(k) or {}).get("s") or 0), 4),
                   "after": round(float((after.get(k) or {}).get("s") or 0), 4),
                   "n": int((after.get(k) or {}).get("n") or 0)} for k, lab in labels.items()]
        cents = what.centroids()
        action, moved = "none", None
        if e is not None and e > 0 and cents:
            unchanged = lambda v: any(max(abs(a - b) for a, b in zip(v, ov)) < 1e-6 for _, ov in before_cents)  # noqa: E731
            moved = next((j for j, c in enumerate(cents) if not unchanged(c["vector"])), None)
            action = ("new_vector" if len(cents) > len(before_cents) and before_cents
                      else "seeded" if not before_cents else "joined")
        steps.append({"index": i, "kind": kind, "item": item, "value": value, "e": e,
                      "action": action, "vector": moved, "entities": deltas,
                      "taste_vectors": taste_view(what), "version": what.affinities.get("version")})
        if emitter:
            emitter.emit("profile_step", "whatif", total=len(plan), **steps[-1])

    ltm_text, ltm_records = "", []
    try:
        ltm_text, ltm_records = agent_run.long_term_memory(os.environ.get("AGENTCORE_MEMORY_ID"), pid, REGION)
    except Exception as exc:  # noqa: BLE001 - memory is optional context
        print(f"[whatif] ltm read failed: {exc!r}")
    if emitter:
        emitter.emit("memory", "whatif", records=ltm_records)

    with ThreadPoolExecutor(max_workers=4) as pool:
        f = {
            "base_agent": pool.submit(agent_run.run, base, query, time.time(), ltm_text, emitter, "baseline"),
            "what_agent": pool.submit(agent_run.run, what, query, now, ltm_text, emitter, "whatif"),
            "base_clips": pool.submit(clips_run.run, base, []),
            "what_clips": pool.submit(clips_run.run, what, reactions),
        }
        results = {}
        for k, fut in f.items():
            try:
                results[k] = fut.result()
            except Exception as exc:  # noqa: BLE001 - show the other recommender even if one fails
                print(f"[whatif] {k} failed: {exc!r}")
                results[k] = {"error": "this recommender failed for this scenario"}
    for k in ("base_clips", "what_clips"):
        for x in results[k].get("interests") or []:
            x["position"] = place(x.pop("vector"))[0]
    if emitter:
        c = results["what_clips"]
        emitter.emit("clips_done", "whatif", confidence=c.get("confidence"), pages=c.get("pages"), seconds=c.get("seconds"),
                     interests=[{k: x.get(k) for k in ("kind", "weight", "sources", "position")} for x in c.get("interests") or []])

    def side(sb, agent, clips):
        return {"taste_vectors": taste_view(sb), "version": sb.affinities.get("version"),
                "declared": {k: sb.declared.get(k) for k in ("excluded_genres", "avoid_themes")},
                "watched": sb.completed(), "agent": agent, "clips": clips}

    return 200, {"profile_id": pid, "query": query, "bucket": bucket or user_profile.time_bucket(now, tz),
                 "ltm": ltm_records, "steps": steps,
                 "baseline": side(base, results["base_agent"], results["base_clips"]),
                 "whatif": side(what, results["what_agent"], results["what_clips"])}


def worker(event):
    """Background run: stream events, then store the full result as the final event."""
    from stream import Emitter
    em = Emitter(RUNS_TABLE, event["run_id"])
    try:
        em.emit("run_start", body=event["body"])
        status, out = simulate(event["body"], em)
        if status == 200:
            em.emit("done", result=decimal_free(out))
        else:
            em.emit("failed", error=out.get("error"))
    except Exception as exc:  # noqa: BLE001
        print(f"[whatif] worker failed: {exc!r}")
        em.emit("failed", error="the scenario could not be run")
    finally:
        em.close()
    return {"run_id": event["run_id"]}


def lambda_handler(event, context):
    if (event or {}).get("warmup"):
        coords()          # keeps the container (and its loaded modules + layout) warm
        return {"warm": True}
    if (event or {}).get("worker"):
        return worker(event)
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    user = claims.get("cognito:username")
    if not user or not ("*" in ALLOWED or user in ALLOWED):
        return respond(403, {"error": "not allowed"})
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return respond(400, {"error": "invalid JSON"})
    if body.get("stream") and RUNS_TABLE:
        # Hand the run to an async invocation of this function and return at
        # once; the console polls GET /whatif/events for its progress.
        run_id = uuid.uuid4().hex
        boto3.client("lambda").invoke(FunctionName=context.function_name, InvocationType="Event",
                                      Payload=json.dumps({"worker": True, "run_id": run_id, "body": body}).encode())
        return respond(202, {"run_id": run_id})
    try:
        status, out = simulate(body)
        return respond(status, out)
    except Exception as exc:  # noqa: BLE001
        print(f"[whatif] failed: {exc!r}")
        return respond(500, {"error": "the scenario could not be run"})
