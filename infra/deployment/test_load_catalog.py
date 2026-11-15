"""Pure parts of load_catalog.py and the shipped data/catalog files (no AWS)."""
import json
import zipfile

import load_catalog as lc


def test_catalog_counts_and_ids():
    records = lc.load_records()
    counts = lc.summary(records)
    assert counts == {"movie": 501, "micro-drama": 8}
    ids = {r["title_id"] for r in records}
    assert {"tt1001001", "tt1001500", "tt2285752", "md0000001", "md0000008"} <= ids


def test_every_record_has_a_shipped_poster():
    names = set(zipfile.ZipFile(lc.CATALOG_DIR / "posters.zip").namelist())
    assert all(n.startswith("posters/") and n.endswith(".jpg") for n in names)
    for r in lc.load_records():
        assert lc.poster_key(r["image"]) in names, r["title_id"]


def test_poster_key_normalisation():
    assert lc.poster_key("images/Quantum_Storm_tt1001001.jpg") == "posters/Quantum_Storm_tt1001001.jpg"
    assert lc.poster_key("posters/one-bar.jpg") == "posters/one-bar.jpg"
    assert lc.poster_key("https://mango.blender.org/wp-content/uploads/2012/09/tos-poster.png") == "posters/tos-poster.jpg"
    assert lc.poster_key("s3://bucket/input/posters/x.jpg?v=1") == "posters/x.jpg"
    assert lc.poster_key("") == ""


def test_to_document_rewrites_image_and_drops_unmapped_fields():
    rec = {"title_id": "tt1", "originalTitle": "A", "image": "images/A_tt1.jpg", "synopsis": "x",
           "title_text": "given"}
    doc = lc.to_document(rec, "my-media")
    assert doc["image"] == "s3://my-media/posters/A_tt1.jpg"
    assert doc["title_text"] == "given"
    assert "synopsis" not in doc and set(doc) <= set(lc.FIELDS)


def test_build_title_text_matches_v1_layout():
    # Every v1 record's title_text is reproduced from its fields.
    v1 = json.loads((lc.CATALOG_DIR / "movie_dataset.json").read_text())
    tos = json.loads((lc.CATALOG_DIR / "exclusive_movie_dataset.json").read_text())
    for rec in v1[:50] + tos:
        assert lc.build_title_text(rec) == rec["title_text"], rec["title_id"]
    rec = {"genres": ["drama"], "keywords": [], "plot": "P", "originalTitle": "T",
           "principalCastMembers": ["A as (x)"], "principalCrewMembers": ["B as (director)", "B as (writer)"],
           "premiere_year": 1987}
    assert lc.build_title_text(rec) == "drama\n\nP\nT\nA\nB\n1980s"
    assert lc.to_document({"title_id": "t", **rec}, "b")["title_text"] == lc.build_title_text(rec)


def test_embedding_body_is_document_side_nova_request():
    body = lc.embedding_body("hello")
    params = body["singleEmbeddingParams"]
    assert body["taskType"] == "SINGLE_EMBEDDING"
    assert params["embeddingPurpose"] == "GENERIC_INDEX" and params["embeddingDimension"] == 1024
    assert params["text"]["value"] == "hello"


def test_bulk_lines_delete_then_index():
    lines = lc.bulk_lines([{"title_id": "a"}], ["old1"]).splitlines()
    assert json.loads(lines[0]) == {"delete": {"_index": "catalog", "_id": "old1"}}
    assert json.loads(lines[1]) == {"index": {"_index": "catalog"}}
    assert json.loads(lines[2]) == {"title_id": "a"}


def test_microdrama_dataset_has_no_account_specific_strings():
    text = (lc.CATALOG_DIR / "microdrama_dataset.json").read_text()
    assert "s3://" not in text and "amazonaws" not in text
    for r in json.loads(text):
        assert r["title_type"] == "micro-drama" and r["image"].startswith("posters/")


def test_genres_use_the_agent_vocabulary():
    assert lc.normalise_genres(["SciFi", "action", "scifi"]) == ["sci-fi", "action"]
    vocab = {g["id"] for g in json.loads((lc.ROOT / "infra/lambdas/profile_settings/genres.json").read_text())["genres"]}
    for r in lc.load_records():
        assert set(lc.to_document(r, "b")["genres"]) <= vocab, r["title_id"]


def test_api_posters_are_presigned_from_the_media_bucket(monkeypatch):
    import sys
    sys.path.insert(0, str(lc.ROOT / "infra/layers/shared/python"))
    import ibc_common

    class S3:
        def generate_presigned_url(self, op, Params, ExpiresIn):
            return f"https://signed/{Params['Bucket']}/{Params['Key']}"

    monkeypatch.setattr(ibc_common, "_s3_client", S3())
    movie = ibc_common.normalize_movie({"title_id": "tt1", "image": "s3://media/posters/a.jpg"})
    assert movie["posterUrl"] == "https://signed/media/posters/a.jpg"
    assert ibc_common.poster_url("https://example.com/p.jpg") == "https://example.com/p.jpg"
    assert ibc_common.poster_url(None) is None and ibc_common.poster_url("s3://bucket-only") is None
