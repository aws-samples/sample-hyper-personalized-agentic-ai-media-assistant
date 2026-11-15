"""Household provisioning, GET /household and GET /home, plus their CDK wiring."""
import importlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SEED = json.loads((ROOT / "infra/deployment/seed/default_household.json").read_text())
# The Lambda and layer directories are CDK assets: bytecode written there would
# change their hashes and force needless layer/function replacements.
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "infra/lambdas/household"))
sys.path.insert(0, str(ROOT / "infra/layers/shared/python"))

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def core():
    return importlib.import_module("household_core")


@pytest.fixture
def fn(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    return importlib.import_module("lambda_function")


def ids(start=0):
    counter = iter(range(start, start + 1000))
    return lambda: "p-" + f"{next(counter):032x}"


# --- seed + provisioning ---------------------------------------------------

def test_committed_seed_is_valid_and_fits_one_transaction(core):
    items = core.build_household("sub-1", SEED, NOW, ids())
    assert sum(len(v) for v in items.values()) <= core.MAX_TRANSACT_ITEMS
    assert [m["display_name"] for m in SEED["members"]] == ["Action Fan", "Rom-Com Lover", "Sports Fan"]


def test_each_member_gets_its_own_profile_id_and_rows(core):
    items = core.build_household("sub-1", SEED, NOW, ids())
    members = [i for i in items["households"] if i["sk"].startswith(core.MEMBER_PREFIX)]
    pids = [m["profile_id"] for m in members]
    assert len(set(pids)) == len(SEED["members"])
    assert all(core.is_profile_id(p) for p in pids)
    for pid, seeded in zip(pids, SEED["members"]):
        rows = [i for i in items["cache"] if i["profile_id"] == pid]
        assert {"sk": core.HERO_SK, "title_id": seeded["hero"]}.items() <= next(r for r in rows if r["sk"] == core.HERO_SK).items()
        rails = sorted((r for r in rows if r["sk"].startswith(core.ROWS_PREFIX)), key=lambda r: r["rank"])
        assert [r["sk"][len(core.ROWS_PREFIX):] for r in rails] == [r["rail_key"] for r in seeded["rails"]]
        assert all(r["source"] == "seed" for r in rows)
    # No archetype / persona keys anywhere in what readers see.
    assert "ARCHETYPE" not in json.dumps(items, default=str)


def test_marker_and_profile_items(core):
    items = core.build_household("sub-1", SEED, NOW, ids())
    assert items["households"][0] == {"account_id": "sub-1", "sk": "HOUSEHOLD",
                                      "created_at": "2026-10-02T12:00:00Z", "seed_version": 2}
    action = items["households"][1]["profile_id"]
    by_type = {i["data_type"]: i for i in items["profiles"] if i["user_id"] == action}
    assert set(by_type) == {"L3#DECLARED", "AFFINITIES", "watch_list"}
    assert by_type["L3#DECLARED"]["updated_by"] == "seed"
    assert by_type["AFFINITIES"]["entities"]["genre:sci-fi"]["s"] == Decimal("0.7")
    romcom = items["households"][2]["profile_id"]
    declared = next(i for i in items["profiles"] if i["user_id"] == romcom and i["data_type"] == "L3#DECLARED")
    assert declared["avoid_themes"] == ["excessive gore"] and declared["excluded_genres"] == []


def test_watched_titles_are_completed_and_ordered_newest_first(core):
    items = core.build_household("sub-1", SEED, NOW, ids())
    action = items["households"][1]["profile_id"]
    titles = [i for i in items["interactions"] if i["profile_id"] == action and i["sk"].startswith("TITLE#")]
    assert [t["sk"][6:] for t in titles] == [w["title_id"] for w in SEED["members"][0]["watched"]]
    assert all(t["completed"] and t["completion"] == Decimal("1.0") for t in titles)
    stamps = [t["last_watched"] for t in titles]
    assert stamps == sorted(stamps, reverse=True) and stamps[0] == "2026-10-01T12:00:00Z"
    events = [i for i in items["interactions"] if i["profile_id"] == action and i["sk"].startswith("EVT#")]
    assert len(events) == len(titles)
    assert all(e["event_type"] == "view" and e["expires_at"] > NOW.timestamp() for e in events)
    assert all(e["sk"].startswith(f"EVT#{e_ts}#") for e, e_ts in zip(events, stamps))


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(members=[]),
    lambda s: s["members"][1].update(member_key=s["members"][0]["member_key"]),
    lambda s: s["members"][0].pop("hero"),
    lambda s: s["members"][0]["rails"][0].update(items=[{"blurb": "x"}]),
    lambda s: s["members"][0]["rails"][1].update(rail_key=s["members"][0]["rails"][0]["rail_key"]),
])
def test_bad_seed_is_rejected(core, mutate):
    seed = json.loads(json.dumps(SEED))
    mutate(seed)
    with pytest.raises(core.SeedError):
        core.build_household("sub-1", seed, NOW, ids())


# --- handler ---------------------------------------------------------------

class FakeStore:
    def __init__(self, fn, race=False):
        self.fn, self.race = fn, race
        self.tables = {"households": [], "profiles": [], "cache": [], "interactions": []}
        self.provision_calls = 0

    def members(self, account_id):
        return [i for i in self.tables["households"] if i["account_id"] == account_id and i["sk"].startswith("MEMBER#")]

    def member(self, account_id, profile_id):
        return next((m for m in self.members(account_id) if m["profile_id"] == profile_id), None)

    def rows(self, profile_id):
        return [i for i in self.tables["cache"] if i["profile_id"] == profile_id]

    def seed(self):
        return SEED

    def provision(self, items):
        self.provision_calls += 1
        if any(i["sk"] == "HOUSEHOLD" and i["account_id"] == items["households"][0]["account_id"]
               for i in self.tables["households"]):
            raise self.fn.AlreadyProvisioned()
        for key, values in items.items():
            self.tables[key].extend(values)
        if self.race:
            raise self.fn.AlreadyProvisioned()


def event(resource, sub="sub-1", **params):
    e = {"resource": resource, "queryStringParameters": params or None}
    if sub:
        e["requestContext"] = {"authorizer": {"claims": {"sub": sub}}}
    return e


def call(fn, store, monkeypatch, e, movies=None, embeddings=None):
    monkeypatch.setattr(fn, "_store", store)
    monkeypatch.setattr(fn, "hydrate", lambda tids: {t: {"id": t, "title": t} for t in tids if movies is None or t in movies})
    monkeypatch.setattr(fn, "fetch_embeddings", embeddings or (lambda tids: {}))
    resp = fn.lambda_handler(e, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_first_call_creates_household_once(fn, monkeypatch):
    store = FakeStore(fn)
    status, body = call(fn, store, monkeypatch, event("/household"))
    assert status == 200
    assert [m["displayName"] for m in body["members"]] == ["Action Fan", "Rom-Com Lover", "Sports Fan"]
    assert body["members"][0]["avatar"] == {"hue": 10, "icon": "🎬"}
    status, again = call(fn, store, monkeypatch, event("/household"))
    assert again == body and store.provision_calls == 1


def test_concurrent_first_call_returns_the_winner(fn, monkeypatch):
    store = FakeStore(fn, race=True)
    status, body = call(fn, store, monkeypatch, event("/household"))
    assert status == 200 and len(body["members"]) == 3


def test_households_are_separate_per_account(fn, monkeypatch):
    store = FakeStore(fn)
    _, a = call(fn, store, monkeypatch, event("/household", sub="sub-a"))
    _, b = call(fn, store, monkeypatch, event("/household", sub="sub-b"))
    assert not {m["profileId"] for m in a["members"]} & {m["profileId"] for m in b["members"]}


def test_home_returns_hero_and_rails_in_seed_order(fn, monkeypatch):
    store = FakeStore(fn)
    _, household = call(fn, store, monkeypatch, event("/household"))
    pid = household["members"][0]["profileId"]
    status, home = call(fn, store, monkeypatch, event("/home", profile_id=pid))
    assert status == 200 and home["profileId"] == pid
    assert home["hero"]["id"] == SEED["members"][0]["hero"]
    assert [r["displayTitle"] for r in home["rails"]] == [r["display_title"] for r in SEED["members"][0]["rails"]]
    byw = next(r for r in home["rails"] if r["key"] == "because_you_watched")
    assert all(m.get("blurb") for m in byw["movies"])


def test_home_drops_titles_missing_from_catalog(fn, monkeypatch):
    store = FakeStore(fn)
    _, household = call(fn, store, monkeypatch, event("/household"))
    pid = household["members"][0]["profileId"]
    keep = {SEED["members"][0]["rails"][0]["items"][0]["title_id"]}
    _, home = call(fn, store, monkeypatch, event("/home", profile_id=pid), movies=keep)
    assert home["hero"] is None
    assert [len(r["movies"]) for r in home["rails"]] == [1]


def test_home_refuses_another_accounts_member(fn, monkeypatch):
    store = FakeStore(fn)
    _, other = call(fn, store, monkeypatch, event("/household", sub="sub-other"))
    pid = other["members"][0]["profileId"]
    status, _ = call(fn, store, monkeypatch, event("/home", sub="sub-1", profile_id=pid))
    assert status == 404


@pytest.mark.parametrize("params", [{}, {"profile_id": "demo-user-action_fan"}, {"profile_id": "p-" + "0" * 32}])
def test_home_rejects_missing_or_unknown_profile(fn, monkeypatch, params):
    status, _ = call(fn, FakeStore(fn), monkeypatch, event("/home", **params))
    assert status == 404


def test_requires_cognito_identity(fn, monkeypatch):
    status, _ = call(fn, FakeStore(fn), monkeypatch, event("/household", sub=None))
    assert status == 401


# --- add member (POST /household/members) ----------------------------------

class MemberStore(FakeStore):
    """FakeStore plus the add-member path, with the marker's member_seq lock."""

    def marker(self, account_id):
        return next((i for i in self.tables["households"]
                     if i["account_id"] == account_id and i["sk"] == "HOUSEHOLD"), None)

    def add_member(self, account_id, seen_seq, items):
        marker = MemberStore.marker(self, account_id)
        if int(marker.get("member_seq") or 0) != seen_seq:
            raise self.fn.core.MemberError(409, "the household changed; try again")
        marker["member_seq"] = seen_seq + 1
        self.tables["households"].append(items["member"])
        self.tables["profiles"].extend(items["profiles"])


def post(fn, store, monkeypatch, body, sub="sub-1", groups=None):
    e = {"resource": "/household/members", "httpMethod": "POST",
         "body": body if isinstance(body, str) else json.dumps(body)}
    if sub:
        claims = {"sub": sub, **({"cognito:groups": groups} if groups else {})}
        e["requestContext"] = {"authorizer": {"claims": claims}}
    return call(fn, store, monkeypatch, e)


def household_with_seed(fn, monkeypatch):
    store = MemberStore(fn)
    call(fn, store, monkeypatch, event("/household"))
    return store


def test_add_member_creates_an_empty_member(fn, monkeypatch):
    store = household_with_seed(fn, monkeypatch)
    status, body = post(fn, store, monkeypatch, {"display_name": "  Sam  "})
    assert status == 201 and body["displayName"] == "Sam" and body["memberKey"] is None
    assert body["rank"] == 4 and not body["avatar"].get("kids")
    pid = body["profileId"]
    # No L3 item, so the Settings API serves defaults at version 0 for onboarding.
    assert [i["data_type"] for i in store.tables["profiles"] if i["user_id"] == pid] == ["watch_list"]
    assert not [i for i in store.tables["cache"] if i["profile_id"] == pid]
    _, household = call(fn, store, monkeypatch, event("/household"))
    assert [m["displayName"] for m in household["members"]] == ["Action Fan", "Rom-Com Lover", "Sports Fan", "Sam"]


def test_kids_member_gets_the_kids_limit_in_l3(fn, monkeypatch):
    store = household_with_seed(fn, monkeypatch)
    status, body = post(fn, store, monkeypatch, {"display_name": "Mia", "kids_profile": True})
    assert status == 201 and body["avatar"]["kids"] is True
    [l3] = [i for i in store.tables["profiles"] if i["user_id"] == body["profileId"] and i["data_type"] == "L3#DECLARED"]
    assert l3["kids_profile"] is True and l3["version"] == 1 and l3["updated_by"] == "household"


@pytest.mark.parametrize("body", [{}, {"display_name": ""}, {"display_name": "x" * 21},
                                  {"display_name": "Sam\u0000"}, {"display_name": "Sam", "kids_profile": "yes"},
                                  "not json", [1]])
def test_add_member_rejects_bad_input(fn, monkeypatch, body):
    store = household_with_seed(fn, monkeypatch)
    assert post(fn, store, monkeypatch, body)[0] == 400


def test_add_member_rejects_duplicate_name_any_case(fn, monkeypatch):
    store = household_with_seed(fn, monkeypatch)
    status, body = post(fn, store, monkeypatch, {"display_name": "action fan"})
    assert status == 409 and "name" in body["error"]


def test_add_member_enforces_household_limit(fn, monkeypatch):
    store = household_with_seed(fn, monkeypatch)
    for name in ["A", "B"]:
        assert post(fn, store, monkeypatch, {"display_name": name})[0] == 201
    assert post(fn, store, monkeypatch, {"display_name": "D"})[0] == 409


def test_concurrent_add_loses_the_lock(fn, monkeypatch):
    store = household_with_seed(fn, monkeypatch)
    real = store.marker
    # The marker read is stale: another add bumped member_seq in between.
    monkeypatch.setattr(store, "marker", lambda a: {**real(a), "member_seq": 7})
    assert post(fn, store, monkeypatch, {"display_name": "Sam"})[0] == 409


def test_add_member_needs_a_household_and_no_booth(fn, monkeypatch):
    assert post(fn, MemberStore(fn), monkeypatch, {"display_name": "Sam"})[0] == 404
    store = household_with_seed(fn, monkeypatch)
    assert post(fn, store, monkeypatch, {"display_name": "Sam"}, groups="booth")[0] == 403
    assert post(fn, store, monkeypatch, {"display_name": "Sam"}, sub=None)[0] == 401


# --- My List membership check (shared layer) -------------------------------

class FakeHouseholds:
    def __init__(self, members):
        self.members = members

    def Table(self, name):
        return self

    def get_item(self, Key):
        ok = (Key["account_id"], Key["sk"]) in self.members
        return {"Item": {"account_id": Key["account_id"]}} if ok else {}


@pytest.fixture
def common(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("HOUSEHOLDS_TABLE", "households")
    mod = importlib.import_module("ibc_common")
    pid = "p-" + "a" * 32
    monkeypatch.setattr(mod._session, "resource", lambda *_: FakeHouseholds({("sub-1", "MEMBER#" + pid)}))
    return mod, pid


def test_list_pk_accepts_own_member_only(common):
    mod, pid = common
    assert mod.resolve_profile_pk(event("/get_list", sub="sub-1"), "demo-user", pid) == pid
    assert mod.resolve_profile_pk(event("/get_list", sub="sub-2"), "demo-user", pid) is None
    assert mod.resolve_profile_pk(event("/get_list", sub=None), "demo-user", pid) is None


def test_list_pk_keeps_legacy_slug(common):
    mod, _ = common
    assert mod.resolve_profile_pk(event("/get_list"), "demo-user", "sports_fan") == "demo-user-sports_fan"
    assert mod.resolve_profile_pk(event("/get_list"), None, "sports_fan") is None


# --- synthesized wiring ----------------------------------------------------

def application_template():
    from config import load_config, stack_name
    cfg = load_config()
    path = Path(cfg["artifactDir"]) / "cdk.out" / (stack_name(cfg, "Application") + ".template.json")
    if not path.exists():
        pytest.skip("Synthesize infra/deployment/app.py first")
    return cfg, json.loads(path.read_text())


def test_new_tables_keys_and_ttl():
    cfg, app = application_template()
    tables = {r["Properties"]["TableName"]: r["Properties"] for r in app["Resources"].values() if r["Type"] == "AWS::DynamoDB::Table"}
    expected = {"households": ("account_id", "sk", None), "recommendation-cache": ("profile_id", "sk", "expires_at"),
                "user-interactions": ("profile_id", "sk", "expires_at")}
    for name, (pk, sk, ttl) in expected.items():
        props = tables[f'{cfg["prefix"]}-{name}']
        assert [k["AttributeName"] for k in props["KeySchema"]] == [pk, sk]
        assert props.get("TimeToLiveSpecification", {}).get("AttributeName") == ttl
        assert "LocalSecondaryIndexes" not in props and "GlobalSecondaryIndexes" not in props


def test_household_routes_require_cognito():
    _, app = application_template()
    resources = {k: v["Properties"]["PathPart"] for k, v in app["Resources"].items() if v["Type"] == "AWS::ApiGateway::Resource"}
    methods = [v["Properties"] for v in app["Resources"].values() if v["Type"] == "AWS::ApiGateway::Method"
               and v["Properties"]["HttpMethod"] != "OPTIONS"  # CORS preflight is unauthenticated by design
               and resources.get(v["Properties"].get("ResourceId", {}).get("Ref")) in {"household", "home"}]
    assert len(methods) == 2
    assert all(m["HttpMethod"] == "GET" and m["AuthorizationType"] == "COGNITO_USER_POOLS" for m in methods)


def test_add_member_route_is_post_with_cognito():
    _, app = application_template()
    res = {k: v["Properties"] for k, v in app["Resources"].items() if v["Type"] == "AWS::ApiGateway::Resource"}
    [members] = [k for k, p in res.items() if p["PathPart"] == "members"
                 and res.get(p["ParentId"].get("Ref"), {}).get("PathPart") == "household"]
    methods = sorted((v["Properties"]["HttpMethod"], v["Properties"]["AuthorizationType"])
                     for v in app["Resources"].values() if v["Type"] == "AWS::ApiGateway::Method"
                     and v["Properties"].get("ResourceId", {}).get("Ref") == members
                     and v["Properties"]["HttpMethod"] != "OPTIONS")
    assert methods == [("POST", "COGNITO_USER_POOLS")]


# --- seed upgrade (existing households get new seed members) ---------------

class UpgradeStore(MemberStore):
    """MemberStore plus upgrade_seed, with both optimistic locks."""

    def upgrade_seed(self, account_id, seen_seq, seen_version, version, items_by_table):
        marker = MemberStore.marker(self, account_id)
        if int(marker.get("member_seq") or 0) != seen_seq or marker.get("seed_version") != seen_version:
            return False
        marker["member_seq"], marker["seed_version"] = seen_seq + 1, version
        for key, values in items_by_table.items():
            self.tables[key].extend(values)
        return True


def v1_household(fn, monkeypatch):
    """A household created from seed v1 (no Sports Fan), plus one added member."""
    store = UpgradeStore(fn)
    v1 = {**SEED, "version": 1, "members": [m for m in SEED["members"] if m["member_key"] != "sports-fan"]}
    monkeypatch.setattr(store, "seed", lambda: v1)
    call(fn, store, monkeypatch, event("/household"))
    assert post(fn, store, monkeypatch, {"display_name": "Test"})[0] == 201
    monkeypatch.setattr(store, "seed", lambda: SEED)
    return store


def test_sports_fan_seed_follows_resolved_entities():
    sports = next(m for m in SEED["members"] if m["member_key"] == "sports-fan")
    followed = sports["declared"]["followed"]
    # The synthetic match's home side (SYN-MAT-0001); F1 is not part of the demo.
    assert followed["teams"] == [{"name": "Harbor City FC", "id": "team:harbor-city-fc"}]
    assert followed["drivers"] == []
    registry = json.loads((ROOT / "infra/lambdas/profile_settings/sports_entities.json").read_text())
    known = {e["id"] for e in registry["entities"]}
    assert {e["id"] for k in ("teams", "players", "drivers") for e in followed[k]} <= known


def test_existing_household_gets_missing_seed_members_once(fn, monkeypatch):
    store = v1_household(fn, monkeypatch)
    _, body = call(fn, store, monkeypatch, event("/household"))
    assert [m["displayName"] for m in body["members"]] == ["Action Fan", "Rom-Com Lover", "Test", "Sports Fan"]
    sports = next(m for m in body["members"] if m["memberKey"] == "sports-fan")
    assert sports["rank"] == 4
    pid = sports["profileId"]
    assert {i["data_type"] for i in store.tables["profiles"] if i["user_id"] == pid} == {"L3#DECLARED", "AFFINITIES", "watch_list"}
    assert any(i["sk"] == "HERO#home" for i in store.tables["cache"] if i["profile_id"] == pid)
    assert store.marker("sub-1")["seed_version"] == 2
    _, again = call(fn, store, monkeypatch, event("/household"))
    assert again == body


def test_seed_upgrade_respects_the_member_limit(fn, monkeypatch):
    store = v1_household(fn, monkeypatch)
    for name in ["A", "B"]:
        assert post(fn, store, monkeypatch, {"display_name": name})[0] == 201
    _, body = call(fn, store, monkeypatch, event("/household"))
    assert len(body["members"]) == 5 and "Sports Fan" not in [m["displayName"] for m in body["members"]]
    assert store.marker("sub-1")["seed_version"] == 2


def test_seed_upgrade_lost_race_adds_nothing(fn, monkeypatch):
    store = v1_household(fn, monkeypatch)
    real = store.marker
    monkeypatch.setattr(store, "marker", lambda a: {**real(a), "member_seq": 9})
    _, body = call(fn, store, monkeypatch, event("/household"))
    assert "Sports Fan" not in [m["displayName"] for m in body["members"]]


def test_seed_upgrade_skips_members_already_present(core):
    marker = {"seed_version": 1}
    members = [{"seed_member_key": "action-fan"}, {"seed_member_key": "rom-com-lover"}, {"seed_member_key": "sports-fan"}]
    assert core.seed_members_to_add(SEED, marker, members) == []
    assert core.seed_members_to_add(SEED, {"seed_version": 2}, members[:2]) == []
