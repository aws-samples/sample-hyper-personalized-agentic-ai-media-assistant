"""Cold-start Home rows (infra/lambdas/household/cold_start.py) and GET /home using them."""
import importlib
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True  # Lambda/layer dirs are CDK assets
sys.path.insert(0, str(ROOT / "infra/lambdas/household"))
sys.path.insert(0, str(ROOT / "infra/layers/shared/python"))

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
PID = "p-" + "a" * 32
SEEDED = "p-" + "b" * 32


@pytest.fixture
def cs():
    return importlib.import_module("cold_start")


@pytest.fixture
def fn(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    mod = importlib.import_module("lambda_function")
    mod._search_cache.clear()
    return mod


def ent(s, n=3, days_ago=0, label=None):
    e = {"s": Decimal(str(s)), "n": Decimal(n), "last": (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return {**e, "label": label} if label else e


# --- pure logic ------------------------------------------------------------

def test_liked_genres_strongest_first_after_fading(cs):
    aff = {"entities": {
        "genre:sci-fi": ent(0.5, label="Sci-Fi"),
        "genre:drama": ent(0.7, days_ago=60),          # 0.7 faded two half-lives -> 0.175
        "genre:documentary": ent(0.4, label="Documentary"),
        "genre:horror": ent(0.9),                       # never-show genre: dropped
        "genre:war": ent(0.9, n=2),                     # too little evidence
        "genre:romance": ent(-0.3),                     # disliked
        "person:nolan": ent(0.9)}}
    assert cs.liked_genres(aff, ["Horror"], NOW) == [("sci-fi", "Sci-Fi"), ("documentary", "Documentary"),
                                                     ("drama", "Drama")]


def test_liked_genres_caps_rails(cs):
    aff = {"entities": {f"genre:g{i}": ent(0.5 + i / 100) for i in range(6)}}
    assert len(cs.liked_genres(aff, [], NOW)) == cs.MAX_GENRE_RAILS


def test_limits_apply_l3(cs):
    base = cs.limits({})
    assert {"exists": {"field": "awards"}} in base and len(base) == 3
    never = cs.limits({"excluded_genres": ["horror"]})
    assert {"bool": {"must_not": [{"match_phrase": {"genres": "horror"}}]}} in never
    kids = cs.limits({"kids_profile": True})
    assert {"terms": {"mpaa_ratings": cs.KIDS_RATINGS}} in kids and len(kids) == 4


def test_basis_changes_only_with_inputs(cs):
    aff = {"entities": {"genre:sci-fi": ent(0.5)}}
    b1, specs = cs.plan({}, aff, NOW)
    assert [s["key"] for s in specs] == ["cs_genre_sci-fi", "cs_top_movies", "cs_top_series"]
    assert specs[0]["title"] == "Because You Like Sci-Fi"
    assert cs.plan({}, aff, NOW + timedelta(days=1))[0] == b1       # fading alone keeps the rails
    assert cs.plan({"excluded_genres": ["horror"]}, aff, NOW)[0] != b1
    assert cs.plan({}, {}, NOW)[0] != b1


def fake_search(pools, calls=None):
    """Hits per rail, chosen by the query's title_type / genre filters."""
    def search(body):
        if calls is not None:
            calls.append(body)
        flt = body["query"]["bool"]["filter"]
        genre = next((g for g in pools if {"match_phrase": {"genres": g}} in flt), None)
        series = {"terms": {"title_type": ["tvSeries", "tvMiniSeries"]}} in flt
        key = genre or ("series" if series else "movies")
        return {"hits": {"hits": [{"_source": {"title_id": t}} for t in pools.get(key, [])]}}
    return search


def test_build_rows_dedupes_across_rails_and_picks_a_hero(cs):
    basis, specs = cs.plan({}, {"entities": {"genre:sci-fi": ent(0.5)}}, NOW)
    pools = {"sci-fi": ["t1", "t2"], "movies": ["t1", "t3", "t4"], "series": ["s1"]}
    rows = cs.build_rows(PID, basis, specs, fake_search(pools), NOW)
    hero = rows[0]
    assert hero["sk"] == cs.HERO_SK and hero["title_id"] == "t1" and hero["basis"] == basis
    rails = {r["sk"][len(cs.ROWS_PREFIX):]: [i["title_id"] for i in r["items"]] for r in rows[1:]}
    assert rails == {"cs_genre_sci-fi": ["t1", "t2"], "cs_top_movies": ["t3", "t4"], "cs_top_series": ["s1"]}
    assert [r["rank"] for r in rows[1:]] == [1, 2, 3]
    assert all(r["source"] == "cold_start" and r["expires_at"] == int((NOW + cs.TTL).timestamp()) for r in rows)
    assert cs.is_fresh(rows, basis, NOW) and not cs.is_fresh(rows, "other", NOW)
    assert not cs.is_fresh(rows, basis, NOW + cs.TTL + timedelta(seconds=1))


def test_build_rows_skips_empty_rails(cs):
    basis, specs = cs.plan({}, {}, NOW)
    assert cs.build_rows(PID, basis, specs, fake_search({}), NOW) == []
    rows = cs.build_rows(PID, basis, specs, fake_search({"series": ["s1"]}), NOW)
    assert [r["sk"] for r in rows] == [cs.HERO_SK, cs.ROWS_PREFIX + "cs_top_series"] and rows[1]["rank"] == 1


# --- GET /home ---------------------------------------------------------------

class Store:
    def __init__(self):
        self.cache, self.profiles, self.taste_reads = [], {}, 0

    def member(self, account_id, profile_id):
        return {"profile_id": profile_id} if account_id == "sub-1" else None

    def rows(self, profile_id):
        return [dict(r) for r in self.cache if r["profile_id"] == profile_id]

    def taste(self, profile_id):
        self.taste_reads += 1
        p = self.profiles.get(profile_id, {})
        return p.get("L3#DECLARED", {}), p.get("AFFINITIES", {})

    def write_rows(self, rows, stale_sks):
        pid = rows[0]["profile_id"]
        keep = {r["sk"] for r in rows}
        self.cache = [r for r in self.cache if r["profile_id"] != pid or (r["sk"] not in keep and r["sk"] not in stale_sks)]
        self.cache += rows


def home(fn, store, monkeypatch, pools, calls=None, pid=PID, fail=False):
    def search(body):
        if fail:
            raise RuntimeError("catalog down")
        return fake_search(pools, calls)(body)
    monkeypatch.setattr(fn, "_store", store)
    monkeypatch.setattr(fn, "catalog_search", search)
    monkeypatch.setattr(fn, "hydrate", lambda tids: {t: {"id": t, "title": t} for t in tids})
    resp = fn.lambda_handler({"resource": "/home", "queryStringParameters": {"profile_id": pid},
                              "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)
    return resp["statusCode"], json.loads(resp["body"])


POOLS = {"sci-fi": ["t1", "t2"], "documentary": ["d1"], "movies": ["t3"], "series": ["s1"]}


def test_new_member_gets_rows_from_liked_genres(fn, monkeypatch):
    store = Store()
    store.profiles[PID] = {"AFFINITIES": {"entities": {"genre:sci-fi": ent(0.5, label="Sci-Fi"),
                                                       "genre:documentary": ent(0.5, label="Documentary")}}}
    status, body = home(fn, store, monkeypatch, POOLS)
    assert status == 200 and body["hero"]["id"] in {"t1", "d1"}
    assert [r["displayTitle"] for r in body["rails"]][-2:] == ["Top Rated Movies", "Top Rated Series"]
    assert {r["displayTitle"] for r in body["rails"][:2]} == {"Because You Like Sci-Fi", "Because You Like Documentary"}
    assert len(store.cache) == 5 and all(r["source"] == "cold_start" for r in store.cache)


def test_rows_are_reused_until_inputs_change(fn, monkeypatch):
    store, calls = Store(), []
    store.profiles[PID] = {"AFFINITIES": {"entities": {"genre:sci-fi": ent(0.5)}}}
    home(fn, store, monkeypatch, POOLS, calls)
    first = len(calls)
    fn._search_cache.clear()
    home(fn, store, monkeypatch, POOLS, calls)
    assert len(calls) == first                      # stored rows were fresh: no catalog queries
    # The member now never wants sci-fi: rebuilt, and the dropped rail is deleted.
    store.profiles[PID]["L3#DECLARED"] = {"excluded_genres": ["sci-fi"]}
    _, body = home(fn, store, monkeypatch, POOLS, calls)
    assert len(calls) > first
    assert [r["key"] for r in body["rails"]] == ["cs_top_movies", "cs_top_series"]
    assert not [r for r in store.cache if r["sk"].endswith("cs_genre_sci-fi")]


def test_member_with_no_taste_still_gets_top_rated(fn, monkeypatch):
    _, body = home(fn, Store(), monkeypatch, POOLS)
    assert [r["displayTitle"] for r in body["rails"]] == ["Top Rated Movies", "Top Rated Series"]
    assert body["hero"]["id"] == "t3"


def test_seeded_rows_win_and_skip_cold_start(fn, monkeypatch):
    store, calls = Store(), []
    store.cache = [{"profile_id": SEEDED, "sk": "HERO#home", "title_id": "h", "source": "seed"},
                   {"profile_id": SEEDED, "sk": "ROWS#home#top_picks", "rank": 1, "display_title": "Top Picks",
                    "items": [{"title_id": "x"}], "source": "seed"},
                   {"profile_id": SEEDED, "sk": "ROWS#home#cs_top_movies", "rank": 1, "display_title": "Top Rated Movies",
                    "items": [{"title_id": "y"}], "source": "cold_start"}]
    _, body = home(fn, store, monkeypatch, POOLS, calls, pid=SEEDED)
    assert body["hero"]["id"] == "h" and [r["key"] for r in body["rails"]] == ["top_picks"]
    assert not calls and store.taste_reads == 0


def test_catalog_failure_serves_an_empty_page(fn, monkeypatch):
    status, body = home(fn, Store(), monkeypatch, POOLS, fail=True)
    assert status == 200 and body["hero"] is None and body["rails"] == []


# --- HOME_RAILS_SOURCE: Personalize (P13N#) rows beside seed rows ------------

def seeded_with_p13n():
    store = Store()
    store.cache = [{"profile_id": SEEDED, "sk": "HERO#home", "title_id": "h", "source": "seed"},
                   {"profile_id": SEEDED, "sk": "ROWS#home#top_picks", "rank": 1, "display_title": "Top Picks",
                    "items": [{"title_id": "x"}], "source": "seed"},
                   {"profile_id": SEEDED, "sk": "P13N#HERO#home", "title_id": "ph", "source": "personalize"},
                   {"profile_id": SEEDED, "sk": "P13N#ROWS#home#top_picks", "rank": 1, "display_title": "Top Picks for You",
                    "items": [{"title_id": "px"}], "source": "personalize"}]
    return store


@pytest.mark.parametrize("source", [None, "seed", "bogus"])
def test_seed_mode_ignores_personalize_rows(fn, monkeypatch, source):
    if source:
        monkeypatch.setenv("HOME_RAILS_SOURCE", source)
    else:
        monkeypatch.delenv("HOME_RAILS_SOURCE", raising=False)
    _, body = home(fn, seeded_with_p13n(), monkeypatch, POOLS, pid=SEEDED)
    assert body["hero"]["id"] == "h" and [m["id"] for m in body["rails"][0]["movies"]] == ["x"]


def test_personalize_mode_serves_p13n_rows(fn, monkeypatch):
    monkeypatch.setenv("HOME_RAILS_SOURCE", "personalize")
    store = seeded_with_p13n()
    _, body = home(fn, store, monkeypatch, POOLS, pid=SEEDED)
    assert body["hero"]["id"] == "ph"
    assert [(r["key"], r["displayTitle"]) for r in body["rails"]] == [("top_picks", "Top Picks for You")]
    assert [m["id"] for m in body["rails"][0]["movies"]] == ["px"]
    assert len(store.cache) == 4                    # nothing rewritten


def test_personalize_mode_falls_back_without_p13n_rows(fn, monkeypatch):
    monkeypatch.setenv("HOME_RAILS_SOURCE", "personalize")
    _, body = home(fn, Store(), monkeypatch, POOLS)
    assert [r["displayTitle"] for r in body["rails"]] == ["Top Rated Movies", "Top Rated Series"]


def test_cold_start_never_deletes_p13n_rows(fn, monkeypatch):
    monkeypatch.delenv("HOME_RAILS_SOURCE", raising=False)
    store = Store()
    store.cache = [{"profile_id": PID, "sk": "P13N#HERO#home", "title_id": "ph", "source": "personalize"}]
    _, body = home(fn, store, monkeypatch, POOLS)    # seed mode: cold start builds and writes rows
    assert [r["displayTitle"] for r in body["rails"]] == ["Top Rated Movies", "Top Rated Series"]
    assert any(r["sk"] == "P13N#HERO#home" for r in store.cache)


# --- HOME_RAILS_SOURCE=connect: Connect Predictive Insights (CONN#) rows -------

def seeded_with_both():
    store = seeded_with_p13n()
    store.cache += [{"profile_id": SEEDED, "sk": "CONN#HERO#home", "title_id": "ch", "source": "connect"},
                    {"profile_id": SEEDED, "sk": "CONN#ROWS#home#top_picks", "rank": 1,
                     "display_title": "Top Picks for You", "items": [{"title_id": "cx"}], "source": "connect"}]
    return store


@pytest.mark.parametrize("source, hero, first", [("seed", "h", "x"), ("personalize", "ph", "px"),
                                                 ("connect", "ch", "cx")])
def test_each_mode_serves_only_its_own_rows(fn, monkeypatch, source, hero, first):
    monkeypatch.setenv("HOME_RAILS_SOURCE", source)
    store = seeded_with_both()
    _, body = home(fn, store, monkeypatch, POOLS, pid=SEEDED)
    assert body["hero"]["id"] == hero and [m["id"] for m in body["rails"][0]["movies"]] == [first]
    assert len(store.cache) == 6                    # nothing rewritten


def test_connect_mode_falls_back_to_seed_without_conn_rows(fn, monkeypatch):
    monkeypatch.setenv("HOME_RAILS_SOURCE", "connect")
    _, body = home(fn, seeded_with_p13n(), monkeypatch, POOLS, pid=SEEDED)
    assert body["hero"]["id"] == "h"                # P13N# rows are not a fallback for connect


def test_cold_start_never_deletes_conn_rows(fn, monkeypatch):
    monkeypatch.setenv("HOME_RAILS_SOURCE", "personalize")
    store = Store()
    store.cache = [{"profile_id": PID, "sk": "CONN#HERO#home", "title_id": "ch", "source": "connect"}]
    _, body = home(fn, store, monkeypatch, POOLS)
    assert [r["displayTitle"] for r in body["rails"]] == ["Top Rated Movies", "Top Rated Series"]
    assert any(r["sk"] == "CONN#HERO#home" for r in store.cache)


# --- wiring ---------------------------------------------------------------------

def test_household_fn_can_read_profiles():
    from config import load_config, stack_name
    cfg = load_config()
    path = Path(cfg["artifactDir"]) / "cdk.out" / (stack_name(cfg, "Application") + ".template.json")
    if not path.exists():
        pytest.skip("Synthesize infra/deployment/app.py first")
    res = json.loads(path.read_text())["Resources"]
    [table] = [k for k, v in res.items() if v["Type"] == "AWS::DynamoDB::Table"
               and v["Properties"]["TableName"] == f'{cfg["prefix"]}-profiles']
    role = res[next(k for k, v in res.items() if v["Type"] == "AWS::Lambda::Function"
                    and k.startswith("HouseholdFn"))]["Properties"]["Role"]["Fn::GetAtt"][0]
    stmts = [s for k, v in res.items() if v["Type"] == "AWS::IAM::Policy"
             and {"Ref": role} in v["Properties"]["Roles"] for s in v["Properties"]["PolicyDocument"]["Statement"]]
    reads = [s for s in stmts if "dynamodb:BatchGetItem" in s["Action"] and table in json.dumps(s["Resource"])]
    assert reads
