"""Pure profile helpers for the insights API (no I/O, unit-tested).

`taste_lines` reproduces the taste text the movie agent builds from L2
(backend/movie-ai-agent-v2/user_profile.render_affinities) so the console can
show exactly what the agent reads. insights/tests/test_profile.py checks the
two produce identical text, so a change on the agent side fails the tests.
"""

import base64
import math
import struct
from datetime import datetime, timezone

TASTE_MIN_N = 3
TASTE_TOP_POSITIVE = 8
TASTE_TOP_NEGATIVE = 3
HALF_LIFE_DAYS = {"genre": 30, "person": 60}
DAY_START_H = 5
EVENING_H = 17
BUCKET_MIN_VIEWS = 3
BUCKET_NAMES = {"weekday|daytime": "Weekday daytime", "weekday|evening": "Weekday evenings",
                "weekend|daytime": "Weekend daytime", "weekend|evening": "Weekend evenings"}


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def decode_centroids(stored):
    """AFFINITIES.centroids ({v: base64 float32 LE, w, n}) -> [{vector, weight, count}]."""
    out = []
    for c in stored or []:
        try:
            raw = base64.b64decode(c["v"])
            if len(raw) < 4:
                continue
            out.append({"vector": list(struct.unpack(f"<{len(raw) // 4}f", raw)),
                        "weight": num(c.get("w")), "count": int(num(c.get("n"), 1))})
        except Exception:  # noqa: BLE001 - a malformed centroid is skipped, not fatal
            continue
    return out


def decayed(key, e, now):
    s = num(e.get("s"))
    last = e.get("last")
    if last is None or now <= num(last):
        return s
    kind = key.split(":", 1)[0]
    return s * math.pow(2.0, -(now - num(last)) / (HALF_LIFE_DAYS.get(kind, 30) * 86400))


def entity_rows(entities, now):
    """[{key, kind, label, s, s_now, n, used}] sorted by faded score; `used` = the agent reads it."""
    rows = []
    for key, e in (entities or {}).items():
        kind = key.split(":", 1)[0]
        s_now = decayed(key, e, now)
        n = int(num(e.get("n")))
        rows.append({"key": key, "kind": kind, "label": str(e.get("label") or key.split(":", 1)[-1]),
                     "s": round(num(e.get("s")), 4), "s_now": round(s_now, 4), "n": n,
                     "used": n >= TASTE_MIN_N and abs(s_now) >= 0.3})
    rows.sort(key=lambda r: -abs(r["s_now"]))
    return rows


def time_bucket(epoch, tz_offset_min):
    if tz_offset_min is None:
        return None
    local = datetime.fromtimestamp(epoch + int(num(tz_offset_min)) * 60 - DAY_START_H * 3600, timezone.utc)
    day = "weekend" if local.weekday() >= 5 else "weekday"
    part = "daytime" if local.hour < EVENING_H - DAY_START_H else "evening"
    return f"{day}|{part}"


def _bucket_line(aff, bucket, banned, now):
    ctx = (aff.get("contexts") or {}).get(bucket or "")
    if not ctx or int(num(ctx.get("views"))) < BUCKET_MIN_VIEWS:
        return ""
    liked = []
    for key, e in (ctx.get("entities") or {}).items():
        label = str(e.get("label") or key.split(":", 1)[-1]).strip()
        if int(num(e.get("n"))) < TASTE_MIN_N or (key.startswith("genre:") and label.lower() in banned):
            continue
        s = decayed(key, e, now)
        if s >= 0.3:
            liked.append((s, label))
    parts = []
    if liked:
        parts.append("mostly " + ", ".join(label for _, label in sorted(liked, key=lambda x: -x[0])[:4]))
    short, long_ = num(ctx.get("short_min")), num(ctx.get("long_min"))
    if short + long_ > 0:
        if long_ / (short + long_) >= 0.7:
            parts.append("full-length titles")
        elif short / (short + long_) >= 0.7:
            parts.append("short clips")
    if not parts:
        return ""
    return f"- {BUCKET_NAMES.get(bucket, bucket)} (now): {'; '.join(parts)}"


def taste_lines(aff, excluded_genres=(), now=None, tz_offset_min=None):
    """Same text as the agent's render_affinities."""
    import time
    now = now or time.time()
    tz = tz_offset_min if tz_offset_min is not None else aff.get("tz_offset_min")
    bucket = time_bucket(now, tz)
    banned = {str(g).strip().lower() for g in excluded_genres or []}
    scored = []
    for key, e in (aff.get("entities") or {}).items():
        if int(num(e.get("n"))) < TASTE_MIN_N:
            continue
        kind = key.split(":", 1)[0]
        label = str(e.get("label") or key.split(":", 1)[-1]).strip()
        if kind == "genre" and label.lower() in banned:
            continue
        scored.append((decayed(key, e, now), label, kind))
    pos = sorted([x for x in scored if x[0] >= 0.3], key=lambda x: -x[0])[:TASTE_TOP_POSITIVE]
    neg = sorted([x for x in scored if x[0] <= -0.3], key=lambda x: x[0])[:TASTE_TOP_NEGATIVE]

    def names(items):
        return ", ".join(f"{label} ({kind})" for _, label, kind in items)

    lines = []
    strong = [x for x in pos if x[0] >= 0.6]
    mild = [x for x in pos if x[0] < 0.6]
    if strong:
        lines.append(f"- Strongly enjoys: {names(strong)}")
    if mild:
        lines.append(f"- Enjoys: {names(mild)}")
    if neg:
        lines.append(f"- Tends to skip: {names(neg)}")
    line = _bucket_line(aff, bucket, banned, now)
    if line:
        lines.append(line)
    notes = [str(n).strip() for n in aff.get("notes") or [] if str(n).strip()]
    if notes:
        lines.append(f"- Seeded persona notes (not from viewing): {'; '.join(notes)}")
    return lines


def contexts_summary(contexts, now, top=4):
    """{bucket: {views, short_min, long_min, top: [label]}} for the time-of-day panel."""
    out = {}
    for bucket, ctx in (contexts or {}).items():
        rows = entity_rows(ctx.get("entities"), now)
        out[bucket] = {"name": BUCKET_NAMES.get(bucket, bucket), "views": int(num(ctx.get("views"))),
                       "short_min": round(num(ctx.get("short_min")), 1), "long_min": round(num(ctx.get("long_min")), 1),
                       "top": [r["label"] for r in rows if r["s_now"] > 0][:top]}
    return out


def place(neighbors, coords, sharpness=25.0):
    """3D position for a vector from its nearest titles: a similarity-weighted
    mean of their map coordinates, so it lands in the right neighbourhood of
    the fixed layout. neighbors = [(title_id, cosine)]."""
    pts = [(coords[t], c) for t, c in neighbors if t in coords]
    if not pts:
        return None
    top = max(c for _, c in pts)
    ws = [math.exp(sharpness * (c - top)) for _, c in pts]
    total = sum(ws)
    return [round(sum(p[i] * w for (p, _), w in zip(pts, ws)) / total, 2) for i in range(3)]


# Same grouping as L2 (infra/lambdas/profile_events/l2.taste_centroids); used
# only to preview taste vectors for a profile that has none stored yet.
TASTE_JOIN_COSINE = 0.55
TASTE_MAX_CENTROIDS = 3
TASTE_EVIDENCE_K = 2.0
TASTE_TITLE_E = 0.8
CENTROID_RATE = 0.1


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
