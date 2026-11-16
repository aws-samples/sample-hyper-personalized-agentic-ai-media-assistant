"""Taste centroids: clustering, storage, household seeding and agent retrieval."""

import importlib
import json
import math
import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED = json.loads((ROOT / "infra/deployment/seed/default_household.json").read_text())
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
sys.dont_write_bytecode = True  # Lambda/layer/agent dirs are build assets
sys.path.insert(0, str(ROOT / "infra/lambdas/household"))
sys.path.insert(0, str(ROOT / "infra/layers/shared/python"))
AGENT = ROOT / "backend/movie-ai-agent-v2"

# The agent's runtime-only dependencies are not installed in the deploy venv.
for name in ["strands", "strands.models", "opensearchpy"]:
    mod = sys.modules.setdefault(name, types.ModuleType(name))
    for attr in ["Agent", "BedrockModel", "OpenSearch", "RequestsHttpConnection", "AWSV4SignerAuth"]:
        setattr(mod, attr, object)


@pytest.fixture
def core():
    return importlib.import_module("household_core")


def _load_agent():
    """Import the agent modules once. The agent has its own top-level `config`,
    which shadows the deploy one, so swap it in for the import and put the
    deploy `config` back afterwards (agent modules keep their own binding)."""
    saved = sys.modules.pop("config", None)
    sys.path.insert(0, str(AGENT))
    os.environ.setdefault("AWS_REGION", "us-west-2")
    try:
        return types.SimpleNamespace(**{n: importlib.import_module(n) for n in ["taste", "retrieval", "catchall", "agent_v2"]})
    finally:
        sys.path.remove(str(AGENT))
        sys.modules.pop("config", None)
        if saved is not None:
            sys.modules["config"] = saved


_AGENT = None


@pytest.fixture
def agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = _load_agent()
    return _AGENT


def basis(i, dim=8):
    v = [0.0] * dim
    v[i] = 1.0
    return v


def mix(a, b, t):
    return [x * (1 - t) + y * t for x, y in zip(a, b)]


# --- clustering ------------------------------------------------------------

def test_similar_titles_share_a_centroid_and_dissimilar_start_new(core):
    vecs = [basis(0), mix(basis(0), basis(1), 0.3), basis(2)]
    out = core.taste_centroids(vecs)
    assert [c["count"] for c in out] == [2, 1]
    assert [c["weight"] for c in out] == [0.4444, 0.2857]          # E / (E + K): 1.6 / 3.6, 0.8 / 2.8
    assert all(math.isclose(math.sqrt(sum(x * x for x in c["vector"])), 1.0, rel_tol=1e-9) for c in out)


def test_at_most_three_centroids(core):
    out = core.taste_centroids([basis(i) for i in range(6)])
    assert len(out) == core.TASTE_MAX_CENTROIDS
    assert sum(c["count"] for c in out) == 6
    assert all(0 < c["weight"] < 1 for c in out)


def test_threshold_is_inclusive_at_055(core):
    # cos(a, b) just above / below 0.55
    a = basis(0)
    above = [0.56, math.sqrt(1 - 0.56 ** 2)] + [0.0] * 6
    below = [0.54, math.sqrt(1 - 0.54 ** 2)] + [0.0] * 6
    assert len(core.taste_centroids([a, above])) == 1
    assert len(core.taste_centroids([a, below])) == 2


def test_lambda_and_agent_clustering_agree(core, agent):
    vecs = [basis(0), mix(basis(0), basis(1), 0.4), basis(3), basis(5), mix(basis(3), basis(4), 0.2)]
    assert core.taste_centroids(vecs) == agent.taste.taste_centroids(vecs)
    assert (core.TASTE_JOIN_COSINE, core.TASTE_MAX_CENTROIDS) == (agent.taste.TASTE_JOIN_COSINE, agent.taste.TASTE_MAX_CENTROIDS)
    assert (core.TASTE_EVIDENCE_K, core.TASTE_TITLE_E, core.CENTROID_RATE) == \
        (agent.taste.TASTE_EVIDENCE_K, agent.taste.TASTE_TITLE_E, agent.taste.CENTROID_RATE)


def test_batch_matches_replaying_titles_through_the_processor(core):
    sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))
    l2 = importlib.import_module("l2")
    vecs = [basis(0), mix(basis(0), basis(1), 0.4), basis(3), basis(5), mix(basis(3), basis(4), 0.2), basis(6)]
    cents = []
    for v in vecs:
        cents = l2.update_centroids(cents, v, core.TASTE_TITLE_E)
    batch = core.taste_centroids(vecs)
    assert [(c["count"], c["weight"]) for c in cents] == [(c["count"], c["weight"]) for c in batch]
    for a, b in zip(cents, batch):
        assert all(math.isclose(x, y, abs_tol=1e-9) for x, y in zip(a["vector"], b["vector"]))


def test_storage_round_trip_between_lambda_and_agent(core, agent):
    stored = core.stored_centroids([basis(0), basis(2)])
    decoded = agent.taste.decode_stored(stored)
    assert [w for w, _ in decoded] == [0.2857, 0.2857]
    assert [round(x, 6) for x in decoded[0][1]] == basis(0)
    assert agent.taste.decode_stored([{"v": "not-base64!", "w": 1}]) == []


# --- household seeding -----------------------------------------------------

def test_household_writes_centroids_from_watched_embeddings(core):
    watched = core.watched_title_ids(SEED)
    emb = {t: basis(i % 8) for i, t in enumerate(watched)}
    items = core.build_household("sub-1", SEED, NOW, embeddings=emb)
    aff = [i for i in items["profiles"] if i["data_type"] == "AFFINITIES"]
    for member, item in zip(SEED["members"], aff):
        n = len([w for w in member.get("watched") or [] if w["title_id"] in emb])
        assert sum(int(c["n"]) for c in item["centroids"]) == n
        assert len(item["centroids"]) <= 3
    assert sum(len(v) for v in items.values()) <= core.MAX_TRANSACT_ITEMS


def test_household_without_embeddings_has_no_centroids(core):
    items = core.build_household("sub-1", SEED, NOW)
    assert all("centroids" not in i for i in items["profiles"] if i["data_type"] == "AFFINITIES")


def test_embedding_failure_still_creates_household(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    fn = importlib.import_module("lambda_function")
    written = {}

    class Store:
        def members(self, account_id):
            return [i for i in written.get("households", []) if i["sk"].startswith("MEMBER#")]

        def seed(self):
            return SEED

        def provision(self, items):
            written.update(items)

    def boom(_):
        raise RuntimeError("aoss down")

    monkeypatch.setattr(fn, "fetch_embeddings", boom)
    body = fn.get_household(Store(), "sub-1", NOW)
    assert len(body["members"]) == len(SEED["members"])


# --- agent retrieval -------------------------------------------------------

def test_fuse_weights_lists(agent):
    a, b = [{"title_id": "a"}], [{"title_id": "b"}]
    assert [h["title_id"] for h in agent.retrieval.fuse([(1.0, a), (0.3, b)], 5)] == ["a", "b"]
    assert [h["title_id"] for h in agent.retrieval.fuse([(0.3, a), (1.0, b)], 5)] == ["b", "a"]


def test_knn_search_adds_one_list_per_centroid_and_excludes(agent, monkeypatch):
    seen = []
    monkeypatch.setattr(agent.retrieval, "embed_query", lambda q: basis(7))
    monkeypatch.setattr(agent.retrieval, "_bm25_only", lambda q, fc, size: [])

    def knn(vec, fc, size):
        seen.append((vec, fc, size))
        return [{"title_id": f"t{len(seen)}"}]
    monkeypatch.setattr(agent.retrieval, "_knn_vector", knn)
    agent.retrieval.knn_search("tense", taste=[(0.7, basis(0)), (0.3, basis(1))], exclude_ids=["tt1"])
    assert [v for v, _, _ in seen] == [basis(7), basis(0), basis(1)]
    assert seen[1][2] == agent.retrieval.TASTE_K
    assert all({"bool": {"must_not": [{"terms": {"title_id": ["tt1"]}}]}} in fc for _, fc, _ in seen)


@pytest.mark.parametrize("d", [{"person": "Nolan"}, {"exact_title": "Dune"}, {"min_year": 1990}, {"max_year": 2000}])
def test_specific_requests_never_get_taste_boost(agent, monkeypatch, d):
    calls = []
    monkeypatch.setattr(agent.catchall, "knn_search", lambda **kw: calls.append(kw) or [])
    agent.catchall.catch_all({**d, "category": "movie"}, "q", taste=[(1.0, basis(0))], exclude_ids=["tt1"])
    assert not calls[0].get("taste")


def test_open_request_gets_taste_boost(agent, monkeypatch):
    calls = []
    monkeypatch.setattr(agent.catchall, "knn_search", lambda **kw: calls.append(kw) or [])
    agent.catchall.catch_all({"category": "movie", "kids": True}, "q", taste=[(1.0, basis(0))], exclude_ids=["tt1"])
    assert calls[0]["taste"] and calls[0]["exclude_ids"] == ["tt1"]


def test_exact_title_request_does_not_exclude_watched(agent, monkeypatch):
    calls = []
    monkeypatch.setattr(agent.catchall, "knn_search", lambda **kw: calls.append(kw) or [])
    agent.catchall.catch_all({"exact_title": "Quantum Storm"}, "q", taste=None, exclude_ids=["tt1001001"])
    assert not calls[0].get("exclude_ids")


# --- orchestrator ----------------------------------------------------------

class FakeSession:
    def __init__(self, taste, watched=("tt-w",), turns=(), prefs=(), limits=None):
        self._taste, self._watched, self._turns = list(taste), list(watched), list(turns)
        self._declared = (list(prefs), dict(limits or {}))
        self.added = []

    def get_declared(self):
        return self._declared

    def ensure_ltm_refresher(self):
        pass

    def get_user_profile_context(self):
        return "Viewer profile: likes sci-fi"

    def get_long_term_context(self):
        return ""

    def get_taste_vectors(self):
        return self._taste

    def get_watched_ids(self):
        return self._watched

    def get_excluded_ids(self):
        return ["tt-last"]

    def get_last_recommendations(self):
        return []

    def add_turn(self, *a, **kw):
        self.added.append(a)


def run(agent, monkeypatch, session, query, decomposition=None):
    """Run the orchestrator with the understanding call's model output stubbed
    (`decomposition` = the model's JSON); routing + apply_profile run for real."""
    blank = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0}
    log = {}
    m = agent.agent_v2
    und = sys.modules["understand"]
    model_out = dict(decomposition or {"genres_include": ["thriller"], "search_text": query})
    monkeypatch.setattr(und, "call", lambda *a, **kw: log.update(understand_input=a[1]) or (json.dumps(model_out), blank, 0.0))
    monkeypatch.setattr(m, "fan_out", lambda q, c, kids=False: log.update(fanout=q, fanout_kids=kids, fanout_category=c) or {"candidates": {"x": "x | genres: thriller"}, "usage": blank})
    monkeypatch.setattr(m, "catch_all", lambda d, q, **kw: log.update(catch_all=kw, catch_all_d=d) or {"candidates": {"x": "x"}, "usage": blank})
    monkeypatch.setattr(m, "catch_all_taste", lambda taste, exclude_ids=None, movie_only=True, filters=None: log.update(surprise=(taste, exclude_ids, movie_only), taste_filters=filters) or {"candidates": {"x": "x"}, "usage": blank})
    monkeypatch.setattr(m, "reduce", lambda q, c, exclude_ids=None, **kw: log.update(reduce_excl=exclude_ids, reduce_q=q, reduce_prefs=kw.get("preferences")) or {"response": "r", "title_ids": [], "tappable_options": [], "usage": blank})
    monkeypatch.setattr(m, "build_recommendations", lambda ids: ([], {}))
    events = list(m.run_agent_stream(query, session=session))
    assert events[-1]["type"] == "response"
    return log


def test_open_request_uses_taste_route(agent, monkeypatch):
    taste = [(1.0, basis(0))]
    log = run(agent, monkeypatch, FakeSession(taste), "whatever you think",
              decomposition={"open_ended": True})
    assert "fanout" not in log and "catch_all" not in log
    assert log["surprise"] == (taste, ["tt-w", "tt-last"], True)


def test_open_request_without_taste_still_uses_taste_route(agent, monkeypatch):
    log = run(agent, monkeypatch, FakeSession([]), "you decide",
              decomposition={"category": "both", "open_ended": True})
    assert log["surprise"] == ([], ["tt-w", "tt-last"], False)


def test_no_phrase_matching_in_orchestrator(agent, monkeypatch):
    # "surprise me" with a model verdict of a genre request must follow the model.
    log = run(agent, monkeypatch, FakeSession([(1.0, basis(0))]), "surprise me")
    assert "surprise" not in log and log["fanout"]
    assert not hasattr(agent.agent_v2, "_is_surprise") and not hasattr(agent.agent_v2, "_SURPRISE_PHRASES")


@pytest.mark.parametrize("model_out,expect", [
    ({"open_ended": True}, "taste"),
    ({"open_ended": True, "person": "Nolan"}, "catch_all"),
    ({"open_ended": True, "genres_include": ["comedy"]}, "fanout"),
    ({"open_ended": True, "keywords": ["heist"]}, "fanout"),
    ({"open_ended": True, "category": "micro-drama"}, "catch_all"),
    ({"open_ended": True, "genres_exclude": ["horror"]}, "taste"),   # an avoid alone stays open
    ({"open_ended": True, "search_text": "cozy film"}, "fanout"),    # a stated want is not open
    ({"open_ended": False}, "fanout"),
])
def test_understand_routes_from_model_fields(agent, monkeypatch, model_out, expect):
    und = sys.modules["understand"]
    monkeypatch.setattr(und, "call", lambda *a, **kw: (json.dumps(model_out), {}, 0.0))
    assert und.understand("anything", [], [])["path"] == expect


def test_understand_falls_back_to_raw_text_on_bad_output(agent, monkeypatch):
    und = sys.modules["understand"]
    monkeypatch.setattr(und, "call", lambda *a, **kw: ("not json", {}, 0.0))
    d = und.understand("a cozy mystery", [], [])
    assert not d["_ok"] and d["path"] == "fanout" and d["search_text"] == "a cozy mystery"


def test_understand_keeps_only_given_rule_overrides(agent, monkeypatch):
    und = sys.modules["understand"]
    out = {"genres_include": ["horror"], "overrides": ["avoid genre: horror", "avoid genre: war"]}
    monkeypatch.setattr(und, "call", lambda *a, **kw: (json.dumps(out), {}, 0.0))
    d = und.understand("a horror film", [], [], rules=["avoid genre: horror"])
    assert d["overrides"] == ["avoid genre: horror"]


def test_avoids_never_reach_the_knn_text(agent, monkeypatch):
    und = sys.modules["understand"]
    out = {"genres_include": ["sci-fi"], "avoid_soft": ["gore"], "search_text": "sci-fi adventure"}
    monkeypatch.setattr(und, "call", lambda *a, **kw: (json.dumps(out), {}, 0.0))
    d = und.understand("sci-fi without the gore", [], [])
    assert "gore" not in d["knn_text"] and "Avoid: gore" in d["llm_text"]


HORROR = {"kind": "genre", "value": "horror", "text": "avoid genre: horror"}
GORE = {"kind": "theme", "value": "gore", "text": "avoid theme: gore"}


def test_active_genre_rule_is_a_hard_filter_on_taste_route(agent, monkeypatch):
    log = run(agent, monkeypatch, FakeSession([(1.0, basis(0))], prefs=[HORROR, GORE]), "surprise me",
              decomposition={"open_ended": True})
    assert {"bool": {"must_not": [{"match": {"genres": "horror"}}]}} in log["taste_filters"]
    assert "standing avoids: horror, gore" in log["reduce_q"]
    assert "avoid genre: horror" in log["understand_input"]  # rules shown for override detection


def test_override_lifts_rules_for_this_request(agent, monkeypatch):
    out = {"genres_include": ["horror"], "search_text": "gory slasher",
           "overrides": ["avoid genre: horror", "avoid theme: gore"]}
    log = run(agent, monkeypatch, FakeSession([], prefs=[HORROR, GORE]), "a gory slasher", decomposition=out)
    assert log["fanout"] and "standing avoids" not in log["reduce_q"]


def test_requested_genre_overrides_genre_rule_without_model_flag(agent, monkeypatch):
    log = run(agent, monkeypatch, FakeSession([], prefs=[HORROR, GORE]), "a horror film",
              decomposition={"genres_include": ["horror"], "search_text": "horror film"})
    assert log["fanout"] and "standing avoids: gore" in log["reduce_q"]  # theme rule still active


def test_rule_genre_dropped_from_fanout_candidates(agent):
    cands = {"a": "A [a] | 2020 | genres: horror, thriller", "b": "B [b] | 2020 | genres: thriller"}
    assert agent.catchall.drop_excluded_genres(cands, ["horror"]) == {"b": cands["b"]}


def test_kids_limit_is_enforced_and_never_shown_to_the_model(agent, monkeypatch):
    log = run(agent, monkeypatch, FakeSession([], prefs=[], limits={"kids": True, "genres": ["horror"]}),
              "a scary movie", decomposition={"genres_include": ["horror"], "search_text": "scary movie",
                                              "overrides": []})
    assert log["fanout_kids"] is True and "catch_all" not in log  # fan-out reads only the kids shard
    assert "horror" not in log["understand_input"].split("[Latest message]")[0]


def test_kids_with_another_filter_uses_catch_all_with_kids_ratings(agent, monkeypatch):
    log = run(agent, monkeypatch, FakeSession([], prefs=[], limits={"kids": True, "genres": []}),
              "pixar films from the 2000s", decomposition={"search_text": "pixar films", "min_year": 2000})
    assert "fanout" not in log and log["catch_all_d"]["kids"] is True


@pytest.mark.parametrize("model_out,expect", [
    ({"kids": True, "search_text": "robot superhero"}, "fanout"),
    ({"kids": True, "person": "Tom Hanks"}, "catch_all"),
])
def test_kids_alone_is_not_a_filter(agent, monkeypatch, model_out, expect):
    und = sys.modules["understand"]
    monkeypatch.setattr(und, "call", lambda *a, **kw: (json.dumps(model_out), {}, 0.0))
    assert und.understand("anything", [], [])["path"] == expect


def _fanout_with_answer(agent, monkeypatch, answer):
    fo = sys.modules["fanout"]
    monkeypatch.setattr(fo, "call", lambda *a, **kw: (answer, {}, 0.0))
    monkeypatch.setattr(fo, "_shard_system", lambda sid: sid)
    monkeypatch.setitem(sys.modules, "retrieval", types.SimpleNamespace(fetch_records_by_ids=lambda ids: {}))
    return fo


def test_kids_fanout_reads_only_kids_shard_in_requested_format(agent, monkeypatch):
    cat = sys.modules["catalog"]
    kids = cat.SHARD_TITLES["kids_00"]
    movie = next(t for t, ty in kids.items() if ty == "movie")
    # The bundled catalog has no series; a synthetic kids series checks the format filter.
    series = "tt9999901"
    monkeypatch.setitem(kids, series, "tvSeries")
    adult = next(t for t in cat.SHARD_TITLES["nonkids_movie_00"])
    fo = _fanout_with_answer(agent, monkeypatch, f"A [{movie}]\nB [{series}]\nC [{adult}]\nD [tt0000000]")
    assert cat.select_shards("movie", kids=True) == ["kids_00"]
    assert set(fo.fan_out("q", "movie", kids=True)["candidates"]) == {movie}
    assert set(fo.fan_out("q", "both", kids=True)["candidates"]) == {movie, series}


def test_fanout_drops_ids_not_in_the_shards_asked(agent, monkeypatch):
    cat = sys.modules["catalog"]
    adult = next(t for t in cat.SHARD_TITLES["nonkids_movie_00"])
    fo = _fanout_with_answer(agent, monkeypatch, f"A [{adult}]\nB [tt0000000]")
    assert set(fo.fan_out("q", "movie")["candidates"]) == {adult}


def test_kids_shard_holds_only_kids_ratings(agent):
    import csv
    kids_csv = AGENT / "shards" / "kids_00.csv"
    allowed = set(sys.modules["router"].KIDS_MPAA_RATINGS)
    rows = list(csv.DictReader(open(kids_csv, encoding="utf-8")))
    assert rows and all(set(r["mpaa_ratings"].split("; ")) & allowed for r in rows)
    assert not any("tv-pg" in r["mpaa_ratings"].split("; ") for r in rows)


def test_every_watchable_adult_title_is_in_a_fanout_shard():
    # Every non-kids title a viewer can watch must be reachable through fan-out
    # (TV-PG titles moved out of kids, shorts, direct-to-video). Video games are
    # never recommended, so no shard may hold one.
    import csv
    manifest = json.loads((AGENT / "shards" / "manifest.json").read_text())
    ids, types = set(), set()
    for s in manifest["shards"]:
        rows = list(csv.DictReader(open(AGENT / s["csv"], encoding="utf-8")))
        types |= {r["title_type"] for r in rows}
        if s["category"] != "kids":
            ids |= {r["title_id"] for r in rows}
    assert "videoGame" not in types
    # The bundled catalog is movies only (no TV shards: tv requests use the movie shards).
    assert {s["category"] for s in manifest["shards"]} == {"kids", "movie"}
    rows = csv.DictReader(open(AGENT / "movie_index_non_kids.csv", encoding="utf-8"))
    missing = [r["title_id"] for r in rows if r["title_type"] != "videoGame" and r["title_id"] not in ids]
    assert not missing


def test_shards_cover_the_bundled_catalog_except_microdramas():
    # build_kids_split.py reads data/catalog; micro-dramas are reached through
    # catch-all search only, so no shard holds one.
    import csv
    catalog = []
    for path in sorted((ROOT / "data/catalog").glob("*.json")):
        catalog += json.loads(path.read_text())
    manifest = json.loads((AGENT / "shards" / "manifest.json").read_text())
    sharded = set()
    for s in manifest["shards"]:
        sharded |= {r["title_id"] for r in csv.DictReader(open(AGENT / s["csv"], encoding="utf-8"))}
    assert sharded == {r["title_id"] for r in catalog if r["title_type"] != "micro-drama"}
    assert not any(t.startswith("md") for t in sharded)


def test_every_search_excludes_video_games():
    ret = sys.modules["retrieval"]
    games = {"bool": {"must_not": [{"terms": {"title_type": ["videoGame"]}}]}}
    assert games in ret._filter_clauses(None, movie_only=False)
    assert games in ret._filter_clauses([{"term": {"kids": True}}], movie_only=True, exclude_ids=["tt1"])


def test_shard_warmer_copy_matches_agent_shards():
    warmer = AGENT.parent / "shard-warmer" / "shards"
    agent = json.loads((AGENT / "shards" / "manifest.json").read_text())
    assert json.loads((warmer / "manifest.json").read_text()) == agent
    for s in agent["shards"]:
        name = Path(s["system_txt"]).name
        assert (warmer / name).read_text(encoding="utf-8") == (AGENT / "shards" / name).read_text(encoding="utf-8")


def test_watched_excluded_in_reduce_on_fanout(agent, monkeypatch):
    log = run(agent, monkeypatch, FakeSession([]), "a tense thriller")
    assert log["fanout"] and set(log["reduce_excl"]) == {"tt-last", "tt-w"}


def test_named_title_is_not_excluded_in_reduce(agent, monkeypatch):
    # A title asked for by name is returned even if it was watched or already
    # shown this session ("play the one you showed me").
    log = run(agent, monkeypatch, FakeSession([]), "play Quantum Storm",
              decomposition={"exact_title": "Quantum Storm", "search_text": "Quantum Storm"})
    assert log["reduce_excl"] == []


def test_catch_all_receives_taste_and_watched(agent, monkeypatch):
    taste = [(1.0, basis(0))]
    log = run(agent, monkeypatch, FakeSession(taste), "kids movies",
              decomposition={"category": "movie", "kids": True, "min_year": 2000})
    assert log["catch_all"] == {"taste": taste, "exclude_ids": ["tt-w"]}


def test_all_candidates_excluded_falls_back_instead_of_empty(agent, monkeypatch):
    # Retrieval returns only titles already recommended/watched -> semantic fallback
    # with the same exclusions, never an empty answer.
    blank = {"inputTokens": 0}
    m = agent.agent_v2
    seen = {}
    monkeypatch.setattr(m, "catch_all_semantic", lambda q, movie_only=True, exclude_ids=None, filters=None: seen.update(seed=q, excl=exclude_ids) or {"candidates": {"tt-new": "New [tt-new] | genres: comedy"}, "usage": blank})
    log = {}
    und = sys.modules["understand"]
    out = {"genres_include": ["comedy"], "search_text": "whimsical, stylish adventure like The Grand Budapest Hotel"}
    monkeypatch.setattr(und, "call", lambda *a, **kw: (json.dumps(out), blank, 0.0))
    monkeypatch.setattr(m, "fan_out", lambda q, c, kids=False: {"candidates": {"tt-last": "Old [tt-last] | genres: comedy"}, "usage": blank})
    monkeypatch.setattr(m, "reduce", lambda q, c, exclude_ids=None, **kw: log.update(cands=list(c)) or {"response": "r", "title_ids": list(c), "tappable_options": [], "usage": blank})
    monkeypatch.setattr(m, "build_recommendations", lambda ids: ([], {}))
    list(m.run_agent_stream("something like the Grand Budapest", session=FakeSession([])))
    assert log["cands"] == ["tt-new"] and set(seen["excl"]) == {"tt-last", "tt-w"}
    assert "Grand Budapest" in seen["seed"]


def _ladder(agent, monkeypatch, model_out, catch_results, semantic_results, limits=None):
    """Run the orchestrator with the main search and each ladder step stubbed.
    `catch_results` / `semantic_results` are consumed one per call."""
    blank = {"inputTokens": 0}
    m = agent.agent_v2
    log = {"catch": [], "semantic": []}
    und = sys.modules["understand"]
    monkeypatch.setattr(und, "call", lambda *a, **kw: (json.dumps(model_out), blank, 0.0))
    catch_iter, sem_iter = iter(catch_results), iter(semantic_results)
    monkeypatch.setattr(m, "catch_all", lambda d, q, **kw: log["catch"].append(dict(d)) or
                        {"candidates": next(catch_iter, {}), "usage": blank})
    monkeypatch.setattr(m, "catch_all_semantic", lambda q, movie_only=True, exclude_ids=None, filters=None:
                        log["semantic"].append({"movie_only": movie_only, "excl": exclude_ids, "filters": filters})
                        or {"candidates": next(sem_iter, {}), "usage": blank})
    monkeypatch.setattr(m, "fan_out", lambda q, c, kids=False: {"candidates": {}, "usage": blank})
    monkeypatch.setattr(m, "reduce", lambda q, c, exclude_ids=None, **kw: log.update(cands=list(c), relaxed=kw.get("relaxed"))
                        or {"response": "r", "title_ids": list(c), "tappable_options": [], "usage": blank})
    monkeypatch.setattr(m, "build_recommendations", lambda ids: ([], {}))
    list(m.run_agent_stream("q", session=FakeSession([], limits=limits)))
    return log


def test_ladder_drops_rating_before_years(agent, monkeypatch):
    out = {"search_text": "thrillers", "min_year": 1980, "max_year": 1989, "min_rating": 8}
    log = _ladder(agent, monkeypatch, out, [{}, {"a": "A [a]"}], [])
    assert len(log["catch"]) == 2 and log["catch"][1]["min_rating"] is None and log["catch"][1]["min_year"] == 1980
    assert log["cands"] == ["a"] and log["relaxed"] == ["the 8+ rating"] and not log["semantic"]


def test_ladder_walks_to_semantic_then_widens_format_then_shown(agent, monkeypatch):
    out = {"search_text": "thrillers", "min_year": 1980}
    log = _ladder(agent, monkeypatch, out, [{}, {}], [{}, {}, {"b": "B [b]"}])
    assert [s["movie_only"] for s in log["semantic"]] == [True, False, False]
    assert set(log["semantic"][1]["excl"]) == {"tt-last", "tt-w"} and log["semantic"][2]["excl"] == ["tt-w"]
    assert log["relaxed"] == ["titles from 1980 on", "the movies-only format", "titles already suggested"]


def test_ladder_keeps_kids_ratings_on_every_step(agent, monkeypatch):
    out = {"search_text": "robots", "category": "both"}
    log = _ladder(agent, monkeypatch, out, [], [{}, {"c": "C [c]"}], limits={"kids": True})
    assert log["semantic"] and all(s["filters"] for s in log["semantic"])


def test_no_ladder_when_main_search_has_results(agent, monkeypatch):
    out = {"search_text": "thrillers", "min_year": 1980}
    log = _ladder(agent, monkeypatch, out, [{"a": "A [a]"}], [])
    assert log["relaxed"] is None and len(log["catch"]) == 1


def test_relaxed_reply_says_the_full_request_found_nothing(agent, monkeypatch):
    red = sys.modules["reduce"]
    seen = {}
    monkeypatch.setattr(red, "call", lambda s, u, model_id=None: seen.update(u=u) or
                        (json.dumps({"response": "Tense picks.", "title_ids": ["a"]}), {}, 0.0))
    r = red.reduce("thrillers", {"a": "A [a]"}, relaxed=["the 8+ rating", "the 1980-1989 date range"])
    assert r["response"] == "I couldn't find an exact match, so here are the closest picks. Tense picks."
    assert "the 8+ rating" not in r["response"]  # details go to the reranker only
    assert "Search note" in seen["u"] and "the 8+ rating" in seen["u"]
    plain = red.reduce("thrillers", {"a": "A [a]"})
    assert plain["response"] == "Tense picks."


def test_relaxed_reply_drops_a_second_introduction(agent, monkeypatch):
    red = sys.modules["reduce"]
    seen = {}
    monkeypatch.setattr(red, "call", lambda s, u, model_id=None: seen.update(u=u) or
                        (json.dumps({"response": "Here are some engaging picks.", "title_ids": ["a"]}), {}, 0.0))
    r = red.reduce("thrillers", {"a": "A [a]"}, relaxed=["the 8+ rating"])
    assert r["response"] == "I couldn't find an exact match, so here are the closest picks."
    assert "do NOT open with 'Here are'" in seen["u"]
    plain = red.reduce("thrillers", {"a": "A [a]"})
    assert plain["response"] == "Here are some engaging picks."  # normal replies untouched


def test_question_about_shown_titles_answers_without_search(agent, monkeypatch):
    blank = {"inputTokens": 0}
    m = agent.agent_v2
    und = sys.modules["understand"]
    monkeypatch.setattr(und, "call", lambda *a, **kw: (json.dumps({"intent": "answer"}), blank, 0.0))
    called = {}
    monkeypatch.setattr(m, "answer_question", lambda q, h, ids: called.update(q=q, ids=ids) or
                        {"response": "It's rated PG.", "title_ids": ["tt1"], "tappable_options": ["More like it"], "usage": blank})
    for name in ["fan_out", "catch_all", "catch_all_taste", "reduce"]:
        monkeypatch.setattr(m, name, lambda *a, **kw: pytest.fail(f"{name} must not run on an answer turn"))
    s = FakeSession([], turns=[{"query": "shark movies", "recommended_ids": ["tt1", "tt2"]}])
    events = list(m.run_agent_stream("is the first one ok for kids?", session=s))
    final = events[-1]
    assert final["intent"] == "answer" and final["response"] == "It's rated PG."
    assert final["movie-recommendation"] == [] and final["tappable_options"] == ["More like it"]
    assert called["ids"] == ["tt1", "tt2"]
    assert s.added and s.added[-1][1] == []  # recorded with no titles


def test_answer_turn_keeps_the_last_batch_in_memory(agent):
    sys.path.insert(0, str(AGENT))
    try:
        saved = sys.modules.pop("config", None)
        session_mod = importlib.import_module("session")
    finally:
        sys.path.remove(str(AGENT))
        sys.modules.pop("config", None)
        if saved is not None:
            sys.modules["config"] = saved
    s = session_mod.SessionMemory()
    recs = [{"title_id": "tt1", "originalTitle": "Jaws"}]
    s.add_turn("shark movies", recs)
    s.add_turn("is it scary?", [], response="Quite, yes.")
    assert s.get_last_recommendations() == recs and s.get_excluded_ids() == ["tt1"]


def test_shown_titles_most_recent_first(agent):
    answer = sys.modules["answer"]
    s = FakeSession([], turns=[{"recommended_ids": ["a", "b"]}, {"recommended_ids": []},
                               {"recommended_ids": ["c", "a"]}])
    assert answer.shown_titles(s) == ["c", "a", "b"]


def _agent_module(name):
    """Import one agent module with the agent's own `config` in place."""
    saved = sys.modules.pop("config", None)
    sys.path.insert(0, str(AGENT))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(AGENT))
        sys.modules.pop("config", None)
        if saved is not None:
            sys.modules["config"] = saved


def test_session_excludes_everything_shown_newest_first_capped(agent):
    session_mod = _agent_module("session")
    cap = session_mod.MAX_EXCLUDE_IDS
    s = session_mod.SessionMemory()
    s.add_turn("q1", [{"title_id": "a"}, {"title_id": "b"}])
    s.add_turn("q2", [{"title_id": "c"}, {"title_id": "a"}])
    s.add_turn("question", [], response="An answer.")
    assert s.get_excluded_ids() == ["c", "a", "b"]
    for n in range(cap):
        s.add_turn(f"more {n}", [{"title_id": f"x{n}"}])
    ids = s.get_excluded_ids()
    assert len(ids) == cap and ids[0] == f"x{cap - 1}" and "b" not in ids


def test_agentcore_memory_writes_event_off_the_request_path(agent, monkeypatch):
    import threading
    memory = _agent_module("memory")
    started, release, done = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class SlowClient:
        def create_event(self, **kw):
            started.set()
            release.wait(5)
            calls.append(kw)
            done.set()

    monkeypatch.setattr(memory, "_get_memory_client", lambda: SlowClient())
    m = memory.AgentCoreMemory("mem", "sess", "actor")
    m.add_turn("shark movies", [{"title_id": "tt1", "originalTitle": "Jaws"}])
    # add_turn returned while create_event is still blocked; local state is already updated.
    assert started.wait(5) and not done.is_set()
    assert m.get_excluded_ids() == ["tt1"] and m.get_last_recommendations()[0]["title_id"] == "tt1"
    release.set()
    assert done.wait(5)
    ev = calls[0]
    assert ev["session_id"] == "sess" and ev["actor_id"] == "actor"
    assert ev["messages"] == [("shark movies", "user"), ("Recommended: Jaws", "assistant")]
    assert ev["event_timestamp"].tzinfo is not None


# --- L1 behaviour facts (SESSION#behavior) ---------------------------------

BEHAVIOR = {
    "session_id": "s-1", "device": "phone",
    "views": {"tt-open": {"item": "tt-open", "type": "movie", "title": "Heat", "watched_s": 600, "plays": 1}},
    "recent": [{"item": "md-1", "type": "microdrama", "parent": "tt-parent", "title": "Paddington 2",
                "completion": 1.0, "watched_s": 20, "plays": 3, "reason": "ended"},
               {"item": "clip-x", "type": "trailer", "completion": 0.1, "watched_s": 2, "reason": "swipe"}],
    "format_mix": {"short_min": 6.5, "long_min": 10},
    "entities": {"genre:comedy": {"m": 4.2, "label": "Comedy"}, "person:al_pacino": {"m": 9.0, "label": "Al Pacino"}},
    "live": {"game_id": "SYN-MAT-0001"},
}


def test_behavior_viewed_ids_are_catalog_titles_only(agent):
    sb = sys.modules["session_behavior"]
    assert sb.viewed_ids(BEHAVIOR) == ["tt-open", "tt-parent"]
    assert sb.viewed_ids(None) == []


def test_behavior_context_line_states_facts_without_mode_labels(agent):
    line = sys.modules["session_behavior"].context_line(BEHAVIOR)
    assert "6.5 min of short-form and 10.0 min of long-form on phone" in line
    assert "Heat (movie): watching now, 10.0 min" in line
    assert "a microdrama from Paddington 2: 100% watched, 20 s, played 3x, finished" in line
    assert "Al Pacino (9.0), Comedy (4.2)" in line and "live game SYN-MAT-0001" in line
    for label in ("quick_clips", "lean_back", "following_live", "mode"):
        assert label not in line
    assert sys.modules["session_behavior"].context_line({}) == ""


def test_behavior_from_another_session_is_ignored(agent):
    sb = sys.modules["session_behavior"]
    assert sb.for_session(BEHAVIOR, "s-1") is BEHAVIOR
    assert sb.for_session(BEHAVIOR, "s-2") is None
    assert sb.for_session(BEHAVIOR, "") is None and sb.for_session(None, "s-1") is None


class BehaviorSession(FakeSession):
    def __init__(self, behavior, **kw):
        super().__init__([], **kw)
        self.behavior, self.actor_id, self.session_id = behavior, "p-" + "a" * 32, "s-1"

    def get_session_behavior(self):
        return self.behavior


def test_played_titles_are_excluded_and_facts_reach_the_rerank(agent, monkeypatch):
    emitted = []
    monkeypatch.setattr(agent.agent_v2, "emit_impressions", lambda *a: emitted.append(a))
    log = run(agent, monkeypatch, BehaviorSession(BEHAVIOR), "a thriller")
    assert {"tt-open", "tt-parent", "tt-w"} <= set(log["reduce_excl"])
    assert "This session's viewing" in log["reduce_prefs"] and "Viewer profile" in log["reduce_prefs"]
    assert emitted and emitted[0][0] == "p-" + "a" * 32 and emitted[0][1] == "s-1"


def test_no_behavior_leaves_the_turn_unchanged(agent, monkeypatch):
    monkeypatch.setattr(agent.agent_v2, "emit_impressions", lambda *a: None)
    log = run(agent, monkeypatch, BehaviorSession(None), "a thriller")
    assert log["reduce_prefs"] == "Viewer profile: likes sci-fi"


def test_impression_events_match_the_contract(agent):
    sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))
    contract = importlib.import_module("contract")
    ep = sys.modules["events_producer"]
    events = ep.impression_events("p-" + "a" * 32, "s-1", 3, ["tt1", "tt2"])
    assert [e["payload"]["rank"] for e in events] == [1, 2] and events[0]["payload"]["slate_id"] == "s-1:3"
    for e in events:
        client = {k: v for k, v in e.items() if k not in {"server_ts", "account_id", "booth"}}
        client["device"] = "phone"  # server producer has no device; the processor keeps the session's
        assert contract.validate(client) is None


# --- L2 taste text (AFFINITIES -> profile block) --------------------------------

ARRIVAL_AFF = {"version": 3, "notes": ["preferred era: 2010s-present"], "entities": {
    "genre:sci-fi": {"s": 0.895, "n": 12, "label": "Sci-Fi", "last": 1_000_000},
    "genre:drama": {"s": 0.895, "n": 12, "label": "Drama", "last": 1_000_000},
    "person:christopher_nolan": {"s": 0.6, "n": 6, "label": "Christopher Nolan"},
    "genre:romance": {"s": 0.45, "n": 4, "label": "romance", "last": 1_000_000},
    "genre:horror": {"s": -0.5, "n": 5, "label": "Horror", "last": 1_000_000},
    "genre:mystery": {"s": 0.195, "n": 2, "label": "mystery", "last": 1_000_000},   # too little evidence
    "person:amy_adams": {"s": 0.9, "n": 2, "label": "Amy Adams", "last": 1_000_000},  # too little evidence
}}


def test_affinities_render_with_design_templates(agent):
    up = _agent_module("user_profile")
    lines = up.render_affinities(ARRIVAL_AFF, now=1_000_000)
    assert lines[0] == "- Strongly enjoys: Sci-Fi (genre), Drama (genre), Christopher Nolan (person)"
    assert lines[1] == "- Enjoys: romance (genre)"
    assert lines[2] == "- Tends to skip: Horror (genre)"
    assert lines[3] == "- Seeded persona notes (not from viewing): preferred era: 2010s-present"
    assert not any("mystery" in l or "Amy Adams" in l for l in lines)


@pytest.mark.parametrize("utc,tz,bucket", [
    ("2026-10-05T01:30:00", -240, "weekend|evening"),   # Sun 21:30 local
    ("2026-10-05T07:30:00", -240, "weekend|evening"),   # Mon 03:30 local -> still Sunday night
    ("2026-10-05T09:30:00", -240, "weekday|daytime"),   # Mon 05:30 local
    ("2026-10-05T20:59:00", -240, "weekday|daytime"),   # Mon 16:59 local
    ("2026-10-05T21:00:00", -240, "weekday|evening"),   # Mon 17:00 local
    ("2026-10-10T03:30:00", -240, "weekday|evening"),   # Fri 23:30 local
    ("2026-10-10T14:00:00", 120, "weekend|daytime"),    # Sat 16:00 in UTC+2
    ("2026-10-10T14:00:00", None, None),                # no offset, no bucket
])
def test_time_bucket_matches_between_processor_and_agent(agent, utc, tz, bucket):
    sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))
    l1 = importlib.import_module("l1")
    up = _agent_module("user_profile")
    epoch = datetime.strptime(utc, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    assert l1.time_bucket(epoch, tz) == bucket == up.time_bucket(epoch, tz)


def _ctx(views, long_min, short_min, entities):
    return {"views": views, "long_min": long_min, "short_min": short_min, "entities": entities}


def test_current_bucket_line_needs_enough_viewing(agent):
    up = _agent_module("user_profile")
    now = datetime(2026, 10, 5, 1, 30, tzinfo=timezone.utc).timestamp()     # Sun 21:30 in New York
    ents = {"genre:sci-fi": {"s": 0.5, "n": 3, "label": "Sci-Fi", "last": now},
            "genre:drama": {"s": 0.36, "n": 3, "label": "Drama", "last": now},
            "genre:comedy": {"s": 0.9, "n": 1, "label": "Comedy", "last": now}}   # too few signals
    aff = {"tz_offset_min": -240, "entities": {}, "contexts": {
        "weekend|evening": _ctx(3, 300.0, 10.0, ents),
        "weekday|daytime": _ctx(5, 1.0, 40.0, {})}}
    assert up.render_affinities(aff, now=now) == ["- Weekend evenings (now): mostly Sci-Fi, Drama; full-length titles"]
    # Monday 10:00 local: the weekday-daytime bucket applies instead.
    monday = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc).timestamp()
    assert up.render_affinities(aff, now=monday) == ["- Weekday daytime (now): short clips"]
    # This session's offset wins over the stored one (UTC+9: Monday 23:00 -> weekday evening, no data).
    assert up.render_affinities(aff, now=monday, tz_offset_min=540) == []
    aff["contexts"]["weekend|evening"]["views"] = 2
    assert up.render_affinities(aff, now=now) == []


def test_affinities_fade_at_read_time_and_respect_excluded_genres(agent):
    up = _agent_module("user_profile")
    later = 1_000_000 + 30 * 86400            # one genre half-life later
    lines = up.render_affinities(ARRIVAL_AFF, excluded_genres=["Drama"], now=later)
    # Sci-Fi 0.895 -> 0.4475 (Enjoys); seeded Nolan has no `last` and keeps 0.6; Drama is excluded.
    assert "- Strongly enjoys: Christopher Nolan (person)" in lines
    assert any(l.startswith("- Enjoys: Sci-Fi (genre)") for l in lines)
    assert not any("Drama" in l for l in lines)


class ProfileTable:
    def __init__(self, items):
        self.items, self.reads = items, 0

    def get_item(self, Key):
        self.reads += 1
        item = self.items.get(Key["data_type"])
        return {"Item": item} if item else {}


def test_profile_block_uses_affinities_and_ignores_legacy_preferences(agent, monkeypatch):
    import time as _time
    up = _agent_module("user_profile")
    fresh = json.loads(json.dumps(ARRIVAL_AFF))
    for e in fresh["entities"].values():
        if "last" in e:
            e["last"] = int(_time.time())
    monkeypatch.setattr(up, "_get_table", lambda: ProfileTable({"AFFINITIES": fresh}))
    block = up.get_profile_block("p-1")
    assert block.startswith("Viewer taste (from viewing history") and "Sci-Fi (genre)" in block
    # A leftover PREFERENCES item is never read: no AFFINITIES means no taste block.
    legacy = {"PREFERENCES": {"favorite_genres": ["comedy"], "favorite_directors": ["Nora Ephron"]}}
    monkeypatch.setattr(up, "_get_table", lambda: ProfileTable(legacy))
    assert up.get_profile_block("demo-user-rom_com") == ""
    assert not hasattr(up, "_legacy_profile_block")


def test_profile_text_is_refreshed_during_a_session(agent, monkeypatch):
    memory = _agent_module("memory")
    up = _agent_module("user_profile")
    blocks = iter(["taste v1", "taste v2"])
    monkeypatch.setattr(up, "get_profile_block", lambda user_id, tz=None: next(blocks))
    clock = [100.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(memory, "_get_memory_client", lambda: object())
    m = memory.AgentCoreMemory("mem", "sess", "actor")
    assert m.get_user_profile_context() == "taste v1"
    clock[0] += 30
    assert m.get_user_profile_context() == "taste v1"            # within PROFILE_TTL_S
    clock[0] += 31
    assert m.get_user_profile_context() == "taste v2"            # re-read after the processor updated L2


def test_declared_rules_are_read_once_per_session(agent, monkeypatch):
    memory = _agent_module("memory")
    up = _agent_module("user_profile")
    calls = []
    monkeypatch.setattr(up, "get_declared", lambda profile_id: calls.append(profile_id) or ([], {"kids": False}))
    clock = [100.0]
    monkeypatch.setattr(memory.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(memory, "_get_memory_client", lambda: object())
    m = memory.AgentCoreMemory("mem", "sess", "actor")
    m.get_declared()
    clock[0] += 3600
    m.get_declared()
    assert calls == ["actor"]                                    # a Settings change applies next session