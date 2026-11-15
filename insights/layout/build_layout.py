"""Build the 3D catalog map from the embeddings already in the catalog index.

Runs as a container Lambda on a schedule. Each run fingerprints the catalog's
title ids and rebuilds only when the catalog changed (or `{"force": true}`):

1. read every title's Nova embedding (1024-d) from OpenSearch;
2. UMAP to 3D with cosine distance (fixed seed, so a rebuild of an unchanged
   catalog lands in the same place);
3. k-means on the 3D points, so clusters match what the eye sees;
4. name each cluster from its distinctive genres (labels.py);
5. write layout.json to the insights data bucket.

The map is a projection: neighbourhoods are meaningful, exact distances are
not. Exact similarity is always taken from the 1024-d vectors (the API's kNN).
"""

import json
import os
import time

import boto3
import numpy as np

import catalog
from labels import label_clusters

BUCKET = os.environ.get("DATA_BUCKET", "")
KEY = os.environ.get("LAYOUT_KEY", "layout/layout.json")
CLUSTERS = int(os.environ.get("LAYOUT_CLUSTERS", "18"))
SCALE = 50.0  # map coordinates span roughly [-SCALE, SCALE]

_s3 = boto3.client("s3")


def _current_fingerprint():
    try:
        head = _s3.head_object(Bucket=BUCKET, Key=KEY)
        return head.get("Metadata", {}).get("fingerprint")
    except _s3.exceptions.ClientError:
        return None


def project(vectors, seed=42):
    import umap  # heavy import (numba); only when a rebuild is needed

    x = np.asarray(vectors, dtype=np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True) + 1e-12
    coords = umap.UMAP(n_components=3, n_neighbors=15, min_dist=0.12, metric="cosine",
                       random_state=seed).fit_transform(x)
    coords -= np.median(coords, axis=0)
    # Scale by the 95th percentile, not the max: a few outlying titles would
    # otherwise squeeze the body of the catalog into the middle of the view.
    coords *= SCALE / (np.percentile(np.abs(coords), 95) or 1.0)
    return coords


def cluster(coords, k, seed=42):
    from sklearn.cluster import KMeans

    km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(coords)
    return km.labels_.tolist(), km.cluster_centers_


def build():
    titles = catalog.fetch_all()
    coords = project([t["embedding"] for t in titles])
    assignments, centers = cluster(coords, min(CLUSTERS, len(titles)))
    meta = label_clusters(titles, assignments)
    clusters = [{"id": int(c), "label": meta[c]["label"], "size": meta[c]["size"],
                 "genres": meta[c]["genres"], "examples": meta[c]["examples"],
                 "center": [round(float(v), 2) for v in centers[c]]} for c in sorted(meta)]
    # Compact rows: [id, name, type, year, rating, genres, x, y, z, cluster]
    rows = [[t["title_id"], t["name"], t["type"], t["year"], t["rating"], ", ".join(t["genres"][:3]),
             *[round(float(v), 2) for v in coords[i]], int(assignments[i])] for i, t in enumerate(titles)]
    return {"version": 1, "built_at": int(time.time()), "count": len(rows),
            "columns": ["id", "name", "type", "year", "rating", "genres", "x", "y", "z", "cluster"],
            "clusters": clusters, "titles": rows}


def handler(event, context):
    started = time.time()
    fp = catalog.fingerprint(catalog.fetch_ids())
    if not (event or {}).get("force") and fp == _current_fingerprint():
        return {"rebuilt": False, "fingerprint": fp}
    layout = build()
    layout["fingerprint"] = fp
    _s3.put_object(Bucket=BUCKET, Key=KEY, Body=json.dumps(layout, separators=(",", ":")).encode(),
                   ContentType="application/json", Metadata={"fingerprint": fp})
    return {"rebuilt": True, "fingerprint": fp, "count": layout["count"],
            "clusters": len(layout["clusters"]), "seconds": round(time.time() - started, 1)}
