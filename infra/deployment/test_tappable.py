"""Idle-search tappable options: pool, selection, processor write, GET /home read."""

import importlib
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True  # Lambda and layer directories are CDK assets
sys.path.insert(0, str(ROOT / "infra/layers/shared/python"))
sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))

tappable = importlib.import_module("tappable")
l2 = importlib.import_module("l2")


def vec(*xs, dim=4):
    return list(xs) + [0.0] * (dim - len(xs))


POOL = [
    {"id": "g1", "text": "Generic one", "genres": ["comedy"], "kids": True, "generic": True, "vector": vec(0, 0, 1)},
    {"id": "g2", "text": "Generic two", "genres": ["horror"], "kids": False, "generic": True, "vector": vec(0, 0, 1)},
    {"id": "g3", "text": "Generic three", "genres": [], "kids": True, "generic": True, "vector": vec(0, 0, 1)},
    {"id": "g4", "text": "Generic four", "genres": ["family"], "kids": True, "generic": True, "vector": vec(0, 0, 1)},
    {"id": "s1", "text": "Space epics", "genres": ["sci-fi"], "kids": False, "generic": False, "vector": vec(1, 0.1)},
    {"id": "s2", "text": "Robot stories", "genres": ["sci-fi"], "kids": False, "generic": False, "vector": vec(0.9, 0.2)},
    {"id": "d1", "text": "Nature docs", "genres": ["documentary"], "kids": True, "generic": False, "vector": vec(0.1, 1)},
    {"id": "h1", "text": "Ghost stories", "genres": ["horror"], "kids": False, "generic": False, "vector": vec(0.95, 0, 0.1)},
]


# --- shipped pool ------------------------------------------------------------

def test_shipped_pool_is_valid():
    opts = tappable.pool()
    vocab = {g["id"] for g in json.loads((ROOT / "infra/lambdas/profile_settings/genres.json").read_text())["genres"]}
    source = json.loads((ROOT / "infra/deployment/seed/tappable_options.json").read_text())["options"]
    assert [o["text"] for o in opts] == [o["text"] for o in source], "re-run scripts/build_tappable_pool.py"
    assert all(set(o["genres"]) <= vocab for o in opts)
    assert len({o["id"] for o in opts}) == len(opts)
    assert len({len(o["vector"]) for o in opts}) == 1
    assert sum(o["generic"] for o in opts) >= 3 and sum(o["generic"] and o["kids"] for o in opts) >= 3


# --- cold start --------------------------------------------------------------

def test_starter_draws_three_generic_options_at_random():
    picks = {tuple(o["text"] for o in tappable.starter(rng=random.Random(s), options=POOL)) for s in range(30)}
    assert all(len(p) == 3 for p in picks) and len(picks) > 1
    assert all(t.startswith("Generic") for p in picks for t in p)


def test_starter_respects_never_genres_and_kids():
    texts = lambda d: {o["text"] for o in tappable.starter(d, count=4, options=POOL)}  # noqa: E731
    assert "Generic two" not in texts({"excluded_genres": ["Horror"]})
    assert texts({"kids_profile": True}) == {"Generic one", "Generic three", "Generic four"}


# --- taste --------------------------------------------------------------------

def test_one_option_per_centroid_first_then_closest():
    cents = [{"vector": vec(1, 0), "weight": 0.7}, {"vector": vec(0, 1), "weight": 0.3}]
    out = [o["text"] for o in tappable.from_taste(cents, options=POOL)]
    assert out[:2] == ["Space epics", "Nature docs"]
    assert out[2] in {"Robot stories", "Ghost stories"} and len(out) == tappable.STORED


def test_taste_options_skip_never_genres_and_respect_kids():
    cents = [{"vector": vec(1, 0), "weight": 1.0}]
    no_horror = [o["text"] for o in tappable.from_taste(cents, declared={"excluded_genres": ["horror"]}, options=POOL)]
    assert "Ghost stories" not in no_horror and "Generic two" not in no_horror
    kids = tappable.from_taste(cents, declared={"kids_profile": True}, options=POOL)
    assert all(o["kids"] for o in kids)


def test_loved_genres_choose_options_before_any_centroid():
    out = [o["text"] for o in tappable.from_taste([], ["documentary", "sci-fi"], count=3, options=POOL)]
    assert out[:2] == ["Nature docs", "Space epics"] and len(out) == 3


def test_no_taste_means_no_stored_options():
    assert tappable.from_taste([], [], options=POOL) == []


def test_visible_filters_stored_and_tops_up():
    stored = [{"text": "Ghost stories", "genres": ["horror"], "kids": False},
              {"text": "Space epics", "genres": ["sci-fi"], "kids": False}]
    shown = tappable.visible(stored, {"excluded_genres": ["horror"]}, rng=random.Random(1), options=POOL)
    assert shown[0] == "Space epics" and len(shown) == 3 and "Ghost stories" not in shown
    assert tappable.visible(None, {}, rng=random.Random(1), options=POOL)[0].startswith("Generic")


# --- processor: options are written with the taste ----------------------------

class KeyedProfiles:
    def __init__(self, items):
        self.items = {k: dict(v) for k, v in items.items()}

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get(Key["data_type"])
        return {"Item": json.loads(json.dumps(item))} if item else {}

    def put_item(self, Item, ConditionExpression, ExpressionAttributeValues=None):
        self.items[Item["data_type"]] = json.loads(json.dumps(Item, default=float))


@pytest.fixture
def processor(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    mod = importlib.import_module("processor")
    mod._catalog.clear()
    monkeypatch.setattr(tappable, "_pool", POOL)
    return mod


def test_taste_update_stores_options_on_affinities(processor, monkeypatch):
    profiles = KeyedProfiles({"AFFINITIES": {"version": 1, "centroids": l2.encode_centroids(
        [{"vector": vec(1, 0), "weight": 1.0, "count": 3}])},
        "L3#DECLARED": {"excluded_genres": ["horror"]}})
    monkeypatch.setattr(processor, "_fetch_embeddings", lambda ids: {i: vec(1, 0.05) for i in ids})
    monkeypatch.setattr(processor, "catalog", lambda item_id: {"title": "X", "genres": ["sci-fi"], "cast": []})

    class Interactions:
        def update_item(self, **kw):
            pass

    sig = [("view", "tt1", 0.8, {"item": "tt1", "ts": 1_760_000_000, "completion": 0.95, "type": "movie"})]
    processor.update_long_term("p-" + "a" * 32, sig, table=Interactions(), profiles=profiles)
    aff = profiles.items["AFFINITIES"]
    texts = [o["text"] for o in aff["tappable_options"]]
    assert texts[0] == "Space epics" and "Ghost stories" not in texts
    assert aff["version"] == 2


def test_negative_only_signal_keeps_stored_options(processor, monkeypatch):
    stored = [{"id": "s1", "text": "Space epics", "genres": ["sci-fi"], "kids": False}]
    profiles = KeyedProfiles({"AFFINITIES": {"version": 1, "tappable_options": stored}})
    monkeypatch.setattr(processor, "catalog", lambda item_id: {"title": "X", "genres": ["drama"], "cast": []})

    class Interactions:
        def update_item(self, **kw):
            pass

    sig = [("view", "tt2", -0.4, {"item": "tt2", "ts": 1_760_000_000, "completion": 0.1, "type": "movie"})]
    processor.update_long_term("p-" + "a" * 32, sig, table=Interactions(), profiles=profiles)
    assert profiles.items["AFFINITIES"]["tappable_options"] == stored


def test_onboarding_loved_genres_store_options(processor):
    profiles = KeyedProfiles({"L3#DECLARED": {"kids_profile": False}})
    processor.apply_loved("p-" + "a" * 32, [{"id": "documentary", "label": "Documentary"}], profiles=profiles)
    assert profiles.items["AFFINITIES"]["tappable_options"][0]["text"] == "Nature docs"


# --- GET /home ------------------------------------------------------------------

@pytest.fixture
def household(monkeypatch):
    for k in ("HOUSEHOLDS_TABLE", "PROFILES_TABLE", "REC_CACHE_TABLE", "INTERACTIONS_TABLE"):
        monkeypatch.setenv(k, k.lower())
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    sys.path.insert(0, str(ROOT / "infra/lambdas/household"))
    mod = importlib.import_module("lambda_function")
    monkeypatch.setattr(tappable, "_pool", POOL)
    return mod


class TasteStore:
    def __init__(self, declared, affinities):
        self.declared, self.affinities, self.backfilled = declared, affinities, None

    def member(self, account_id, profile_id):
        return {"profile_id": profile_id}

    def taste(self, profile_id):
        return self.declared, self.affinities

    def backfill_options(self, profile_id, version, options):
        self.backfilled = (version, options)


PID = "p-" + "c" * 32


def test_options_part_returns_stored_options_without_rows(household):
    store = TasteStore({}, {"version": 3, "tappable_options": [
        {"text": "Space epics", "genres": ["sci-fi"], "kids": False},
        {"text": "Nature docs", "genres": ["documentary"], "kids": True},
        {"text": "Robot stories", "genres": ["sci-fi"], "kids": False}]})
    body = household.get_home(store, "sub-1", PID, part="options")
    assert body == {"profileId": PID, "tappableOptions": ["Space epics", "Nature docs", "Robot stories"]}
    assert store.backfilled is None


def test_cold_start_profile_gets_generic_options(household):
    body = household.get_home(TasteStore({"kids_profile": True}, {}), "sub-1", PID, part="options")
    assert len(body["tappableOptions"]) == 3 and "Generic two" not in body["tappableOptions"]


def test_taste_without_options_is_backfilled_once(household):
    store = TasteStore({}, {"version": 4, "centroids": l2.encode_centroids(
        [{"vector": vec(0, 1), "weight": 1.0, "count": 2}])})
    body = household.get_home(store, "sub-1", PID, part="options")
    assert body["tappableOptions"][0] == "Nature docs"
    assert store.backfilled[0] == 4 and store.backfilled[1][0]["text"] == "Nature docs"
