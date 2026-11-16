"""Read-only access to the catalog index (OpenSearch Serverless, SigV4)."""

import hashlib
import json
import os
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

REGION = os.environ.get("AWS_REGION", "us-west-2")
PAGE = 250
FIELDS = ["title_id", "originalTitle", "title_type", "premiere_year", "imdb_rating", "genres"]

_session = boto3.Session()


def search(body, index=None):
    host = os.environ["OPENSEARCH_HOST"]
    index = index or os.environ.get("OPENSEARCH_INDEX", "catalog")
    url = f"https://{host}/{index}/_search"
    data = json.dumps(body).encode("utf-8")
    req = AWSRequest(method="POST", url=url, data=data, headers={
        "Content-Type": "application/json", "X-Amz-Content-SHA256": hashlib.sha256(data).hexdigest()})
    SigV4Auth(_session.get_credentials(), "aoss", REGION).add_auth(req)
    http = urllib.request.Request(url, data=data, headers=dict(req.headers), method="POST")
    with urllib.request.urlopen(http, timeout=60) as resp:
        return json.loads(resp.read())


def genre_list(raw):
    """Catalog genres arrive as a list or as 'a; b' / 'a, b' text."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        items = raw.replace(",", ";").split(";")
    else:
        items = []
    return [g.strip().lower() for g in items if g and g.strip()]


def fetch_ids():
    """Every title_id, sorted (cheap: no vectors). Used for the change fingerprint."""
    ids, start = [], 0
    while True:
        hits = search({"size": 1000, "from": start, "sort": [{"title_id": "asc"}],
                       "_source": ["title_id"], "query": {"match_all": {}}})["hits"]["hits"]
        ids += [h["_source"]["title_id"] for h in hits]
        if len(hits) < 1000:
            return ids
        start += len(hits)


def fingerprint(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:16]


def fetch_all():
    """[{title_id, name, type, year, rating, genres, embedding}] for every title with a vector."""
    out, start = [], 0
    while True:
        hits = search({"size": PAGE, "from": start, "sort": [{"title_id": "asc"}],
                       "_source": FIELDS + ["embedding"], "query": {"match_all": {}}})["hits"]["hits"]
        for h in hits:
            s = h["_source"]
            if not s.get("embedding"):
                continue
            out.append({"title_id": s["title_id"], "name": s.get("originalTitle") or s["title_id"],
                        "type": s.get("title_type") or "", "year": s.get("premiere_year"),
                        "rating": s.get("imdb_rating"), "genres": genre_list(s.get("genres")),
                        "embedding": s["embedding"]})
        if len(hits) < PAGE:
            return out
        start += len(hits)
