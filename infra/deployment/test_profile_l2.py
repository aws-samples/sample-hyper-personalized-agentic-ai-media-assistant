"""L2 (AFFINITIES) from L1 signals, and TITLE# watch state."""

import importlib
import json
import math
import sys
import time
import types
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True  # Lambda directories are CDK assets
sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))
sys.path.insert(0, str(ROOT / "infra/lambdas/household"))

l1 = importlib.import_module("l1")
l2 = importlib.import_module("l2")

PID = "p-" + "b" * 32
CATALOG = {
    "tt1001129": {"title": "Wrath", "genres": ["action", "comedy", "thriller"],
                   "cast": ["Fox Clever as (Hero)", "Snake Quick", "Blade Runner", "Lion Heart"]},
    "tt1001113": {"title": "Retaliation", "genres": ["action", "sci-fi", "thriller"],
                  "cast": ["Max Thunder", "Shark Bite", "Scorpion Sting"]},
    "tt-film": {"title": "Parent Film", "genres": ["drama"], "cast": ["Some Actor"]},
}
SEEDED = {"genre:action": {"s": 0.7, "n": 10, "label": "Action"},
          "genre:sci-fi": {"s": 0.7, "n": 10, "label": "Sci-Fi"}}


def ev(etype, ts, payload, sid="3f485953", n=None, surface="vod_player", booth=False):
    stamp = f"2026-10-04T{ts}.000Z"
    return {"event_id": f"{etype}-{ts}-{n or ''}", "schema_version": 1, "event_type": etype, "profile_id": PID,
            "session_id": sid, "device": "phone", "surface": surface, "client_ts": stamp, "server_ts": stamp,
            "booth": booth, "payload": payload}


# Your real browser test (session 3f485953 and cf06652c), as recorded in the S3 log.
BULLET_TRAIN = [
    ev("play_start", "21:55:11", {"item_id": "tt1001129", "content_type": "movie", "position_s": 1, "duration_s": 7680}),
    ev("progress", "21:55:41", {"item_id": "tt1001129", "content_type": "movie", "position_s": 32, "duration_s": 7680, "watched_s": 31}),
    ev("play_end", "21:55:54", {"item_id": "tt1001129", "content_type": "movie", "position_s": 44, "duration_s": 7680,
                                "reason": "closed", "watched_s": 12}),
]
TENET = [
    ev("agent_accept", "22:01:01", {"item_id": "tt1001113", "slate_id": "cf06652c:1"}, sid="cf06652c", surface="agent"),
    ev("play_start", "22:01:07", {"item_id": "tt1001113", "content_type": "movie", "position_s": 1, "duration_s": 7680}, sid="cf06652c"),
    ev("play_end", "22:01:15", {"item_id": "tt1001113", "content_type": "movie", "position_s": 9, "duration_s": 7680,
                                "reason": "closed", "watched_s": 8}, sid="cf06652c"),
]


def cat(item_id):
    return CATALOG.get(item_id)


def fold_signals(events, state=None):
    before = json.loads(json.dumps(state)) if state else None
    after, _ = l1.fold(state, events, cat)
    return l2.signals(before, after), after


# --- engagement --------------------------------------------------------------

@pytest.mark.parametrize("view,e", [
    ({"type": "movie", "watched_s": 43, "completion": 0.006}, None),     # sampling: not scored
    ({"type": "movie", "watched_s": 900, "completion": 0.05}, -0.4),     # real early abandon
    ({"type": "movie", "watched_s": 2000, "completion": 0.3}, 0.1),
    ({"type": "movie", "watched_s": 4000, "completion": 0.6}, 0.5),
    ({"type": "movie", "watched_s": 7000, "completion": 0.95}, 0.8),
    ({"type": "movie", "watched_s": 3, "completion": 1.0}, None),         # 'ended' after a seek
    ({"type": "microdrama", "watched_s": 1, "completion": 0.03}, None),   # swipe past: neutral
    ({"type": "microdrama", "watched_s": 2, "completion": 0.07}, None),   # dwell ramp starts at 2 s
    ({"type": "microdrama", "watched_s": 6, "duration_s": 30}, 0.15),     # dwell ramp, half way
    ({"type": "microdrama", "watched_s": 20, "completion": 0.3}, 0.3),    # dwell ramp full at 10 s
    ({"type": "microdrama", "watched_s": 20, "duration_s": 30}, 0.4),     # completion >= 50%
    ({"type": "microdrama", "watched_s": 60, "completion": 1.0, "plays": 1}, 0.6),
    ({"type": "microdrama", "watched_s": 120, "completion": 1.0, "plays": 3}, 0.8),
    ({"type": "microdrama", "watched_s": 25, "duration_s": 30, "reason": "ended"}, 0.6),
    # Poster cards: completion against a 2-minute cap, never the film's length.
    ({"type": "title_card", "watched_s": 8, "duration_s": 120}, 0.225),
    ({"type": "title_card", "watched_s": 70, "duration_s": 7200}, 0.4),
    ({"type": "title_card", "watched_s": 115, "duration_s": 7200, "reason": "swipe"}, 0.6),
])
def test_view_engagement(view, e):
    assert l2.view_engagement(view) == e


# --- signals from L1 ----------------------------------------------------------

def test_heartbeats_alone_produce_no_signal():
    sigs, state = fold_signals(BULLET_TRAIN[:2])
    assert sigs == []
    sigs, _ = fold_signals(BULLET_TRAIN[2:], state)
    assert [(k, i) for k, i, _, _ in sigs] == [("view", "tt1001129")]


def test_bullet_train_short_play_updates_watch_state_but_not_taste():
    sigs, _ = fold_signals(BULLET_TRAIN)
    [(kind, item, e, view)] = sigs
    assert (kind, item, e) == ("view", "tt1001129", None)
    assert view["watched_s"] == 43 and view["completion"] == 0.006


def test_tenet_accept_is_a_weak_signal_and_short_play_is_not_scored():
    sigs, _ = fold_signals(TENET)
    assert [(k, i, e) for k, i, e, _ in sigs] == [("view", "tt1001113", None), ("agent_accept", "tt1001113", 0.05)]


def test_already_seen_signals_are_not_repeated():
    _, state = fold_signals(TENET)
    sigs, _ = fold_signals([ev("impression", "22:03:00", {"item_id": "tt1"}, sid="cf06652c", surface="agent")], state)
    assert sigs == []


def test_new_session_compares_against_empty():
    _, state = fold_signals(BULLET_TRAIN)          # session 3f485953
    sigs, _ = fold_signals(TENET, state)           # session cf06652c
    assert ("agent_accept", "tt1001113") in [(k, i) for k, i, _, _ in sigs]


def test_rewatch_is_a_new_view_signal():
    start = {"item_id": "md-1", "content_type": "microdrama", "duration_s": 60}
    first = [ev("play_start", "12:00:00", start), ev("play_end", "12:01:00", {**start, "reason": "ended", "watched_s": 60})]
    _, state = fold_signals(first)
    again = [ev("play_start", "12:02:00", start, n=2), ev("play_end", "12:03:00", {**start, "reason": "ended", "watched_s": 60}, n=2)]
    sigs, _ = fold_signals(again, state)
    assert [(k, i, e) for k, i, e, _ in sigs] == [("view", "md-1", 0.8)]   # a replay


# --- entity formula -----------------------------------------------------------

def test_entity_update_formula_on_seeded_values():
    ents = json.loads(json.dumps(SEEDED))
    l2.update_entities(ents, {"genre:action": "action", "genre:thriller": "thriller"}, 0.5, 1_000_000)
    assert ents["genre:action"] == {"s": 0.775, "n": 11, "label": "Action", "last": 1_000_000}   # 0.7 + 0.15*0.5, no decay
    assert ents["genre:thriller"] == {"s": 0.075, "n": 1, "label": "thriller", "last": 1_000_000}


def test_entity_decays_by_half_life_and_clamps():
    ents = {"genre:drama": {"s": 0.8, "n": 3, "label": "Drama", "last": 0},
            "person:x": {"s": 0.8, "n": 3, "label": "X", "last": 0}}
    l2.update_entities(ents, {"genre:drama": "Drama", "person:x": "X"}, 0.0, 30 * 86400)
    assert ents["genre:drama"]["s"] == 0.4                       # one 30-day half-life
    assert math.isclose(ents["person:x"]["s"], 0.8 * 2 ** -0.5, abs_tol=1e-4)   # 60-day half-life
    ents = {"genre:a": {"s": 0.99, "n": 1, "label": "A"}}
    l2.update_entities(ents, {"genre:a": "A"}, 0.8, 1)
    assert ents["genre:a"]["s"] == 1.0


# --- centroids ----------------------------------------------------------------

def basis(i, dim=8):
    v = [0.0] * dim
    v[i] = 1.0
    return v


def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))


def test_first_signal_is_the_centroid_and_its_weight_reflects_evidence():
    [c] = l2.update_centroids([], basis(0), 0.8)
    assert c["vector"] == basis(0) and c["count"] == 1 and c["evidence"] == 0.8
    assert c["weight"] == 0.2857                                  # 0.8 / (0.8 + K=2): never 100%
    [c] = l2.update_centroids([], basis(0), 0.11)
    assert c["weight"] == 0.0521                                  # a 5 s clip says very little


def test_young_centroid_is_an_evidence_weighted_mean():
    a, b = basis(0), [0.6, 0.8] + [0.0] * 6                       # cos(a, b) = 0.6: they join
    [c] = l2.update_centroids(l2.update_centroids([], a, 0.8), b, 0.8)
    assert math.isclose(cos(c["vector"], a), cos(c["vector"], b), rel_tol=1e-9)   # 2nd counts as much as the 1st
    assert c["count"] == 2 and c["weight"] == 0.4444
    [c] = l2.update_centroids(l2.update_centroids([], a, 0.8), b, 0.1)
    assert cos(c["vector"], a) > 0.99                             # a 40% watch barely moves a finished film


def test_step_falls_to_centroid_rate_once_evidence_is_large():
    near = [0.9, 0.1] + [0.0] * 6
    old = {"vector": basis(0), "count": 20, "evidence": 16.0}
    [c] = l2.update_centroids([old], near, 0.8)
    want = l2._unit([(1 - 0.08) * a + 0.08 * b for a, b in zip(basis(0), l2._unit(near))])   # 0.1 * 0.8 > 0.8 / 16.8
    assert all(math.isclose(x, y, abs_tol=1e-12) for x, y in zip(c["vector"], want))
    assert c["count"] == 21 and c["weight"] == round(16.8 / 18.8, 4)


def test_more_evidence_for_a_counted_title_does_not_raise_count():
    [c] = l2.update_centroids(l2.update_centroids([], basis(0), 0.8), basis(0), 0.2, new_title=False)
    assert c["count"] == 1 and math.isclose(c["evidence"], 1.0) and c["weight"] == 0.3333


def test_dissimilar_title_starts_a_centroid_until_the_cap_and_negatives_never_move_taste():
    cents = [{"vector": basis(i), "count": 1, "evidence": 0.8} for i in range(3)]
    capped = l2.update_centroids(cents, basis(6), 0.8)
    assert len(capped) == 3 and sum(c["count"] for c in capped) == 4
    assert max(c["vector"][6] for c in capped) < 0.05             # nudged by CENTROID_RATE / 2 * e only
    assert l2.update_centroids(cents, basis(6), -0.4)[0]["vector"] == basis(0)
    assert len(l2.update_centroids(cents[:1], basis(5), 0.5)) == 2


def test_legacy_centroid_without_evidence_counts_a_completed_title_each():
    stored = l2.encode_centroids([{"vector": basis(1), "weight": 1.0, "count": 3}])
    stored[0].pop("e")
    [c] = l2.decode_centroids(stored)
    assert c["evidence"] == pytest.approx(2.4)
    assert l2.update_centroids([c], basis(1), 0.8)[0]["weight"] == round(3.2 / 5.2, 4)


@pytest.mark.parametrize("parts,e", [
    ({"v": 0.8}, 0.8),
    ({"f": 0.5}, 0.5),
    ({"v": 0.8, "f": 0.5}, 1.0),          # finished, then liked: capped at 1
    ({"v": 0.1, "f": 0.5}, 0.7),          # 40% watched, then liked
    ({"v": 0.8, "f": -0.4}, -0.4),        # a dislike replaces the positives
    ({"v": -0.4, "f": 0.5}, 0.5),
    ({"v": -0.4}, -0.4),
])
def test_one_title_one_combined_e(parts, e):
    assert l2.combine(parts) == e


def test_like_after_watching_adds_only_the_difference():
    contribs = {}
    first = l2.fold_contribution(contribs, "tt1", "view", 0.8, 1000)
    assert first == {"entities": 0.8, "centroid": 0.8, "first": True, "first_centroid": True}
    like = l2.fold_contribution(contribs, "tt1", "feedback", 0.5, 1100)
    assert like == {"entities": 0.2, "centroid": 0.2, "first": False, "first_centroid": False}
    again = l2.fold_contribution(contribs, "tt1", "view", 0.5, 1200)     # a partial rewatch adds nothing
    assert again["entities"] == 0 and again["centroid"] == 0


def test_like_then_watch_ends_at_the_same_place_as_watch_then_like():
    a, b = {}, {}
    steps_a = [l2.fold_contribution(a, "t", "view", 0.8, 1), l2.fold_contribution(a, "t", "feedback", 0.5, 2)]
    steps_b = [l2.fold_contribution(b, "t", "feedback", 0.5, 1), l2.fold_contribution(b, "t", "view", 0.8, 2)]
    assert a["t"]["e"] == b["t"]["e"] == 1.0
    assert sum(s["centroid"] for s in steps_a) == pytest.approx(sum(s["centroid"] for s in steps_b))


def test_dislike_after_watching_lowers_favorites_but_not_taste_and_relike_is_not_double_counted():
    c = {}
    l2.fold_contribution(c, "t", "view", 0.8, 1)
    dis = l2.fold_contribution(c, "t", "feedback", -0.4, 2)
    assert dis["entities"] == -1.2 and dis["centroid"] == 0
    relike = l2.fold_contribution(c, "t", "feedback", 0.5, 3)
    assert relike["entities"] == 1.4 and relike["centroid"] == pytest.approx(0.2)   # centroids saw 0.8 already


def test_contribution_expires_so_a_later_rewatch_counts_again():
    c = {}
    l2.fold_contribution(c, "t", "view", 0.8, 0)
    later = l2.fold_contribution(c, "t", "view", 0.8, (l2.CONTRIB_TTL_DAYS + 1) * 86400)
    assert later["first"] and later["entities"] == 0.8 and later["centroid"] == 0.8
    assert l2.prune_contributions({"old": {"t": 0}, "new": {"t": 100 * 86400}}, 100 * 86400) == {"new": {"t": 100 * 86400}}


def test_poster_card_is_kept_apart_from_the_title_itself():
    assert l2.contribution_key("tt1", {"type": "title_card"}) == "card:tt1"
    assert l2.contribution_key("tt1", {"type": "movie"}) == "tt1" and l2.contribution_key("tt1", None) == "tt1"


def test_processor_clustering_matches_seed_and_agent():
    core = importlib.import_module("household_core")
    vecs = [basis(0), [0.6, 0.4] + [0.0] * 6, basis(3), basis(5), [0, 0, 0, 0.8, 0.2, 0, 0, 0]]
    assert l2.taste_centroids(vecs) == core.taste_centroids(vecs)
    assert (l2.TASTE_JOIN_COSINE, l2.TASTE_MAX_CENTROIDS) == (core.TASTE_JOIN_COSINE, core.TASTE_MAX_CENTROIDS)
    stored = l2.encode_centroids([{"vector": basis(1), "weight": 0.4444, "count": 2, "evidence": 1.6}])
    assert stored == core.stored_centroids([basis(1), basis(1)])
    assert (l2.TASTE_EVIDENCE_K, l2.TASTE_TITLE_E, l2.CENTROID_RATE) == \
        (core.TASTE_EVIDENCE_K, core.TASTE_TITLE_E, core.CENTROID_RATE)
    assert l2.decode_centroids(stored)[0]["count"] == 2 and l2.decode_centroids([{"v": "bad!"}]) == []


# --- processor ----------------------------------------------------------------

def _dyn(v):
    return json.loads(json.dumps(v, default=float), parse_float=Decimal)


class Interactions:
    """user-interactions: SESSION#behavior (put/get) and TITLE# (update/query)."""

    def __init__(self, titles=None):
        self.items = {("SESSION#behavior",): None}
        self.titles = dict(titles or {})

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get((Key["sk"],))
        return {"Item": _dyn(item)} if item else {}

    def put_item(self, Item, ConditionExpression, ExpressionAttributeValues=None):
        self.items[(Item["sk"],)] = json.loads(json.dumps(Item, default=float))

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        cur = self.titles.setdefault(Key["sk"], {})
        vals = ExpressionAttributeValues
        cur.update(last_watched=vals[":ts"], completion=float(vals[":c"]), content_type=vals[":t"])
        if ":yes" in vals:
            cur["completed"] = True
            cur["times_watched"] = cur.get("times_watched", 0) + 1
        else:
            cur.setdefault("completed", False)

    def query(self, **kw):
        return {"Items": [{"sk": k, "last_watched": v["last_watched"]} for k, v in self.titles.items() if v.get("completed")]}


class Profiles:
    def __init__(self, item=None, conflicts=0):
        self.item, self.conflicts, self.puts = item, conflicts, 0

    def get_item(self, Key, ConsistentRead=False):
        return {"Item": _dyn(self.item)} if self.item else {}

    def put_item(self, Item, ConditionExpression, ExpressionAttributeValues=None):
        from botocore.exceptions import ClientError
        if self.conflicts:
            self.conflicts -= 1
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        assert (ExpressionAttributeValues or {}).get(":old") == (self.item or {}).get("version")
        self.puts += 1
        self.item = json.loads(json.dumps(Item, default=float))


@pytest.fixture
def processor(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    mod = importlib.import_module("processor")
    # The fixture events are dated 2026-10-04; pin "now" to that evening so the
    # 30-day taste_contrib TTL (l2.prune_contributions) doesn't expire them.
    frozen = datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc).timestamp()
    clock = types.SimpleNamespace(**{n: getattr(time, n) for n in dir(time) if not n.startswith("_")})
    clock.time = lambda: frozen
    monkeypatch.setattr(mod, "time", clock)
    mod._catalog.clear()
    mod.prefetch_catalog(list(CATALOG), fetch=lambda ids: {i: CATALOG[i] for i in ids if i in CATALOG})
    monkeypatch.setattr(mod, "_fetch_embeddings", lambda ids: {i: VECTORS[i] for i in ids if i in VECTORS})
    return mod


VECTORS = {"tt1001113": basis(0), "tt1001163": [0.9, 0.1] + [0.0] * 6, "tt1001001": basis(4)}


def aff_item(**extra):
    return {"user_id": PID, "data_type": "AFFINITIES", "version": 1, "entities": json.loads(json.dumps(SEEDED)), **extra}


def test_short_play_writes_watch_state_and_leaves_affinities_alone(processor):
    inter, prof = Interactions(), Profiles(aff_item())
    assert processor.update_profile(PID, BULLET_TRAIN, table=inter, profiles=prof) == 3
    assert inter.titles["TITLE#tt1001129"] == {"last_watched": "2026-10-04T21:55:54Z", "completion": 0.006,
                                                "content_type": "movie", "completed": False}
    assert prof.puts == 0 and prof.item["version"] == 1


def test_accept_is_interest_not_taste(processor):
    seeded_titles = {"TITLE#tt1001163": {"completed": True, "last_watched": "2026-10-01T03:24:36Z"}}
    inter, prof = Interactions(seeded_titles), Profiles(aff_item(notes=["kept"]), conflicts=1)
    processor.update_profile(PID, TENET, table=inter, profiles=prof)
    aff = prof.item
    assert aff["version"] == 2 and aff["notes"] == ["kept"]
    assert aff["entities"]["genre:action"]["s"] == 0.7075 and aff["entities"]["genre:action"]["n"] == 10
    assert aff["entities"]["person:shark_bite"] == pytest.approx(
        {"s": 0.0075, "n": 0, "label": "Shark Bite", "last": aff["entities"]["person:shark_bite"]["last"]})
    assert "centroids" not in aff and not aff["taste_contrib"]
    assert inter.titles["TITLE#tt1001113"]["completed"] is False


def test_list_add_seeds_centroids_from_watched(processor):
    seeded_titles = {"TITLE#tt1001163": {"completed": True, "last_watched": "2026-10-01T03:24:36Z"},
                     "TITLE#tt1001001": {"completed": True, "last_watched": "2026-09-30T03:24:36Z"}}
    inter, prof = Interactions(seeded_titles), Profiles(aff_item())
    add = [ev("list_add", "22:02:00", {"item_id": "tt1001113"}, surface="title_detail")]
    processor.update_profile(PID, add, table=inter, profiles=prof)
    aff = prof.item
    assert aff["entities"]["genre:action"]["n"] == 11
    cents = l2.decode_centroids(aff["centroids"])
    # Seeded from the two completed titles (oldest first: Quantum Storm, then Nano Technology), then
    # Retaliation (basis 0) joined the Nano Technology-like centroid with e = 0.7.
    assert sorted(c["count"] for c in cents) == [1, 2]
    assert sorted(round(c["evidence"], 4) for c in cents) == [0.8, 1.5]
    assert aff["taste_contrib"]["tt1001113"]["e"] == 0.7


def _finish_tenet(start="20"):
    events = [ev("play_start", f"{start}:00:00", {"item_id": "tt1001113", "content_type": "movie", "duration_s": 7680})]
    events += [ev("progress", f"{start}:{m:02d}:30", {"item_id": "tt1001113", "watched_s": 60, "position_s": m * 60}, n=m)
               for m in range(1, 5)]
    events += [ev("play_end", f"{start}:05:00", {"item_id": "tt1001113", "content_type": "movie", "duration_s": 7680,
                                                 "position_s": 7680, "reason": "ended", "watched_s": 20})]
    return events


def test_watch_then_like_counts_one_title(processor):
    inter, prof = Interactions(), Profiles(aff_item())
    like = ev("feedback", "20:06:00", {"item_id": "tt1001113", "value": "like", "content_type": "movie"},
              surface="title_detail")
    processor.update_profile(PID, _finish_tenet() + [like], table=inter, profiles=prof)
    aff = prof.item
    assert aff["entities"]["genre:sci-fi"]["n"] == 11                       # one title, one piece of evidence
    assert aff["entities"]["genre:sci-fi"]["s"] == pytest.approx(0.85, abs=1e-4)   # 0.7 + 0.15 * 1.0
    [c] = l2.decode_centroids(aff["centroids"])
    assert c["count"] == 1 and c["evidence"] == pytest.approx(1.0) and c["weight"] == 0.3333
    assert aff["taste_contrib"]["tt1001113"]["p"] == {"v": 0.8, "f": 0.5}


def test_mark_watched_counts_as_finished_title(processor):
    inter, prof = Interactions(), Profiles(aff_item())
    mark = ev("mark_watched", "20:00:00", {"item_id": "tt1001113"}, surface="title_detail")
    processor.update_profile(PID, [mark], table=inter, profiles=prof)
    aff = prof.item
    assert inter.titles["TITLE#tt1001113"]["completed"] is True     # excluded from recommendations
    assert aff["entities"]["genre:sci-fi"] == pytest.approx(
        {"s": 0.82, "n": 11, "label": "Sci-Fi", "last": aff["entities"]["genre:sci-fi"]["last"]})
    [c] = l2.decode_centroids(aff["centroids"])
    assert c["count"] == 1 and c["evidence"] == pytest.approx(0.8)
    assert aff["taste_contrib"]["tt1001113"]["p"] == {"v": 0.8}
    assert not aff.get("contexts")                                  # a mark has no time of day


def test_watch_then_mark_counts_one_title(processor):
    inter, prof = Interactions(), Profiles(aff_item())
    mark = ev("mark_watched", "20:06:00", {"item_id": "tt1001113"}, surface="title_detail")
    processor.update_profile(PID, _finish_tenet() + [mark], table=inter, profiles=prof)
    aff = prof.item
    assert aff["entities"]["genre:sci-fi"]["n"] == 11 and aff["entities"]["genre:sci-fi"]["s"] == 0.82
    [c] = l2.decode_centroids(aff["centroids"])
    assert c["evidence"] == pytest.approx(0.8)
    assert aff["taste_contrib"]["tt1001113"]["p"] == {"v": 0.8}


def test_mark_watched_in_l1_adds_no_minutes():
    sigs, state = fold_signals([ev("mark_watched", "20:00:00", {"item_id": "tt1001113"}, surface="title_detail")])
    [entry] = state["recent"]
    assert entry["reason"] == "marked" and entry["completion"] == 1.0 and entry["watched_s"] == 0
    assert state["format_mix"] == {"short_min": 0, "long_min": 0} or not any(state["format_mix"].values())
    assert not state["entities"]
    assert [(k, i, e) for k, i, e, _v in sigs] == [("view", "tt1001113", 0.8)]


def test_finished_film_marks_title_completed(processor):
    inter, prof = Interactions(), Profiles(aff_item())
    events = [ev("play_start", "20:00:00", {"item_id": "tt1001113", "content_type": "movie", "duration_s": 7680}),
              ev("play_end", "20:05:00", {"item_id": "tt1001113", "content_type": "movie", "duration_s": 7680,
                                          "position_s": 7680, "reason": "ended", "watched_s": 20})]
    # One event credits at most 120 s of watching, so feed heartbeats for a real watch time
    # (the end is reached by seeking; completion comes from the position).
    beats = [ev("progress", f"{20 + m // 60:02d}:{m % 60:02d}:30", {"item_id": "tt1001113", "watched_s": 60,
                                                                        "position_s": m * 60}, n=m) for m in range(1, 5)]
    processor.update_profile(PID, events[:1] + beats + events[1:], table=inter, profiles=prof)
    assert inter.titles["TITLE#tt1001113"]["completed"] is True and inter.titles["TITLE#tt1001113"]["times_watched"] == 1
    assert prof.item["entities"]["genre:sci-fi"]["s"] == 0.82                     # 0.7 + 0.15 * 0.8


def test_booth_events_skip_long_term(processor):
    inter, prof = Interactions(), Profiles(aff_item())
    booth = [dict(e, booth=True) for e in TENET]
    assert processor.update_profile(PID, booth, table=inter, profiles=prof) == 3
    assert prof.puts == 0 and inter.titles == {}
    assert inter.items[("SESSION#behavior",)]["agent_accepts"] == ["tt1001113"]   # L1 still updates


def test_l2_failure_does_not_fail_the_batch(processor, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("profiles down")
    monkeypatch.setattr(processor, "update_long_term", boom)
    assert processor.update_profile(PID, TENET, table=Interactions(), profiles=Profiles()) == 3


# --- time-of-day contexts -----------------------------------------------------

contract = importlib.import_module("contract")


@pytest.mark.parametrize("tz,ok", [(-240, True), (0, True), (840, True), (-841, False), (90.5, False),
                                   (True, False), ("-240", False)])
def test_contract_accepts_an_optional_utc_offset(tz, ok):
    event = {k: v for k, v in TENET[0].items() if k not in {"server_ts", "booth"}}
    assert contract.validate(event) is None                     # still optional
    assert (contract.validate({**event, "tz_offset_min": tz}) is None) is ok


def with_tz(events, tz):
    return [dict(e, tz_offset_min=tz) for e in events]


def test_finished_view_carries_its_local_bucket():
    # 22:01 UTC on Sunday 2026-10-04 is 18:01 in New York (UTC-4) and 07:01 Monday in Tokyo (UTC+9).
    _, state = fold_signals(with_tz(TENET, -240))
    assert state["tz_offset_min"] == -240 and state["recent"][0]["bucket"] == "weekend|evening"
    _, state = fold_signals(with_tz(TENET, 540))
    assert state["recent"][0]["bucket"] == "weekday|daytime"
    _, state = fold_signals(TENET)
    assert state["recent"][0]["bucket"] is None


def test_context_update_counts_minutes_views_and_entities():
    view = {"bucket": "weekend|evening", "type": "movie", "watched_s": 600, "ts": 1000}
    ctx = l2.update_context({}, view, {"genre:sci-fi": "sci-fi"}, 0.8)
    assert ctx["weekend|evening"]["views"] == 1 and ctx["weekend|evening"]["long_min"] == 10.0
    assert ctx["weekend|evening"]["entities"]["genre:sci-fi"]["s"] == 0.12
    l2.update_context(ctx, {**view, "type": "microdrama", "watched_s": 60}, {"genre:sci-fi": "sci-fi"}, 0.4)
    assert ctx["weekend|evening"]["views"] == 2 and ctx["weekend|evening"]["short_min"] == 1.0
    assert ctx["weekend|evening"]["entities"]["genre:sci-fi"]["n"] == 2
    assert l2.update_context({}, {**view, "bucket": None}, {"genre:x": "x"}, 0.8) == {}


def test_processor_writes_bucket_scores_and_offset(processor):
    inter, prof = Interactions(), Profiles(aff_item())
    events = [ev("play_start", "22:00:00", {"item_id": "tt1001113", "content_type": "movie", "duration_s": 7680}, sid="s9")]
    events += [ev("progress", f"22:0{m}:00", {"item_id": "tt1001113", "watched_s": 60, "position_s": 60 * m}, sid="s9", n=m)
               for m in range(1, 4)]
    events += [ev("play_end", "22:04:00", {"item_id": "tt1001113", "content_type": "movie", "duration_s": 7680,
                                           "position_s": 7600, "reason": "ended", "watched_s": 60}, sid="s9")]
    processor.update_profile(PID, with_tz(events, -240), table=inter, profiles=prof)
    aff = prof.item
    assert aff["tz_offset_min"] == -240
    ctx = aff["contexts"]["weekend|evening"]           # Sunday 18:00 in New York
    assert ctx["views"] == 1 and ctx["long_min"] == 4.0
    assert ctx["entities"]["genre:sci-fi"]["s"] == 0.12      # bucket starts at 0, +0.15 * 0.8
    assert aff["entities"]["genre:sci-fi"]["s"] == 0.82      # overall taste unchanged in behaviour


# --- clips feed: feedback events and poster cards ------------------------------

def test_feedback_is_recorded_in_l1_and_scored_once():
    fb = {"item_id": "tt1001113", "value": "more", "content_type": "title_card", "parent_item_id": "tt1001113"}
    sigs, state = fold_signals([ev("feedback", "12:00:00", fb, surface="clips_feed")])
    assert state["feedback"][0] == {"item": "tt1001113", "value": "more", "type": "title_card",
                                    "parent": "tt1001113", "ts": state["feedback"][0]["ts"]}
    assert [(k, i, e) for k, i, e, _ in sigs] == [("feedback", "tt1001113", 0.7)]
    again, _ = fold_signals([ev("progress", "12:00:05", {"item_id": "tt1001113", "watched_s": 3})], state)
    assert again == []                                     # not re-scored on the next fold


def test_dislike_feedback_lowers_parent_entities_at_half_weight(processor):
    inter, prof = Interactions(), Profiles(aff_item())
    fb = {"item_id": "tt1001113", "value": "dislike", "content_type": "title_card", "parent_item_id": "tt1001113"}
    processor.update_profile(PID, [ev("feedback", "12:00:00", fb, surface="clips_feed")], table=inter, profiles=prof)
    assert prof.item["entities"]["genre:action"]["s"] == 0.67                     # 0.7 + 0.075 * -0.4
    assert "centroids" not in prof.item                                            # dislikes never move taste vectors


def test_poster_card_view_scores_taste_but_is_not_a_viewing(processor):
    inter, prof = Interactions(), Profiles(aff_item())
    card = {"item_id": "tt1001113", "content_type": "title_card", "parent_item_id": "tt1001113", "duration_s": 120}
    events = [ev("play_start", "12:00:00", card, surface="clips_feed"),
              ev("play_end", "12:01:00", {**card, "reason": "swipe", "watched_s": 60}, surface="clips_feed")]
    processor.update_profile(PID, events, table=inter, profiles=prof)
    assert inter.titles == {}                                                      # no TITLE#: not watched
    assert prof.item["entities"]["genre:action"]["s"] == 0.73                      # 0.7 + 0.075 * 0.4
