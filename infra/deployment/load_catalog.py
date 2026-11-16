"""Embed the repository catalog and index it into this deployment's search collection.

    .venv/bin/python infra/deployment/load_catalog.py --config infra/environments/dev.json
    .venv/bin/python infra/deployment/load_catalog.py --dry-run     # counts only, no AWS

Reads every data/catalog/*.json file (the synthetic movie catalog, Tears of Steel
and the micro-dramas), embeds each title's `title_text` with the configured Nova
Multimodal Embeddings model (document-side GENERIC_INDEX purpose, 1024-d, the
pair of the agent's TEXT_RETRIEVAL queries), normalises genre ids ("scifi" ->
"sci-fi") and bulk-indexes the documents into
the `catalog` index the Application stack created. Poster `image` fields point
at s3://<mediaBucket>/posters/<file>, where load_media.py uploads posters.zip.

Safe to re-run. The collection is a classic vector-search collection, which
assigns document ids itself, so a re-run first deletes the documents already
stored for the same title_ids and then indexes fresh copies: the index always
holds exactly one document per title_id.

The caller must be a principal in the collection's data-access policy (the
SearchAccess policy in stacks.py) with aoss:WriteDocument on the catalog index.
"""
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import DEFAULT_CONFIG, ROOT, load_config, stack_name

CATALOG_DIR = ROOT / "data/catalog"
INDEX = "catalog"
DIMENSION = 1024
# Fields mapped on the catalog index (stacks.py catalog_fields). Anything else
# in a source record (e.g. the v1 `synopsis`) is dropped so nothing is
# dynamically mapped.
FIELDS = ["title_id", "originalTitle", "principalCastMembers", "principalCrewMembers",
          "mpaa_ratings", "genres", "imdb_url", "keywords", "plot", "title_type",
          "imdb_rating", "premiere_date", "premiere_year", "title_text", "awards", "image"]


# The v1 synthetic catalog spells science fiction "scifi"; the agent, the genre
# settings (profile_settings/genres.json) and the micro-dramas use "sci-fi".
GENRE_ALIASES = {"scifi": "sci-fi"}

# --------------------------------------------------------------------------- pure

def load_records(directory=CATALOG_DIR):
    """All catalog records from data/catalog/*.json, sorted by title_id; ids unique."""
    records = []
    for path in sorted(Path(directory).glob("*.json")):
        data = json.loads(path.read_text())
        records.extend(data if isinstance(data, list) else [data])
    ids = [r["title_id"] for r in records]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate title_id in catalog: {dupes}")
    return sorted(records, key=lambda r: r["title_id"])


def poster_file(image):
    """Poster file name for an `image` value: 'images/<f>', 'posters/<f>', a URL or an
    s3:// URI. A non-jpg source (the Tears of Steel PNG) maps to the repo's jpg copy."""
    name = str(image or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    if not name:
        return ""
    stem, _, ext = name.rpartition(".")
    if ext.lower() != "jpg":
        name = (stem or name) + ".jpg"
    return name


def poster_key(image):
    """Repo/bucket-relative poster key, 'posters/<file>.jpg' (or '' when there is none)."""
    name = poster_file(image)
    return f"posters/{name}" if name else ""


def _names(members):
    """'Name as (role)' -> 'Name', de-duplicated, order kept."""
    out = []
    for m in members or []:
        name = str(m).split(" as (", 1)[0].strip()
        if name and name not in out:
            out.append(name)
    return out


def build_title_text(rec):
    """The embedded text, in the v1 catalog's title_text layout: genres, keywords,
    plot, title, cast, crew, awards (when any), decade."""
    lines = [", ".join(rec.get("genres") or []), ", ".join(rec.get("keywords") or []),
             rec.get("plot") or "", rec.get("originalTitle") or "",
             ", ".join(_names(rec.get("principalCastMembers"))),
             ", ".join(_names(rec.get("principalCrewMembers")))]
    if rec.get("awards"):
        lines.append(", ".join(rec["awards"]))
    year = rec.get("premiere_year")
    if isinstance(year, int):
        lines.append(f"{year // 10 * 10}s")
    return "\n".join(lines)


def normalise_genres(genres):
    """Lowercased genre ids with the catalog's aliases applied, order kept."""
    out = []
    for g in genres or []:
        g = GENRE_ALIASES.get(str(g).strip().lower(), str(g).strip().lower())
        if g and g not in out:
            out.append(g)
    return out


def to_document(rec, media_bucket):
    """Index document for one record (embedding added separately)."""
    doc = {k: rec[k] for k in FIELDS if k in rec}
    doc["title_text"] = rec.get("title_text") or build_title_text(rec)
    doc["genres"] = normalise_genres(rec.get("genres"))
    key = poster_key(rec.get("image"))
    doc["image"] = f"s3://{media_bucket}/{key}" if key else ""
    return doc


def embedding_body(text):
    """Nova Multimodal Embeddings request for a catalog document."""
    return {"taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {"embeddingPurpose": "GENERIC_INDEX", "embeddingDimension": DIMENSION,
                                      "text": {"truncationMode": "END", "value": text}}}


def bulk_lines(docs, delete_ids=()):
    """NDJSON _bulk body: delete the old copies, then index the new documents."""
    lines = [json.dumps({"delete": {"_index": INDEX, "_id": i}}) for i in delete_ids]
    for doc in docs:
        lines += [json.dumps({"index": {"_index": INDEX}}), json.dumps(doc, ensure_ascii=False)]
    return "\n".join(lines) + "\n"


def summary(records):
    counts = {}
    for r in records:
        counts[r.get("title_type", "?")] = counts.get(r.get("title_type", "?"), 0) + 1
    return counts


# --------------------------------------------------------------------------- AWS

class Search:
    """Minimal SigV4-signed client for the serverless collection (no extra dependency)."""

    def __init__(self, session, endpoint, region):
        from botocore.auth import SigV4Auth
        self.endpoint, self.region = endpoint.rstrip("/"), region
        self.creds, self.auth = session.get_credentials(), SigV4Auth

    def call(self, method, path, body=None, content_type="application/json"):
        import urllib.request
        from botocore.awsrequest import AWSRequest
        import hashlib
        data = body.encode() if isinstance(body, str) else (json.dumps(body).encode() if body is not None else None)
        # OpenSearch Serverless requires the signed payload hash header.
        headers = {"x-amz-content-sha256": hashlib.sha256(data or b"").hexdigest()}
        if data:
            headers["Content-Type"] = content_type
        req = AWSRequest(method=method, url=self.endpoint + path, data=data, headers=headers)
        self.auth(self.creds.get_frozen_credentials(), "aoss", self.region).add_auth(req)
        request = urllib.request.Request(req.url, data=data, method=method, headers=dict(req.headers))
        with urllib.request.urlopen(request, timeout=120) as resp:
            return json.loads(resp.read() or b"{}")

    def existing_ids(self, title_ids):
        """Document _ids currently stored for these title_ids."""
        found = []
        for i in range(0, len(title_ids), 500):
            chunk = title_ids[i:i + 500]
            res = self.call("POST", f"/{INDEX}/_search", {"size": 10000, "_source": False,
                                                          "query": {"terms": {"title_id": chunk}}})
            found += [h["_id"] for h in res["hits"]["hits"]]
        return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true", help="Print catalog counts and exit; no AWS calls")
    parser.add_argument("--workers", type=int, default=8, help="Parallel embedding calls")
    args = parser.parse_args()
    records = load_records()
    print(f"{len(records)} catalog titles from {CATALOG_DIR.relative_to(ROOT)}: {summary(records)}", flush=True)
    missing = [r["title_id"] for r in records if not r.get("title_text")]
    print(f"title_text built for {len(missing)} record(s); posters: "
          f"{sum(1 for r in records if poster_key(r.get('image')))}", flush=True)
    if args.dry_run:
        return

    cfg = load_config(args.config)
    from deploy import aws_environment
    env = aws_environment(cfg["region"])
    os.environ.clear()
    os.environ.update(env)
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    if session.client("sts").get_caller_identity()["Account"] != cfg["account"]:
        raise SystemExit("Wrong deployment account")
    stack = session.client("cloudformation").describe_stacks(StackName=stack_name(cfg, "Application"))["Stacks"][0]
    endpoint = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}["SearchEndpoint"]
    docs = [to_document(r, cfg["mediaBucket"]) for r in records]

    bedrock = session.client("bedrock-runtime", region_name=cfg.get("embeddingRegion", cfg["region"]))
    model = cfg.get("embeddingModel", "amazon.nova-2-multimodal-embeddings-v1:0")

    def embed(doc):
        resp = bedrock.invoke_model(modelId=model, body=json.dumps(embedding_body(doc["title_text"])),
                                    accept="application/json", contentType="application/json")
        vector = json.loads(resp["body"].read())["embeddings"][0]["embedding"]
        if len(vector) != DIMENSION:
            raise SystemExit(f"{doc['title_id']}: expected {DIMENSION}-d embedding, got {len(vector)}")
        return vector

    print(f"Embedding {len(docs)} titles with {model}", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for doc, vector in zip(docs, pool.map(embed, docs)):
            doc["embedding"] = vector

    search = Search(session, endpoint, cfg["region"])
    old = search.existing_ids([d["title_id"] for d in docs])
    print(f"Replacing {len(old)} existing document(s); indexing {len(docs)} into {INDEX}", flush=True)
    errors = []
    for i in range(0, max(len(old), len(docs)), 100):
        res = search.call("POST", "/_bulk", bulk_lines(docs[i:i + 100], old[i:i + 100]),
                          content_type="application/x-ndjson")
        errors += [item for item in res.get("items", [])
                   if next(iter(item.values())).get("status", 200) >= 300
                   and next(iter(item.values())).get("status") != 404]
    if errors:
        raise SystemExit(f"{len(errors)} bulk error(s), first: {json.dumps(errors[0])[:500]}")
    print("Done (search results reflect the new documents after the collection refreshes, ~1 min)")


if __name__ == "__main__":
    main()
