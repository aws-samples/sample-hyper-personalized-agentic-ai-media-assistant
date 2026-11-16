"""Clips feed: the ported POC policy (clips_policy) and the feed Lambda."""

import importlib
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True  # Lambda directories are CDK assets
sys.path.insert(0, str(ROOT / "infra/lambdas/clips_feed"))
sys.path.insert(0, str(ROOT / "infra/layers/shared/python"))

policy = importlib.import_module("clips_policy")

PID = "p-" + "c" * 32


def basis(i, n=8, tilt=0.0, j=None):
    v = [0.0] * n
    v[i] = 1.0
    if j is not None:
        v[j] = tilt
    return v


def title(tid, vec, genres=(), kind="title", rating=7.0, name=None):
    return {"id": tid, "title": name or tid, "kind": kind, "genres": list(genres), "plot": "", "image": f"https://img/{tid}.jpg",
            "rating": rating, "year": 2020, "cast": [], "maturity": None, "vector": vec}


# --- signal / impressions -----------------------------------------------------

@pytest.mark.parametrize("imp,w", [
    ({"dwell_ms": 1500}, 0.0),                      # a swipe is neutral
    ({"dwell_ms": 3000}, 0.0375),                   # the POC's 3-second pause
    ({"dwell_ms": 8000}, 0.225),
    ({"dwell_ms": 30000}, 0.3),                     # dwell saturates at 10 s
    ({"dwell_ms": 0, "feedback": "like"}, 1.0),
    ({"dwell_ms": 10000, "feedback": "more"}, 1.8),
    ({"dwell_ms": 10000, "feedback": "dislike"}, -1.0),
])
def test_signal_matches_poc(imp, w):
    assert policy.signal(imp) == pytest.approx(w)


def test_merge_impressions_keeps_max_dwell_latest_feedback_and_caps():
    imps = policy.merge_impressions({}, [{"item_id": "a", "dwell_ms": 5000}], 100)
    imps = policy.merge_impressions(imps, [{"item_id": "a", "dwell_ms": 3000, "feedback": "like"}], 110)
    assert imps["a"] == {"dwell_ms": 5000, "feedback": "like", "order": 1, "at": 110}
    imps = policy.merge_impressions(imps, [{"item_id": "a", "feedback": "none"}], 120)
    assert imps["a"]["feedback"] == "none"                                   # an un-like clears it
    many = policy.merge_impressions(imps, [{"item_id": f"t{i}", "dwell_ms": 1} for i in range(80)], 130)
    assert len(many) == policy.IMPRESSIONS_MAX and "a" not in many          # oldest dropped


def test_rebuild_merges_similar_titles_and_separates_dislikes():
    titles = {"a": title("a", basis(0)), "b": title("b", basis(0, tilt=0.3, j=1)),
              "c": title("c", basis(2)), "d": title("d", basis(3))}
    imps = {"a": {"dwell_ms": 10000, "order": 1, "at": 1000}, "b": {"dwell_ms": 0, "feedback": "like", "order": 2, "at": 1000},
            "c": {"dwell_ms": 10000, "order": 3, "at": 1000}, "d": {"dwell_ms": 0, "feedback": "dislike", "order": 4, "at": 1000}}
    pos, neg = policy.rebuild(imps, titles, 1000)
    assert [p["sources"] for p in pos] == [["a", "b"], ["c"]]
    assert pos[0]["w"] == pytest.approx(1.3) and len(neg) == 1 and neg[0]["sources"] == ["d"]


def test_rebuild_decays_on_a_ten_minute_half_life():
    pos, _ = policy.rebuild({"a": {"dwell_ms": 0, "feedback": "like", "order": 1, "at": 0}},
                            {"a": title("a", basis(0))}, 600)
    assert pos[0]["w"] == pytest.approx(0.5)


def test_interests_use_l2_centroids_as_priors():
    priors = [(0.7, basis(0)), (0.3, basis(1))]
    cold = policy.interests([], priors)
    assert [round(x["w"], 2) for x in cold] == [0.42, 0.18] and {x["kind"] for x in cold} == {"taste"}
    warm = policy.interests([{"v": basis(2), "w": 1.0, "sources": ["x"]}], priors)
    assert [x["kind"] for x in warm] == ["session", "taste"]
    assert warm[0]["w"] == 0.5 and warm[1]["w"] == 0.2 and warm[1]["prior"]   # one bounded prior query


# --- select -------------------------------------------------------------------

MDS = [title("md1", basis(0, tilt=0.4, j=5), ["sci-fi"], "microdrama", 6.5, "Neon Runner"),
       title("md2", basis(1, tilt=0.4, j=5), ["romance"], "microdrama", 6.5, "Dust & Devotion")]
SCIFI = [title(f"sf{i}", basis(0, tilt=0.05 * i, j=6), ["sci-fi"], rating=8.0) for i in range(6)]
ROMANCE = [title(f"ro{i}", basis(1, tilt=0.05 * i, j=6), ["romance"], rating=8.0) for i in range(6)]
TOP = [title(f"top{i}", basis(2 + i % 3), ["drama"], rating=9.0) for i in range(10)]


def near(vec, k, pool=SCIFI + ROMANCE + TOP):
    return sorted(pool, key=lambda t: -policy.cosine(vec, t["vector"]))[:k]


def test_microdramas_lead_the_feed():
    ints = policy.interests([], [(1.0, basis(0))])
    picks = policy.select(ints, [], [near(x["v"], 40) for x in ints], MDS, TOP, 6, set())
    ids = [t["id"] for t, _, _ in picks]
    assert ids[0] == "md1" and picks[0][2]["microdrama_boost"]
    assert sum(r != "for-you" for _, r, _ in picks) >= 1                    # always a discovery slot
    assert len(set(ids)) == 6


def test_cold_start_without_taste_is_all_discovery_with_microdramas_first():
    picks = policy.select([], [], [], MDS, TOP, 6, set(), seed="s")
    assert [r for _, r, _ in picks] == ["discover"] * 6
    ids = [t["id"] for t, _, _ in picks]
    # Boosted microdramas rank ahead of higher-rated titles; diversity still spaces them.
    assert ids[0] == "md1" and "md2" in ids[:3]


def test_excluded_and_disliked_titles_drop_out():
    ints = policy.interests([], [(1.0, basis(0))])
    picks = policy.select(ints, [], [near(x["v"], 40) for x in ints], MDS, TOP, 6, {"md1", "sf0"})
    ids = [t["id"] for t, _, _ in picks]
    assert "md1" not in ids and "sf0" not in ids
    neg = [{"v": basis(0), "w": 1.0}]
    pen = policy.select(ints, neg, [near(x["v"], 40) for x in ints], MDS, TOP, 6, set())
    rel = {t["id"]: why["relevance"] for t, _, why in pen}
    base = {t["id"]: why["relevance"] for t, _, why in policy.select(ints, [], [near(x["v"], 40) for x in ints], MDS, TOP, 6, set())}
    assert all(rel[i] < base[i] for i in rel if i in base and i.startswith("sf"))


def test_explanation_names_the_title_that_contributed_most():
    # Live test: Neon Runner watched 30 s, the other microdramas ~2.5 s, all merged.
    titles = {"a": title("a", basis(0), name="Long Way To Gold"), "b": title("b", basis(0, tilt=0.2, j=1), name="Neon Runner"),
              "c": title("c", basis(0, tilt=0.3, j=2), name="Dragon's Soul")}
    imps = {"a": {"dwell_ms": 2500, "order": 1, "at": 0}, "b": {"dwell_ms": 30000, "order": 2, "at": 0},
            "c": {"dwell_ms": 2500, "order": 3, "at": 0}}
    pos, _ = policy.rebuild(imps, titles, 0)
    assert len(pos) == 1 and pos[0]["sources"][-1] == "Dragon's Soul"
    assert policy.strongest_source(pos[0]) == "Neon Runner"
    assert policy._explain("session", policy.interests(pos, [])[0])["summary"] == "Because you watched Neon Runner"


def test_loosely_related_titles_stay_separate_interests():
    # cosine 0.75: typical of unrelated titles in this catalog's Nova space.
    a, b = basis(0), [0.75, (1 - 0.75 ** 2) ** 0.5] + [0.0] * 6
    assert policy.cosine(a, b) == pytest.approx(0.75)
    pos, _ = policy.rebuild({"a": {"dwell_ms": 0, "feedback": "like", "order": 1, "at": 0},
                             "b": {"dwell_ms": 0, "feedback": "more", "order": 2, "at": 0}},
                            {"a": title("a", a), "b": title("b", b)}, 0)
    assert len(pos) == 2


def test_long_term_taste_counts_toward_personalized_slots():
    priors = [(1.0, basis(0))]
    weak = [{"v": basis(1), "w": 0.05, "sources": ["x"], "contrib": {"x": 0.05}}]   # a brief pause
    ints = policy.interests(weak, priors)
    assert ints[-1]["w"] == 0.2 and ints[-1]["strength"] == 0.6                      # bounded query, full strength
    picks = policy.select(ints, [], [near(x["v"], 40) for x in ints], [], TOP, 6, set())
    assert sum(r == "for-you" for _, r, _ in picks) == 4                              # round(6 x 0.6)


def test_keep_count_replaces_the_tail_after_strong_feedback():
    assert policy.keep_count(["a", "b", "c"], [{"item_id": "x", "dwell_ms": 9000}]) == 3
    assert policy.keep_count(["a", "b", "c"], [{"item_id": "x", "feedback": "more"}]) == 1
    assert policy.keep_count([], [{"item_id": "x", "feedback": "dislike"}]) == 0


# --- the Lambda, against fake tables and a fake catalog -----------------------

CATALOG = {t["id"]: t for t in MDS + SCIFI + ROMANCE + TOP + [title("hz", basis(7), ["horror"], rating=9.9),
                                                              title("noaward", basis(0, tilt=0.01, j=7), ["sci-fi"], rating=9.8)]}


def _src(t):
    return {"title_id": t["id"], "originalTitle": t["title"], "title_type": "micro-drama" if t["kind"] == "microdrama" else "movie",
            "genres": t["genres"], "plot": "", "image": ("s3://media/input/posters/" + t["id"] + ".jpg") if t["kind"] == "microdrama"
            else t["image"], "imdb_rating": t["rating"], "premiere_date": "2020-01-01", "principalCastMembers": [],
            "mpaa_ratings": ["pg"], "embedding": t["vector"], "awards": None if t["id"] == "noaward" else ["an award"]}


def _passes(doc, clause):
    if "terms" in clause:
        (field, values), = clause["terms"].items()
        v = doc.get(field)
        return any(x in values for x in (v if isinstance(v, list) else [v]))
    if "term" in clause:
        (field, value), = clause["term"].items()
        return doc.get(field) == value
    if "exists" in clause:
        return doc.get(clause["exists"]["field"]) is not None
    if "match" in clause:
        (field, value), = clause["match"].items()
        return value in (doc.get(field) or [])
    if "bool" in clause:
        b = clause["bool"]
        if "should" in b:
            return any(_passes(doc, c) for c in b["should"])
        return not any(_passes(doc, c) for c in b.get("must_not", []))
    raise AssertionError(clause)


def fake_search(body):
    q = body["query"]
    docs = [_src(t) for t in CATALOG.values()]
    if "script_score" in q:
        fc = q["script_score"]["query"]["bool"]["filter"]
        qv = q["script_score"]["script"]["params"]["query_value"]
        docs = sorted([d for d in docs if all(_passes(d, c) for c in fc)], key=lambda d: -policy.cosine(qv, d["embedding"]))
    elif "terms" in q:
        docs = [d for d in docs if d["title_id"] in q["terms"]["title_id"]]
    else:
        docs = [d for d in docs if all(_passes(d, c) for c in q["bool"]["filter"])]
        if body.get("sort"):
            docs.sort(key=lambda d: -d["imdb_rating"])
    return {"hits": {"hits": [{"_source": d} for d in docs[:body["size"]]]}}


class Table:
    def __init__(self, items=None):
        self.items = {k: json.loads(json.dumps(v), parse_float=Decimal) for k, v in (items or {}).items()}

    def _key(self, Key):
        return tuple(sorted(Key.items()))

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get(self._key(Key))
        return {"Item": item} if item else {}

    def put_item(self, Item):
        key = {k: Item[k] for k in ("profile_id", "sk")}
        self.items[self._key(key)] = Item

    def query(self, **kw):
        return {"Items": [i for i in self.items.values() if str(i.get("sk", "")).startswith("TITLE#") and i.get("completed")]}


@pytest.fixture
def feed(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    # Other Lambdas also name their module lambda_function: load this one by path.
    spec = importlib.util.spec_from_file_location("clips_feed_lambda", ROOT / "infra/lambdas/clips_feed/lambda_function.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for cache in (mod._titles, mod._pools, mod._videos):
        cache.clear()
    centroid = policy.unit(basis(0))
    import base64
    import struct
    tables = {
        "PROFILES_TABLE": Table({(("data_type", "AFFINITIES"), ("user_id", PID)): {
            "user_id": PID, "data_type": "AFFINITIES",
            "centroids": [{"v": base64.b64encode(struct.pack("<8f", *centroid)).decode(), "w": 1.0, "n": 4}]},
            (("data_type", "L3#DECLARED"), ("user_id", PID)): {"user_id": PID, "data_type": "L3#DECLARED",
                                                                "excluded_genres": ["horror"]}}),
        "INTERACTIONS_TABLE": Table({(("profile_id", PID), ("sk", "TITLE#sf1")): {"profile_id": PID, "sk": "TITLE#sf1",
                                                                                 "completed": True}}),
        "VIDEO_MAPPING_TABLE": Table({(("title_id", "md1"),): {"title_id": "md1",
                                                              "video_s3_url": "s3://media/clips/microdramas/neon-runner.mp4"},
                                      (("title_id", "md2"),): {"title_id": "md2",
                                                              "video_s3_url": "s3://media/input/vertical/dust.mp4"}}),
    }
    monkeypatch.setenv("CLIP_MEDIA_PREFIX", "s3://media/clips/")
    monkeypatch.setattr(mod, "_table", lambda env: tables[env])
    monkeypatch.setattr(mod, "_search", fake_search)
    monkeypatch.setattr(mod, "presign", lambda url, expires=3600: f"https://signed/{url[5:]}" if url and url.startswith("s3://") else url)
    mod.tables = tables
    return mod


def test_session_returns_microdramas_first_and_respects_limits(feed):
    out = feed.build(PID, "sess-1", now=1000)
    cards = out["cards"]
    assert out["keep"] == 0 and len(cards) == 6
    assert cards[0]["id"] == "md1" and cards[0]["kind"] == "microdrama"
    assert cards[0]["videoPath"] == "microdramas/neon-runner.mp4"                    # on the media CDN
    assert cards[0]["videoUrl"] == "https://signed/media/clips/microdramas/neon-runner.mp4"
    assert cards[0]["posterUrl"] == "https://signed/media/input/posters/md1.jpg"
    ids = [c["id"] for c in cards]
    assert "hz" not in ids and "sf1" not in ids                                       # excluded genre, watched title
    assert "noaward" not in ids                                                       # discovery needs an award listing
    assert all(c["kind"] == "title_card" for c in cards if not c["id"].startswith("md"))
    md2 = next((c for c in cards if c["id"] == "md2"), None)
    if md2:
        assert md2["videoPath"] is None and md2["videoUrl"].endswith("input/vertical/dust.mp4")
    saved = feed.tables["INTERACTIONS_TABLE"].items[(("profile_id", PID), ("sk", "SESSION#clips"))]
    assert saved["issued"] == ids and saved["impressions"] == {}


def test_more_like_this_reshapes_the_next_refill(feed):
    first = feed.build(PID, "sess-2", now=1000)["cards"]
    queued = [c["id"] for c in first[1:]]
    out = feed.build(PID, "sess-2", queued=queued,
                     updates=[{"item_id": "ro0", "dwell_ms": 9000, "feedback": "more"}], now=1010)
    assert out["keep"] == 1                                                           # the tail is replaced
    new = [c["id"] for c in out["cards"]]
    assert len(new) == 5 and not set(new) & {first[0]["id"], queued[0], "ro0"}
    romance = [c for c in out["cards"] if "romance" in c["genres"]]
    assert len(romance) >= 3 and romance[0]["explanation"]["summary"] == "Because you watched ro0"


def test_a_new_session_id_starts_fresh(feed):
    feed.build(PID, "sess-3", updates=[{"item_id": "ro0", "feedback": "like"}], now=1000)
    out = feed.build(PID, "sess-4", now=2000)
    saved = feed.tables["INTERACTIONS_TABLE"].items[(("profile_id", PID), ("sk", "SESSION#clips"))]
    assert saved["session_id"] == "sess-4" and saved["impressions"] == {} and out["cards"][0]["id"] == "md1"


def test_handler_checks_membership(feed, monkeypatch):
    monkeypatch.setattr(feed, "resolve_profile_pk", lambda *a: None)
    res = feed.lambda_handler({"resource": "/clips/session", "body": json.dumps({"profile_id": PID, "session_id": "s"})}, None)
    assert res["statusCode"] == 404
    res = feed.lambda_handler({"resource": "/clips/session", "body": json.dumps({"profile_id": "p-movie", "session_id": "s"})}, None)
    assert res["statusCode"] == 400
