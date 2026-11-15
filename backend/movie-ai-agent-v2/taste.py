"""L2 taste centroids (design §7.4): grouping, storage encoding, decoding.

Mirrors infra/lambdas/household/household_core.py (taste_centroids,
encode_vector/decode_vector); infra/deployment/test_taste.py checks both agree.
Centroids are stored in the profile table's AFFINITIES item as
[{"v": base64 float32 LE, "w": weight, "n": count}]. When a member has none
stored, the agent derives them from the member's watched titles per session.
"""

from __future__ import annotations

import base64
import math
import struct

TASTE_JOIN_COSINE = 0.55
TASTE_MAX_CENTROIDS = 3
TASTE_EVIDENCE_K = 2.0
TASTE_TITLE_E = 0.8
CENTROID_RATE = 0.1


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


def taste_centroids(vectors: list[list[float]]) -> list[dict]:
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


def encode_vector(v: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode("ascii")


def decode_vector(s: str) -> list[float]:
    raw = base64.b64decode(s)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def decode_stored(stored) -> list[tuple[float, list[float]]]:
    """AFFINITIES.centroids -> [(weight, vector)], skipping malformed entries."""
    out = []
    for c in stored or []:
        try:
            out.append((float(c["w"]), decode_vector(c["v"])))
        except Exception:  # noqa: BLE001
            continue
    return out
