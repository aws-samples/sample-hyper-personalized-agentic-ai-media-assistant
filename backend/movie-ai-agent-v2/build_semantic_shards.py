"""Offline: semantically cluster the non-kids MOVIE catalog into shards.

Pulls each title's stored embedding from OpenSearch, runs balanced k-means with a
token cap (recursive split of oversized clusters + merge of tiny ones), computes a
centroid per cluster, and writes cluster shards + a manifest (centroids, sizes,
title_id -> cluster map) for centroid routing.

Reuses the SAME projection/preamble as build_shards.py so the shards are
interchangeable with the token-balanced ones.

Usage (OPENSEARCH_HOST = the deployment's search collection host, from the
Application stack output SearchEndpoint without "https://"):
    OPENSEARCH_HOST=<collection-host> AWS_REGION=<region> python build_semantic_shards.py
"""

import csv
import json
import numpy as np
from pathlib import Path

from sklearn.cluster import KMeans

import os
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

from build_shards import render, SYSTEM_PREAMBLE, MOVIE_TYPES
from config import OPENSEARCH_HOST, OPENSEARCH_INDEX


def get_opensearch_client() -> OpenSearch:
    """Self-contained OpenSearch Serverless client for the recommendation index
    (relocated from movie-ai-agent; v2 has no shared tools.opensearch_client)."""
    if not OPENSEARCH_HOST:
        raise SystemExit("Set OPENSEARCH_HOST to the search collection host")
    region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    awsauth = AWSV4SignerAuth(boto3.Session().get_credentials(), region, "aoss")
    return OpenSearch(
        hosts=[{"host": OPENSEARCH_HOST, "port": 443}],
        http_auth=awsauth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=300,
    )

# --- tunables ---
SEED = 0
K0 = 7                      # initial cluster count (~ total_tokens / target)
TARGET_TOKENS = 28_000      # split a cluster if it exceeds this
FLOOR_TOKENS = 6_000        # merge a cluster into its nearest neighbor if under this
CHARS_PER_TOKEN = 2.85      # calibrated for the medium projection (build_shards)
OUT_DIR = Path("semantic_shards")
NON_KIDS_CSV = "movie_index_non_kids.csv"

_PREAMBLE_TOK = len(SYSTEM_PREAMBLE) / CHARS_PER_TOKEN


def est_tokens(row: dict) -> float:
    return len(render(row)) / CHARS_PER_TOKEN


def cluster_tokens(members: list[dict]) -> float:
    return _PREAMBLE_TOK + sum(est_tokens(r) for r in members)


def load_movie_rows() -> list[dict]:
    rows = list(csv.DictReader(open(NON_KIDS_CSV, encoding="utf-8")))
    return [r for r in rows if r.get("title_type") in MOVIE_TYPES]


def fetch_embeddings(title_ids: list[str]) -> dict:
    client = get_opensearch_client()
    out = {}
    for i in range(0, len(title_ids), 400):
        chunk = title_ids[i:i + 400]
        resp = client.search(index=OPENSEARCH_INDEX, body={
            "_source": ["title_id", "embedding"],
            "query": {"terms": {"title_id": chunk}},
            "size": len(chunk),
        })
        for h in resp["hits"]["hits"]:
            s = h["_source"]
            if s.get("title_id") and s.get("embedding"):
                out[s["title_id"]] = s["embedding"]
    return out


def l2(a: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(a, axis=-1, keepdims=True)
    return a / np.clip(n, 1e-9, None)


def kmeans_labels(X: np.ndarray, k: int) -> np.ndarray:
    k = min(k, len(X))
    if k <= 1:
        return np.zeros(len(X), dtype=int)
    return KMeans(n_clusters=k, random_state=SEED, n_init=10).fit_predict(X)


def build_clusters(rows, id2vec):
    ids = [r["title_id"] for r in rows if r["title_id"] in id2vec]
    row_by_id = {r["title_id"]: r for r in rows}
    X = l2(np.array([id2vec[t] for t in ids], dtype=np.float64))
    idx_by_id = {t: i for i, t in enumerate(ids)}

    # initial clusters
    labels = kmeans_labels(X, K0)
    groups = {}
    for t, lab in zip(ids, labels):
        groups.setdefault(int(lab), []).append(t)

    # recursive split of oversized clusters
    final = []
    stack = list(groups.values())
    while stack:
        members = stack.pop()
        rws = [row_by_id[t] for t in members]
        if len(members) <= 1 or cluster_tokens(rws) <= TARGET_TOKENS:
            final.append(members)
            continue
        sub = X[[idx_by_id[t] for t in members]]
        sub_labels = kmeans_labels(sub, 2)
        a = [t for t, l in zip(members, sub_labels) if l == 0]
        b = [t for t, l in zip(members, sub_labels) if l == 1]
        if not a or not b:                      # degenerate split -> keep as-is
            final.append(members)
        else:
            stack.extend([a, b])

    # merge tiny clusters into the nearest centroid
    def centroid(members):
        return l2(X[[idx_by_id[t] for t in members]].mean(axis=0, keepdims=True))[0]

    cents = [centroid(m) for m in final]
    changed = True
    while changed and len(final) > 1:
        changed = False
        for i, m in enumerate(final):
            if cluster_tokens([row_by_id[t] for t in m]) < FLOOR_TOKENS:
                # nearest other cluster by centroid cosine
                sims = [(-1 if j == i else float(cents[i] @ cents[j])) for j in range(len(final))]
                j = int(np.argmax(sims))
                final[j] = final[j] + m
                del final[i]
                del cents[i]
                cents[j] = centroid(final[j])
                changed = True
                break

    return ids, X, idx_by_id, row_by_id, final


def main():
    rows = load_movie_rows()
    print(f"movie rows: {len(rows)}")
    id2vec = fetch_embeddings([r["title_id"] for r in rows])
    print(f"embeddings fetched: {len(id2vec)}")

    ids, X, idx_by_id, row_by_id, clusters = build_clusters(rows, id2vec)
    clusters.sort(key=len, reverse=True)

    OUT_DIR.mkdir(exist_ok=True)
    for p in list(OUT_DIR.glob("*.csv")) + list(OUT_DIR.glob("*.system.txt")):
        p.unlink()

    fieldnames = list(rows[0].keys())
    manifest = {"projection": "medium", "seed": SEED, "clusters": []}
    print(f"\n{len(clusters)} clusters:\n")
    for ci, members in enumerate(clusters):
        rws = [row_by_id[t] for t in members]
        cvec = l2(X[[idx_by_id[t] for t in members]].mean(axis=0, keepdims=True))[0]
        toks = int(cluster_tokens(rws))
        sid = f"movie_sem_{ci:02d}"

        with open(OUT_DIR / f"{sid}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
            w.writeheader()
            w.writerows(rws)
        (OUT_DIR / f"{sid}.system.txt").write_text(
            SYSTEM_PREAMBLE + "\n".join(render(r) for r in rws), encoding="utf-8")

        # representative titles = closest members to the centroid
        sims = X[[idx_by_id[t] for t in members]] @ cvec
        order = np.argsort(-sims)
        sample = [rws[k]["originalTitle"] for k in order[:8]]

        manifest["clusters"].append({
            "shard": sid, "size": len(members), "est_tokens": toks,
            "centroid": cvec.tolist(), "title_ids": members})
        print(f"  {sid}: {len(members):4} titles  ~{toks:5} tok")
        print(f"        e.g. {', '.join(sample)}")

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    over = [c["shard"] for c in manifest["clusters"] if c["est_tokens"] > 30000]
    print(f"\nwrote {len(clusters)} clusters -> {OUT_DIR}/manifest.json")
    print(f"over-cap (est): {over or 'none'}")


if __name__ == "__main__":
    main()
