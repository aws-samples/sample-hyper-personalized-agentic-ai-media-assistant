"""Self-contained OpenSearch retrieval primitives for the catch-all path.

Deliberately does NOT reuse the v1 tools/ package: the validated pipeline only
needs a signed client, a TEXT_RETRIEVAL query embedder, a hybrid KNN+BM25
search, and a fetch-by-ids for rehydration. Keeping them here makes v2
self-contained for AgentCore direct-code deploy.
"""

from __future__ import annotations

import json

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

from config import (
    AWS_REGION, OPENSEARCH_HOST, OPENSEARCH_INDEX,
    EMBEDDING_MODEL_ID, EMBEDDING_DIMENSION, EMBED_QUERY_PURPOSE, EMBEDDING_REGION,
    CATCHALL_SIZE, RRF_K, TASTE_K, TASTE_WEIGHT,
)

# Fields returned for display / rehydration (no embeddings, no keywords).
DISPLAY_FIELDS = [
    "title_id", "originalTitle", "image", "imdb_rating",
    "premiere_date", "principalCastMembers", "genres", "plot",
]

_os_client = None
_bedrock = None


def get_client() -> OpenSearch:
    """Singleton IAM-signed OpenSearch Serverless (aoss) client."""
    global _os_client
    if _os_client is None:
        creds = boto3.Session().get_credentials()
        auth = AWSV4SignerAuth(creds, AWS_REGION, "aoss")
        _os_client = OpenSearch(
            hosts=[{"host": OPENSEARCH_HOST, "port": 443}],
            http_auth=auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=300,
        )
    return _os_client


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=EMBEDDING_REGION)
    return _bedrock


def embed_query(text: str) -> list[float]:
    """Embed query text for KNN. Uses EMBED_QUERY_PURPOSE (TEXT_RETRIEVAL) —
    the query-side pair for the index's document embeddings."""
    if EMBEDDING_MODEL_ID == "amazon.titan-embed-text-v2:0":
        body = {"inputText": text, "dimensions": EMBEDDING_DIMENSION, "normalize": True}
        resp = _get_bedrock().invoke_model(
            body=json.dumps(body), modelId=EMBEDDING_MODEL_ID,
            accept="application/json", contentType="application/json",
        )
        return json.loads(resp["body"].read())["embedding"]
    body = {
        "taskType": "SINGLE_EMBEDDING",
        "singleEmbeddingParams": {
            "embeddingPurpose": EMBED_QUERY_PURPOSE,
            "embeddingDimension": EMBEDDING_DIMENSION,
            "text": {"truncationMode": "END", "value": text},
        },
    }
    resp = _get_bedrock().invoke_model(
        body=json.dumps(body), modelId=EMBEDDING_MODEL_ID,
        accept="application/json", contentType="application/json",
    )
    return json.loads(resp["body"].read())["embeddings"][0]["embedding"]


# Title types that are never recommended: a catalog can carry video games,
# and this is a watching app. Applied to every agent search (catch_all, taste,
# top-rated, the relaxation ladder); fan-out shards leave them out offline.
NEVER_RECOMMEND_TYPES = ["videoGame"]


def _filter_clauses(filters: list[dict] | None, movie_only: bool,
                    exclude_ids: list[str] | None = None) -> list[dict]:
    fc: list[dict] = [{"bool": {"must_not": [{"terms": {"title_type": NEVER_RECOMMEND_TYPES}}]}}]
    if movie_only:
        fc.append({"term": {"title_type": "movie"}})
    if filters:
        fc.extend(filters)
    if exclude_ids:
        fc.append({"bool": {"must_not": [{"terms": {"title_id": list(exclude_ids)}}]}})
    return fc


def _knn_vector(qv: list[float], fc: list[dict], size: int) -> list[dict]:
    """Exact cosine KNN (script_score) on a query vector, within the filters."""
    inner = {"filter": fc} if fc else {}
    body = {
        "_source": DISPLAY_FIELDS, "size": size,
        "query": {"bool": {"should": [{"script_score": {
            "query": {"bool": inner} if inner else {"match_all": {}},
            "script": {"lang": "knn", "source": "knn_score",
                       "params": {"field": "embedding", "query_value": qv,
                                  "space_type": "cosinesimil"}}}}],
            "minimum_should_match": 1}},
    }
    return [h["_source"] for h in get_client().search(index=OPENSEARCH_INDEX, body=body)["hits"]["hits"]]


def _knn_only(query_text: str, fc: list[dict], size: int) -> list[dict]:
    return _knn_vector(embed_query(query_text or "popular acclaimed title"), fc, size)


def _bm25_only(query_text: str, fc: list[dict], size: int) -> list[dict]:
    body = {
        "_source": DISPLAY_FIELDS, "size": size,
        "query": {"bool": {
            "must": [{"multi_match": {
                "query": query_text or "popular acclaimed title",
                "fields": ["title_text", "plot", "originalTitle^2", "genres"]}}],
            "filter": fc}},
    }
    return [h["_source"] for h in get_client().search(index=OPENSEARCH_INDEX, body=body)["hits"]["hits"]]


def fuse(lists: list[tuple[float, list[dict]]], size: int) -> list[dict]:
    """Weighted Reciprocal Rank Fusion: score = sum(weight / (RRF_K + rank))."""
    scores: dict[str, float] = {}
    src: dict[str, dict] = {}
    for weight, hits in lists:
        for rank, h in enumerate(hits):
            tid = h.get("title_id")
            if not tid:
                continue
            scores[tid] = scores.get(tid, 0.0) + weight / (RRF_K + rank)
            src.setdefault(tid, h)
    ranked = sorted(scores, key=scores.get, reverse=True)[:size]
    return [src[tid] for tid in ranked]


def knn_search(query_text: str, filters: list[dict] | None = None,
               size: int = CATCHALL_SIZE, movie_only: bool = True,
               taste: list[tuple[float, list[float]]] | None = None,
               exclude_ids: list[str] | None = None) -> list[dict]:
    """Hybrid retrieval: TEXT_RETRIEVAL KNN + BM25 lexical, fused with Reciprocal
    Rank Fusion (RRF). Final candidate count is `size` (default CATCHALL_SIZE).

    `taste` adds one KNN list per member taste centroid (TASTE_K hits, within the
    same filters). Each list averages TASTE_WEIGHT, scaled by the centroid's
    share of the member's viewing, so titles
    near both the request and the member's taste rise. `exclude_ids` are kept
    out of every list.
    """
    fc = _filter_clauses(filters, movie_only, exclude_ids)
    lists = [(1.0, _knn_only(query_text, fc, size)), (1.0, _bm25_only(query_text, fc, size))]
    for w, vec in taste or []:
        lists.append((TASTE_WEIGHT * w * len(taste), _knn_vector(vec, fc, TASTE_K)))
    return fuse(lists, size)


def taste_search(taste: list[tuple[float, list[float]]], size: int,
                 movie_only: bool = True, exclude_ids: list[str] | None = None,
                 filters: list[dict] | None = None) -> list[dict]:
    """Retrieval from the member's taste centroids alone ("surprise me"),
    each centroid's list weighted by its share of the member's viewing."""
    fc = _filter_clauses(filters, movie_only, exclude_ids)
    return fuse([(w, _knn_vector(vec, fc, size)) for w, vec in taste], size)


def top_rated(size: int, movie_only: bool = True, exclude_ids: list[str] | None = None,
              filters: list[dict] | None = None) -> list[dict]:
    """Highest-rated catalog titles (cold-start "surprise me": no taste yet)."""
    fc = _filter_clauses(filters, movie_only, exclude_ids)
    body = {"_source": DISPLAY_FIELDS, "size": size,
            "query": {"bool": {"filter": fc + [{"exists": {"field": "imdb_rating"}}]}},
            "sort": [{"imdb_rating": {"order": "desc"}}]}
    return [h["_source"] for h in get_client().search(index=OPENSEARCH_INDEX, body=body)["hits"]["hits"]]


def fetch_embeddings(title_ids: list[str]) -> dict[str, list[float]]:
    """{title_id: catalog embedding} for the titles present in the catalog."""
    if not title_ids:
        return {}
    body = {"_source": ["title_id", "embedding"],
            "query": {"terms": {"title_id": list(title_ids)}}, "size": len(title_ids)}
    out: dict[str, list[float]] = {}
    for h in get_client().search(index=OPENSEARCH_INDEX, body=body)["hits"]["hits"]:
        s = h.get("_source", {})
        if s.get("title_id") and s.get("embedding"):
            out[s["title_id"]] = s["embedding"]
    return out


def fetch_records_by_ids(title_ids: list[str]) -> dict[str, dict]:
    """Fetch full display records for the given title_ids (for rehydrating the
    final recommendation payload). Returns {title_id: record}."""
    if not title_ids:
        return {}
    body = {
        "_source": DISPLAY_FIELDS,
        "query": {"terms": {"title_id": title_ids}},
        "size": len(title_ids),
    }
    out: dict[str, dict] = {}
    for h in get_client().search(index=OPENSEARCH_INDEX, body=body)["hits"]["hits"]:
        src = h.get("_source", {})
        tid = src.get("title_id")
        if tid:
            out[tid] = src
    return out
