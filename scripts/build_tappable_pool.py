#!/usr/bin/env python3
"""Embed the pre-generated tappable options into the shared layer's pool file.

    OPENSEARCH_HOST=... OPENSEARCH_INDEX=catalog AWS_REGION=<region> \
      .venv/bin/python scripts/build_tappable_pool.py [--region us-east-1]
    .venv/bin/python scripts/build_tappable_pool.py --local [--region us-east-1]

Reads  infra/deployment/seed/tappable_options.json  (text + catalog genre tags)
Writes infra/layers/shared/python/tappable_pool.json (same + float32 vectors)

Run it offline when the option list or the catalog changes; nothing here runs
per viewer. Each option text is embedded once (the agent's query-side
purpose, TEXT_RETRIEVAL) and run as a kNN search over the catalog; the
option's stored vector is the mean of its TOP_K matching title embeddings.
That puts options in the same space as the taste centroids (which are means
of title embeddings), so option-to-taste cosine separates tastes far better
than raw query text does. An option with fewer than MIN_MATCHES catalog
matches fails the build.

--local needs no search collection: it embeds the repository catalog
(data/catalog, document-side, exactly as infra/deployment/load_catalog.py
indexes it) and runs the same filtered cosine kNN in memory. Only Bedrock
InvokeModel is called.
"""

import argparse
import base64
import hashlib
import json
import math
import struct
import sys

sys.dont_write_bytecode = True  # the layer dir is a CDK asset; bytecode there changes its hash

from pathlib import Path  # noqa: E402

import boto3  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "infra/deployment/seed/tappable_options.json"
GENRES = ROOT / "infra/lambdas/profile_settings/genres.json"
OUT = ROOT / "infra/layers/shared/python/tappable_pool.json"
MODEL = "amazon.nova-2-multimodal-embeddings-v1:0"
DIMENSION = 1024
TOP_K = 20
MIN_MATCHES = 8


def embed(client, text):
    body = {"taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {"embeddingPurpose": "TEXT_RETRIEVAL", "embeddingDimension": DIMENSION,
                                      "text": {"truncationMode": "END", "value": text}}}
    resp = client.invoke_model(body=json.dumps(body), modelId=MODEL,
                               accept="application/json", contentType="application/json")
    return json.loads(resp["body"].read())["embeddings"][0]["embedding"]


def option_id(text):
    return hashlib.sha256(text.lower().encode()).hexdigest()[:10]


def knn(query_vector, genres):
    """Embeddings of the option's closest catalog titles. When the option is
    tagged with genres, matches must carry at least one of them."""
    sys.path.insert(0, str(ROOT / "infra/layers/shared/python"))
    import ibc_common  # noqa: PLC0415 -- needs OPENSEARCH_HOST/INDEX in the environment
    knn_q = {"vector": query_vector, "k": TOP_K * 3}
    if genres:
        knn_q["filter"] = {"bool": {"should": [{"match_phrase": {"genres": g}} for g in genres]}}
    resp = ibc_common._search({"size": TOP_K, "_source": ["embedding"], "query": {"knn": {"embedding": knn_q}}})
    return [h["_source"]["embedding"] for h in resp["hits"]["hits"] if h["_source"].get("embedding")]


class LocalCatalog:
    """In-memory stand-in for the catalog index: load_catalog documents + embeddings."""

    def __init__(self, client, cache=None):
        sys.path.insert(0, str(ROOT / "infra/deployment"))
        import load_catalog as lc  # noqa: PLC0415
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
        self.docs = [lc.to_document(r, "local") for r in lc.load_records()]
        stored = json.loads(Path(cache).read_text()) if cache and Path(cache).exists() else {}

        def doc_vector(doc):
            if doc["title_id"] in stored:
                return stored[doc["title_id"]]
            resp = client.invoke_model(body=json.dumps(lc.embedding_body(doc["title_text"])), modelId=MODEL,
                                       accept="application/json", contentType="application/json")
            return json.loads(resp["body"].read())["embeddings"][0]["embedding"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.vectors = list(pool.map(doc_vector, self.docs))
        if cache:
            Path(cache).write_text(json.dumps({d["title_id"]: v for d, v in zip(self.docs, self.vectors)}))
        self.units = [mean_unit([v]) for v in self.vectors]

    def knn(self, query_vector, genres):
        q = mean_unit([query_vector])
        scored = [(sum(a * b for a, b in zip(q, u)), i) for i, u in enumerate(self.units)
                  if not genres or set(genres) & set(self.docs[i]["genres"])]
        return [self.vectors[i] for _, i in sorted(scored, reverse=True)[:TOP_K]]


def mean_unit(vectors):
    units = []
    for v in vectors:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        units.append([x / n for x in v])
    s = [sum(col) for col in zip(*units)]
    n = math.sqrt(sum(x * x for x in s)) or 1.0
    return [x / n for x in s]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--local", action="store_true", help="kNN over the repository catalog, no search collection")
    ap.add_argument("--embedding-cache", help="With --local: JSON file reused for catalog title embeddings")
    args = ap.parse_args()
    options = json.loads(SOURCE.read_text())["options"]
    vocab = {g["id"] for g in json.loads(GENRES.read_text())["genres"]}
    texts = [o["text"] for o in options]
    if len(set(t.lower() for t in texts)) != len(texts):
        sys.exit("duplicate option text")
    for o in options:
        unknown = set(o.get("genres") or []) - vocab
        if unknown:
            sys.exit(f"{o['text']!r}: unknown genres {sorted(unknown)}")
    if sum(1 for o in options if o.get("generic")) < 3 or sum(1 for o in options if o.get("generic") and o.get("kids")) < 3:
        sys.exit("need at least 3 generic and 3 generic kids-safe options")
    client = boto3.client("bedrock-runtime", region_name=args.region)
    search = LocalCatalog(client, args.embedding_cache).knn if args.local else knn
    out = []
    for o in options:
        q = embed(client, o["text"])
        hits = search(q, o.get("genres") or [])
        if len(hits) < MIN_MATCHES:
            sys.exit(f"{o['text']!r}: only {len(hits)} catalog matches")
        v = mean_unit(hits)
        out.append({"id": option_id(o["text"]), "text": o["text"], "genres": o.get("genres") or [],
                    "kids": bool(o.get("kids")), "generic": bool(o.get("generic")),
                    "v": base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode("ascii")})
    OUT.write_text(json.dumps({"model": MODEL, "space": f"mean of top {TOP_K} catalog title embeddings",
                               "dimension": DIMENSION, "options": out}, separators=(",", ":")) + "\n")
    print(f"wrote {len(out)} options to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
