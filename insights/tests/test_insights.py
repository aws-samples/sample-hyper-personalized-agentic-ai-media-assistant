"""Unit tests for the insights app (no AWS calls)."""

import base64
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "insights/api"), str(ROOT / "insights/layout")]

import profile as prof  # noqa: E402
from labels import label_clusters  # noqa: E402

NOW = 1_791_480_000.0  # a Thursday


def _aff():
    return {
        "tz_offset_min": -300,
        "entities": {
            "genre:sci-fi": {"s": 0.9, "n": 6, "label": "Sci-Fi"},
            "genre:horror": {"s": -0.5, "n": 4, "label": "Horror"},
            "genre:drama": {"s": 0.45, "n": 3, "label": "Drama", "last": NOW - 30 * 86400},
            "person:new_face": {"s": 0.8, "n": 1, "label": "New Face"},
            "genre:war": {"s": 0.7, "n": 5, "label": "War"},
        },
        "contexts": {"weekday|daytime": {"views": 4, "long_min": 300, "short_min": 10,
                                         "entities": {"genre:sci-fi": {"s": 0.5, "n": 3, "label": "Sci-Fi"}}}},
        "notes": ["weekend: epic sci-fi"],
    }


def test_taste_text_matches_the_agent():
    sys.path.insert(0, str(ROOT / "backend/movie-ai-agent-v2"))
    try:
        from user_profile import render_affinities
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"agent module not importable here: {exc}")
    for excluded in ([], ["war"]):
        assert prof.taste_lines(_aff(), excluded, NOW) == render_affinities(_aff(), excluded, NOW)


def test_entity_rows_mark_what_the_agent_uses():
    rows = {r["key"]: r for r in prof.entity_rows(_aff()["entities"], NOW)}
    assert rows["genre:sci-fi"]["used"]
    assert not rows["person:new_face"]["used"]          # n < 3: not enough evidence
    assert rows["genre:drama"]["s_now"] == pytest.approx(0.225, abs=1e-3)  # one half-life faded
    assert not rows["genre:drama"]["used"]


def test_decode_centroids_roundtrip_and_skips_garbage():
    v = [0.5, -0.25, 1.0]
    stored = [{"v": base64.b64encode(struct.pack("<3f", *v)).decode(), "w": 0.75, "n": 3}, {"v": "!!"}]
    out = prof.decode_centroids(stored)
    assert len(out) == 1 and out[0]["vector"] == v and out[0]["count"] == 3


def test_place_follows_the_nearest_titles():
    coords = {"a": [10, 0, 0], "b": [-10, 0, 0], "c": [0, 10, 0]}
    p = prof.place([("a", 0.9), ("b", 0.6), ("missing", 0.99)], coords)
    assert p[0] > 9.9                                    # dominated by the closest title
    assert prof.place([("missing", 0.9)], coords) is None


def test_cluster_labels_prefer_distinctive_genres_and_disambiguate():
    titles = ([{"genres": ["drama", "romance"], "type": "movie", "name": f"R{i}", "rating": i} for i in range(10)]
              + [{"genres": ["drama", "crime"], "type": "tvSeries", "name": f"C{i}", "rating": i} for i in range(10)]
              + [{"genres": ["drama", "crime"], "type": "tvSeries", "name": f"D{i}", "rating": 9 - i} for i in range(10)])
    labels = label_clusters(titles, [0] * 10 + [1] * 10 + [2] * 10)
    assert labels[0]["label"].startswith("Romance")      # drama is everywhere, romance is not
    assert labels[1]["label"] != labels[2]["label"]      # same genres, told apart by an example
    assert "series" in labels[1]["label"]


def test_handler_requires_an_allowed_user(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    import handler
    monkeypatch.setattr(handler, "ALLOWED", {"demo-user"})
    ev = lambda user: {"resource": "/personas", "requestContext": {"authorizer": {"claims": {"cognito:username": user}}}}
    assert handler.lambda_handler(ev("someone-else"), None)["statusCode"] == 403
    assert handler.lambda_handler({"resource": "/nope"}, None)["statusCode"] == 404
    monkeypatch.setattr(handler, "ROUTES", {"personas": lambda p: (200, {"ok": True})})
    assert handler.lambda_handler(ev("demo-user"), None)["statusCode"] == 200


def test_whatif_sandbox_runs_the_real_processor_without_touching_the_source(monkeypatch):
    sys.path[:0] = [str(ROOT / "insights/whatif"), str(ROOT / "infra/lambdas/profile_events")]
    import processor
    from sandbox import Sandbox
    vec = {"tA": [1.0, 0.0, 0.0], "tB": [0.0, 1.0, 0.0]}
    monkeypatch.setattr(processor, "_fetch_embeddings", lambda ids: {i: vec[i] for i in ids if i in vec})
    monkeypatch.setattr(processor, "prefetch_catalog", lambda ids, **kw: processor._catalog.update(
        {i: (9e12, {"title": i, "genres": ["Horror"], "cast": []}) for i in ids}))
    live = {"AFFINITIES": {"user_id": "p-x", "data_type": "AFFINITIES", "version": 3, "entities": {},
                           "centroids": __import__("l2").encode_centroids([{"vector": vec["tA"], "weight": 1.0, "count": 2}])}}
    base = Sandbox("p-x", live, {})
    what = base.clone()
    e, _ = what.apply("view", "tB", ts=1_800_000_000)
    assert e == 0.8                                            # a finished long-form view
    assert what.affinities["version"] == 4 and base.affinities["version"] == 3
    assert len(what.centroids()) == 2                          # orthogonal title: a new taste vector
    assert what.affinities["entities"]["genre:horror"]["n"] == 1
    assert what.completed() == ["tB"] and base.completed() == []
    e, _ = what.apply("feedback", "tA", "dislike", ts=1_800_000_100)
    assert e == -0.4 and len(what.centroids()) == 2            # negatives never move taste vectors
