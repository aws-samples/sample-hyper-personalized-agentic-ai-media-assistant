"""Settings API (GET / PUT /profile/declared): validation, optimistic locking,
idempotent retries, membership and booth checks, the settings_changed notice,
and its CDK wiring."""
import copy
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Lambda and layer directories are CDK assets: no bytecode there.
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "infra/lambdas/profile_settings"))
sys.path.insert(0, str(ROOT / "infra/layers/shared/python"))

PID = "p-" + "b" * 32
OTHER = "p-" + "c" * 32
GENRES = frozenset({"drama", "comedy", "horror", "sci-fi", "sport"})


@pytest.fixture
def core():
    return importlib.import_module("settings_core")


@pytest.fixture
def fn(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    # Other Lambdas also name their module lambda_function: load this one by path.
    if "profile_settings_lambda" not in sys.modules:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "profile_settings_lambda", ROOT / "infra/lambdas/profile_settings/lambda_function.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["profile_settings_lambda"] = mod
    return sys.modules["profile_settings_lambda"]


def full(core, **over):
    d = core.defaults()
    d.update(over)
    return d


class FakeStore:
    def __init__(self, fn, item=None, members=(("sub-1", PID),), conflict_on_put=False, emit_fails=False):
        self.fn, self.item, self.members = fn, copy.deepcopy(item), set(members)
        self.conflict_on_put, self.emit_fails = conflict_on_put, emit_fails
        self.emitted, self.puts = [], 0

    def is_member(self, account_id, profile_id):
        return (account_id, profile_id) in self.members

    def get(self, profile_id):
        return copy.deepcopy(self.item)

    def put(self, item, expected_version):
        stored = (self.item or {}).get("version")
        if self.conflict_on_put or (stored or 0) != expected_version:
            raise self.fn.Conflict()
        self.item = copy.deepcopy(item)
        self.puts += 1

    def genre_vocab(self):
        return GENRES

    def emit_changed(self, account_id, profile_id, version, fields, source):
        if self.emit_fails:
            raise RuntimeError("stream down")
        self.emitted.append({"account_id": account_id, "profile_id": profile_id, "version": version,
                             "fields": fields, "updated_by": source})

    def emit_loved(self, account_id, profile_id, genres):
        self.loved = getattr(self, "loved", []) + [list(genres)]


def call(fn, store, monkeypatch, method="GET", body=None, sub="sub-1", pid=PID, groups=None):
    monkeypatch.setattr(fn, "_store", store)
    claims = {"sub": sub} if sub else {}
    if groups:
        claims["cognito:groups"] = groups
    ev = {"httpMethod": method, "resource": "/profile/declared", "queryStringParameters": {"profile_id": pid},
          "requestContext": {"authorizer": {"claims": claims}},
          "body": json.dumps(body) if isinstance(body, dict) else body}
    r = fn.lambda_handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


SEEDED = {"user_id": PID, "data_type": "L3#DECLARED", "excluded_genres": [], "avoid_themes": ["excessive gore"],
          "languages": ["en"], "subtitles": "off", "max_length_minutes": None,
          "followed": {"teams": [], "players": [], "drivers": [], "talent": []},
          "consent": {"personalization_from_history": True, "conversation_memory": True},
          "version": 1, "updated_at": "2026-10-03T00:00:00Z", "updated_by": "seed"}


# --- validation -------------------------------------------------------------

def test_valid_onboarding_payload_is_normalized(core):
    out = core.validate(full(core,
        excluded_genres=[" Horror ", "horror"], avoid_themes=["  animal   cruelty ", "Animal cruelty", "jump scares"],
        languages=["EN", "es"], max_length_minutes=150,
        followed={"teams": ["Harbor City FC"], "players": [{"name": "Kofi Asante Brok", "id": "player:kofi_asante_brook"}],
                  "drivers": [], "talent": []}), GENRES)
    assert out["excluded_genres"] == ["horror"]
    assert out["avoid_themes"] == ["animal cruelty", "jump scares"]      # trimmed, de-duplicated by case
    assert out["languages"] == ["en", "es"]
    # Free text is kept as typed; the resolver's canonical id rides alongside.
    assert out["followed"]["teams"] == [{"name": "Harbor City FC"}]
    assert out["followed"]["players"] == [{"name": "Kofi Asante Brok", "id": "player:kofi_asante_brook"}]


def test_genres_must_be_catalog_values(core):
    with pytest.raises(core.Invalid) as e:
        core.validate(full(core, excluded_genres=["horror", "splatterpunk"]), GENRES)
    assert e.value.details == [{"field": "excluded_genres", "reason": "not catalog genres: ['splatterpunk']"}]


def test_every_editable_field_is_required_and_unknown_fields_rejected(core):
    d = full(core)
    del d["consent"]
    d["favorite_genres"] = ["drama"]       # likes belong in L2, not L3
    with pytest.raises(core.Invalid) as e:
        core.validate(d, GENRES)
    reasons = " ".join(x["reason"] for x in e.value.details)
    assert "favorite_genres" in reasons and "consent" in reasons


@pytest.mark.parametrize("field,value", [
    ("kids_profile", "yes"), ("languages", ["english"]), ("subtitles", "auto"),
    ("max_length_minutes", 5), ("max_length_minutes", True), ("avoid_themes", ["x" * 81]),
    ("avoid_themes", ["bad\u0000text"]), ("avoid_themes", [f"t{i}" for i in range(11)]),
    ("followed", {"teams": [{"name": "A", "id": "Bad Id!"}]}), ("followed", {"clubs": []}),
    ("followed", {"players": [{"name": ""}]}), ("consent", {"personalization_from_history": True}),
])
def test_bad_values_are_rejected(core, field, value):
    with pytest.raises(core.Invalid) as e:
        core.validate(full(core, **{field: value}), GENRES)
    assert e.value.details[0]["field"].split(".")[0] == field


def test_followed_dedupes_by_id_then_by_name(core):
    out = core.validate(full(core, followed={"players": [
        {"name": "Kofi Asante-Brook", "id": "player:kofi_asante_brook"}, {"name": "K. Asante-Brook", "id": "player:kofi_asante_brook"},
        "kofi asante-brook", "Kofi  Asante-Brook"]}), GENRES)
    assert out["followed"]["players"] == [{"name": "Kofi Asante-Brook", "id": "player:kofi_asante_brook"}, {"name": "kofi asante-brook"}]


# --- GET -----------------------------------------------------------------

def test_get_without_item_returns_defaults_at_version_zero(fn, monkeypatch):
    status, body = call(fn, FakeStore(fn), monkeypatch)
    assert status == 200 and body["version"] == 0 and body["profile_id"] == PID
    assert body["declared"]["consent"] == {"personalization_from_history": True, "conversation_memory": True}


def test_get_seeded_item(fn, monkeypatch):
    status, body = call(fn, FakeStore(fn, SEEDED), monkeypatch)
    assert status == 200 and body["version"] == 1 and body["updated_by"] == "seed"
    assert body["declared"]["avoid_themes"] == ["excessive gore"]


@pytest.mark.parametrize("sub,pid,code", [(None, PID, 401), ("sub-2", PID, 404), ("sub-1", OTHER, 404),
                                          ("sub-1", "sports_fan", 400)])
def test_identity_and_membership(fn, monkeypatch, sub, pid, code):
    assert call(fn, FakeStore(fn, SEEDED), monkeypatch, sub=sub, pid=pid)[0] == code


# --- PUT -----------------------------------------------------------------

def put_body(core, version, **over):
    return {"version": version, "source": "onboarding", "request_id": "req-0000001",
            "declared": full(core, **over)}


def test_put_writes_bumps_version_and_emits_field_names_only(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED)
    status, body = call(fn, store, monkeypatch, "PUT", put_body(core, 1, excluded_genres=["horror"],
                        avoid_themes=["excessive gore"], languages=["en"]))
    assert status == 200 and body["version"] == 2 and body["updated_by"] == "onboarding"
    assert store.item["excluded_genres"] == ["horror"] and store.item["data_type"] == "L3#DECLARED"
    assert store.item["last_request_id"] == "req-0000001"
    assert store.emitted == [{"account_id": "sub-1", "profile_id": PID, "version": 2,
                              "fields": ["excluded_genres"], "updated_by": "onboarding"}]
    assert "horror" not in json.dumps(store.emitted)          # names, never values


def test_first_put_for_member_without_item(fn, core, monkeypatch):
    store = FakeStore(fn)
    status, body = call(fn, store, monkeypatch, "PUT", put_body(core, 0, kids_profile=True))
    assert status == 200 and body["version"] == 1 and store.item["kids_profile"] is True


def test_stale_version_gets_409_with_current_state(fn, core, monkeypatch):
    store = FakeStore(fn, {**SEEDED, "version": 3})
    status, body = call(fn, store, monkeypatch, "PUT", put_body(core, 1))
    assert status == 409 and body["error"] == "version_conflict" and body["current"]["version"] == 3
    assert store.puts == 0 and not store.emitted


def test_lost_race_on_conditional_write_gets_409(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED, conflict_on_put=True)
    status, body = call(fn, store, monkeypatch, "PUT", put_body(core, 1, excluded_genres=["horror"]))
    assert status == 409 and not store.emitted


def test_retry_of_a_successful_put_is_200_not_409(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED)
    body = put_body(core, 1, excluded_genres=["horror"], avoid_themes=["excessive gore"], languages=["en"])
    assert call(fn, store, monkeypatch, "PUT", body)[0] == 200
    status, again = call(fn, store, monkeypatch, "PUT", body)    # response was lost; client retries
    assert status == 200 and again["version"] == 2 and store.puts == 1 and len(store.emitted) == 1
    # A different request against the same stale version is a real conflict.
    assert call(fn, store, monkeypatch, "PUT", {**body, "request_id": "req-0000002"})[0] == 409


def test_unchanged_put_writes_nothing(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED)
    status, body = call(fn, store, monkeypatch, "PUT", put_body(core, 1, avoid_themes=["excessive gore"], languages=["en"]))
    assert status == 200 and body["version"] == 1 and store.puts == 0 and not store.emitted


def test_put_keeps_other_stored_attributes(fn, core, monkeypatch):
    store = FakeStore(fn, {**SEEDED, "future_field": "kept"})
    call(fn, store, monkeypatch, "PUT", put_body(core, 1, kids_profile=True))
    assert store.item["future_field"] == "kept"


def test_emit_failure_does_not_fail_a_committed_write(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED, emit_fails=True)
    status, body = call(fn, store, monkeypatch, "PUT", put_body(core, 1, kids_profile=True))
    assert status == 200 and body["version"] == 2


@pytest.mark.parametrize("body,field", [
    ({"declared": {}}, "version"), ({"version": -1, "declared": {}}, "version"),
    ({"version": 1, "source": "agent", "declared": {}}, "source"),
    ({"version": 1, "request_id": "x", "declared": {}}, "request_id"),
    ({"version": 1, "declared": {}, "profile_id": OTHER}, "body"),
])
def test_bad_put_envelopes(fn, monkeypatch, body, field):
    status, out = call(fn, FakeStore(fn, SEEDED), monkeypatch, "PUT", body)
    assert status == 400 and out["details"][0]["field"] == field


def test_put_rejects_invalid_values_without_writing(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED)
    status, out = call(fn, store, monkeypatch, "PUT", put_body(core, 1, excluded_genres=["nope"]))
    assert status == 400 and out["details"][0]["field"] == "excluded_genres" and store.puts == 0


def test_booth_callers_cannot_write(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED)
    assert call(fn, store, monkeypatch, "PUT", put_body(core, 1, kids_profile=True), groups="[booth]")[0] == 403
    assert call(fn, store, monkeypatch, "GET", groups="[booth]")[0] == 200
    assert store.puts == 0


def test_put_not_json_and_too_large(fn, monkeypatch):
    assert call(fn, FakeStore(fn, SEEDED), monkeypatch, "PUT", "{nope")[0] == 400
    assert call(fn, FakeStore(fn, SEEDED), monkeypatch, "PUT", "x" * (40 * 1024))[0] == 413


def test_cors_allows_put(fn, monkeypatch):
    monkeypatch.setattr(fn, "_store", FakeStore(fn, SEEDED))
    r = fn.lambda_handler({"httpMethod": "GET", "queryStringParameters": {"profile_id": PID},
                           "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None)
    assert "PUT" in r["headers"]["Access-Control-Allow-Methods"]


# --- genre vocabulary -----------------------------------------------------

def test_genre_vocab_is_the_static_list(fn):
    store = object.__new__(fn.Store)
    vocab = store.genre_vocab()
    assert len(vocab) == 26 and {"horror", "sci-fi", "reality-tv"} <= vocab
    assert all(g == g.strip().lower() for g in vocab)


def test_genres_file_is_well_formed(fn):
    data = json.loads(Path(fn.GENRES_FILE).read_text())
    ids = [g["id"] for g in data["genres"]]
    assert len(ids) == len(set(ids)) and all(g.get("label") for g in data["genres"])


def test_seeded_excluded_genres_are_in_static_list(fn):
    seed = json.loads((ROOT / "infra/deployment/seed/default_household.json").read_text())
    for m in seed["members"]:
        assert set((m.get("declared") or {}).get("excluded_genres", [])) <= fn.GENRE_VOCAB


def test_settings_fn_has_no_catalog_access():
    src = (ROOT / "infra/deployment/stacks.py").read_text()
    block = src[src.index("def _profile_settings"):]
    block = block[:block.index("return fn")]
    assert "aoss" not in block and "OPENSEARCH" not in block


# --- the stream processor ignores the notice ---------------------------------

def test_processor_skips_settings_changed(monkeypatch):
    import base64
    sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    processor = importlib.import_module("processor")
    seen = []
    monkeypatch.setattr(processor, "update_profile", lambda pid, evs: seen.append(evs) or len(evs))
    monkeypatch.setattr(processor, "prefetch_catalog", lambda ids: None)
    notice = {"event_id": "e1", "event_type": "settings_changed", "profile_id": PID,
              "server_ts": "2026-10-11T00:00:00.000Z", "payload": {"version": 2, "fields": ["kids_profile"]}}
    rec = {"kinesis": {"sequenceNumber": "1", "data": base64.b64encode(json.dumps(notice).encode()).decode()}}
    assert processor.lambda_handler({"Records": [rec]}, None) == {"batchItemFailures": []}
    assert seen == []


# --- loved genres (L2 head start) ---------------------------------------------

def test_loved_genres_are_sent_to_l2_not_stored_in_l3(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED)
    body = {**put_body(core, 1, excluded_genres=["horror"], avoid_themes=["excessive gore"], languages=["en"]),
            "loved_genres": ["Sci-Fi", "drama", "drama"]}
    status, out = call(fn, store, monkeypatch, "PUT", body)
    assert status == 200 and store.loved == [["sci-fi", "drama"]]
    assert "loved_genres" not in store.item and "loved_genres" not in out["declared"]


def test_loved_genres_sent_even_when_rules_unchanged(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED)
    body = {**put_body(core, 1, avoid_themes=["excessive gore"], languages=["en"]), "loved_genres": ["comedy"]}
    status, _ = call(fn, store, monkeypatch, "PUT", body)
    assert status == 200 and store.puts == 0 and store.loved == [["comedy"]]


@pytest.mark.parametrize("loved,reason", [(["splatterpunk"], "not catalog genres"), (["horror"], "also excluded"),
                                          ("drama", "must be a list")])
def test_loved_genres_validated(fn, core, monkeypatch, loved, reason):
    store = FakeStore(fn, SEEDED)
    body = {**put_body(core, 1, excluded_genres=["horror"]), "loved_genres": loved}
    status, out = call(fn, store, monkeypatch, "PUT", body)
    assert status == 400 and reason in out["details"][0]["reason"] and store.puts == 0


def test_get_lists_genre_options(fn, monkeypatch):
    status, out = call(fn, FakeStore(fn, SEEDED), monkeypatch)
    assert status == 200 and {"id": "sci-fi", "label": "Sci-Fi"} in out["options"]["genres"]


def _l2():
    sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))
    return importlib.import_module("l2")


def test_seed_loved_weak_start_never_lowers_learned():
    l2 = _l2()
    ents = {"genre:drama": {"s": 0.9, "n": 12, "label": "Drama", "last": 1000}}
    changed = l2.seed_loved(ents, [{"id": "sci-fi", "label": "Sci-Fi"}, {"id": "drama", "label": "Drama"}], 1000)
    assert changed == ["genre:sci-fi"]
    assert ents["genre:sci-fi"] == {"s": 0.5, "n": 3, "label": "Sci-Fi", "last": 1000}
    assert ents["genre:drama"]["s"] == 0.9
    assert l2.seed_loved(ents, [{"id": "sci-fi", "label": "Sci-Fi"}], 1000) == []   # repeat is a no-op


def test_processor_applies_loved_only_from_settings_api(monkeypatch):
    import base64
    sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    processor = importlib.import_module("processor")
    applied, behaviour = [], []
    monkeypatch.setattr(processor, "apply_loved", lambda pid, genres: applied.append((pid, genres)))
    monkeypatch.setattr(processor, "update_profile", lambda pid, evs: behaviour.append(evs) or len(evs))
    monkeypatch.setattr(processor, "prefetch_catalog", lambda ids: None)

    def rec(seq, producer):
        ev = {"event_id": seq, "event_type": "genres_loved", "profile_id": PID, "producer": producer,
              "payload": {"genres": [{"id": "sci-fi", "label": "Sci-Fi"}]}}
        return {"kinesis": {"sequenceNumber": seq, "data": base64.b64encode(json.dumps(ev).encode()).decode()}}
    assert processor.lambda_handler({"Records": [rec("1", "profile_settings"), rec("2", "client")]}, None) \
        == {"batchItemFailures": []}
    assert applied == [(PID, [{"id": "sci-fi", "label": "Sci-Fi"}])] and behaviour == []


# --- synthesized wiring ------------------------------------------------------

def application_template():
    sys.path.insert(0, str(ROOT / "infra/deployment"))
    from config import load_config, stack_name
    cfg = load_config()
    path = Path(cfg["artifactDir"]) / "cdk.out" / (stack_name(cfg, "Application") + ".template.json")
    if not path.exists():
        pytest.skip("Synthesize infra/deployment/app.py first")
    return cfg, json.loads(path.read_text())


def test_declared_routes_require_cognito_and_cors_allows_put():
    _, app = application_template()
    res = {k: v["Properties"] for k, v in app["Resources"].items() if v["Type"] == "AWS::ApiGateway::Resource"}
    declared = [k for k, p in res.items() if p["PathPart"] == "declared"
                and res.get(p["ParentId"].get("Ref"), {}).get("PathPart") == "profile"]
    assert len(declared) == 1
    methods = [v["Properties"] for v in app["Resources"].values() if v["Type"] == "AWS::ApiGateway::Method"
               and v["Properties"].get("ResourceId", {}).get("Ref") == declared[0]]
    secured = sorted((m["HttpMethod"], m["AuthorizationType"]) for m in methods if m["HttpMethod"] != "OPTIONS")
    assert secured == [("GET", "COGNITO_USER_POOLS"), ("PUT", "COGNITO_USER_POOLS")]
    [options] = [m for m in methods if m["HttpMethod"] == "OPTIONS"]
    allow = options["Integration"]["IntegrationResponses"][0]["ResponseParameters"][
        "method.response.header.Access-Control-Allow-Methods"]
    assert "PUT" in allow


def test_settings_function_cannot_delete_or_batch():
    _, app = application_template()
    [fn] = [k for k, v in app["Resources"].items() if v["Type"] == "AWS::Lambda::Function" and k.startswith("ProfileSettingsFn")]
    role = app["Resources"][fn]["Properties"]["Role"]["Fn::GetAtt"][0]
    actions = set()
    for v in app["Resources"].values():
        if v["Type"] == "AWS::IAM::Policy" and {"Ref": role} in v["Properties"]["Roles"]:
            for st in v["Properties"]["PolicyDocument"]["Statement"]:
                a = st["Action"]
                actions.update(a if isinstance(a, list) else [a])
    dynamo = {a for a in actions if a.startswith("dynamodb:")}
    assert dynamo == {"dynamodb:GetItem", "dynamodb:PutItem"}


# --- followed-entity resolver ----------------------------------------------

@pytest.fixture
def registry():
    return importlib.import_module("entity_resolver").Registry.load()


@pytest.mark.parametrize("text,expected", [
    ("Kofi Asante-Brook", "player:kofi-asante-brook"), ("Kofi Asante Brok", "player:kofi-asante-brook"),
    ("asante-brook", "player:kofi-asante-brook"), ("K. Asante-Brook", "player:kofi-asante-brook"),
    ("hcf", "team:harbor-city-fc"), ("Harbour City", "team:harbor-city-fc"),
    ("Redclife Athletic", "team:redcliffe-athletic"), ("Tobias Lark", "player:tobiah-lark"),
    ("okafor hale", "player:desmond-okafor-hale"),
    # Unknown or too short to correct: stays unresolved rather than guessed.
    ("Spurs", None), ("max", None), ("Finn", None), ("", None),
])
def test_resolver_maps_free_text_to_one_entity(registry, text, expected):
    found = registry.resolve(text)
    assert (found and found["id"]) == expected


def test_resolver_files_matches_under_their_kind_and_rejects_unknown_ids(registry):
    out = registry.resolve_followed({
        "teams": [{"name": "Kofi Asante-Brook"}, {"name": "redcliffe"}, {"name": "Spurs"}],
        "players": [{"name": "asante-brook", "id": "player:not-real"}],
        "drivers": [], "talent": [{"name": "Nolan"}]})
    assert out["teams"] == [{"name": "Redcliffe Athletic", "id": "team:redcliffe-athletic"}, {"name": "Spurs"}]
    assert out["players"] == [{"name": "Kofi Asante-Brook", "id": "player:kofi-asante-brook"}]   # once, canonical
    assert out["talent"] == [{"name": "Nolan"}]                                    # not a sports kind


def test_registry_ids_are_unique_and_well_formed():
    data = json.loads((ROOT / "infra/lambdas/profile_settings/sports_entities.json").read_text())
    ids = [e["id"] for e in data["entities"]]
    assert len(ids) == len(set(ids))
    for e in data["entities"]:
        assert e["type"] in ("team", "player", "driver") and e["id"].startswith(e["type"] + ":")
        if "team" in e:
            assert e["team"] in ids


def test_frontend_live_entities_exist_in_registry():
    """Every entity the frontend's live content tags must be one the resolver can store."""
    import re
    data = json.loads((ROOT / "infra/lambdas/profile_settings/sports_entities.json").read_text())
    known = {e["id"] for e in data["entities"]}
    src = (ROOT / "frontend/src/data/sports.ts").read_text()
    ids = set(re.findall(r"'((?:team|player|driver):[a-z0-9-]+)'", src))
    ids |= {f"player:{slug}" for slug in re.findall(r"\bsoccerPlayer\('([a-z0-9-]+)'", src)}
    assert len(ids) >= 20 and ids <= known, sorted(ids - known)


def test_get_resolves_followed_saved_before_the_resolver(fn, monkeypatch):
    item = {**SEEDED, "followed": {"teams": [{"name": "Harbor City FC"}], "players": [], "drivers": [], "talent": []}}
    store = FakeStore(fn, item)
    status, body = call(fn, store, monkeypatch)
    assert status == 200
    assert body["declared"]["followed"]["teams"] == [{"name": "Harbor City FC", "id": "team:harbor-city-fc"}]
    assert store.item["followed"]["teams"] == [{"name": "Harbor City FC"}]     # read never writes


def test_put_stores_resolved_followed(fn, core, monkeypatch):
    store = FakeStore(fn, SEEDED)
    followed = {"teams": [{"name": "Kofi Asante Brok"}], "players": [], "drivers": [{"name": "verstapen"}], "talent": []}
    status, body = call(fn, store, monkeypatch, "PUT", put_body(core, 1, avoid_themes=["excessive gore"],
                        languages=["en"], followed=followed))
    assert status == 200
    assert store.item["followed"]["players"] == [{"name": "Kofi Asante-Brook", "id": "player:kofi-asante-brook"}]
    assert store.item["followed"]["drivers"] == [{"name": "verstapen"}]   # no motorsport entities: kept as typed
    assert store.item["followed"]["teams"] == []


def test_movie_role_can_resolve_story_model_profile():
    """RetrieveAndGenerate (overall-story questions) calls GetInferenceProfile on the scene model."""
    cfg, app = application_template()
    stmts = [s for v in app["Resources"].values() if v["Type"] == "AWS::IAM::Policy"
             and any("MovieRole" in str(r) for r in v["Properties"].get("Roles", []))
             for s in v["Properties"]["PolicyDocument"]["Statement"]]
    grants = [s for s in stmts if "bedrock:GetInferenceProfile" in (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
    assert grants and cfg["models"]["scene"] in json.dumps(grants)


# --- Registry build + type-ahead (GET /profile/entities) ---------------------

def test_registry_is_built_from_the_checked_in_roster():
    """sports_entities.json must be exactly what the build script makes from the roster."""
    import subprocess
    r = subprocess.run([sys.executable, str(ROOT / "scripts/build_sports_entities.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


def test_roster_covers_the_whole_match_squad():
    data = json.loads((ROOT / "infra/lambdas/profile_settings/sports_entities.json").read_text())
    ids = {e["id"] for e in data["entities"]}
    meta = json.loads((ROOT / "backend/live-sports-agent/data/match_meta.json").read_text())
    names = {e["name"] for e in data["entities"] if e["type"] == "player"}
    assert names == {p["name"] for p in meta["players"]}     # every squad member, nobody else
    assert {e["id"] for e in data["entities"] if e["type"] == "team"} == {
        "team:harbor-city-fc", "team:redcliffe-athletic"}
    assert sum(e["type"] == "driver" for e in data["entities"]) == 0


def test_roster_keeps_only_public_facts():
    """No feed ids or tracking data in the checked-in copy."""
    roster = json.loads((ROOT / "infra/deployment/seed/sports_roster.json").read_text())
    allowed = {"teams": {"id", "name", "sport", "official_name"},
               "players": {"id", "name", "team", "number"},
               "drivers": {"id", "name", "team", "number", "code"}}
    for kind, keys in allowed.items():
        for row in roster[kind]:
            assert set(row) <= keys, (kind, row)


def test_surnames_and_aliases_resolve(fn):
    reg = fn.ENTITIES
    assert reg.resolve("RCA")["id"] == "team:redcliffe-athletic"
    assert reg.resolve("Zielonka")["id"] == "player:marek-zielonka"
    assert reg.resolve("Ostergaard")["id"] == "player:finn-ostergaard"


def entities(fn, monkeypatch, q, kind=None, method="GET", sub="sub-1"):
    monkeypatch.setattr(fn, "_store", None)   # the route never touches the store
    params = {"q": q, **({"kind": kind} if kind else {})}
    ev = {"httpMethod": method, "resource": "/profile/entities", "queryStringParameters": params,
          "requestContext": {"authorizer": {"claims": {"sub": sub} if sub else {}}}}
    r = fn.lambda_handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def test_entities_prefix_typo_and_kind_order(fn, monkeypatch):
    status, body = entities(fn, monkeypatch, "asante")
    assert status == 200 and body["entities"][0]["id"] == "player:kofi-asante-brook"
    first = body["entities"][0]
    assert first["kind"] == "players" and first["team"] == "Harbor City FC" and first["number"] == 11
    _, body = entities(fn, monkeypatch, "zielonak")            # typo
    assert body["entities"][0]["id"] == "player:marek-zielonka"
    _, body = entities(fn, monkeypatch, "har", kind="teams")   # requested kind first
    assert body["entities"][0]["kind"] == "teams"
    _, body = entities(fn, monkeypatch, "the cliffs")          # nickname from the alias file
    assert body["entities"][0]["id"] == "team:redcliffe-athletic"


def test_entities_needs_no_profile_but_needs_a_caller(fn, monkeypatch):
    assert entities(fn, monkeypatch, "lark")[0] == 200
    assert entities(fn, monkeypatch, "lark", sub=None)[0] == 401
    assert entities(fn, monkeypatch, "lark", method="PUT")[0] == 405
    status, body = entities(fn, monkeypatch, "")
    assert status == 200 and body["entities"] == []
    status, body = entities(fn, monkeypatch, "x" * 500)
    assert status == 200 and body["entities"] == []
