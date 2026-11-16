"""Events pipeline: contract, L1 fold, POST /events ingestion, stream processor, CDK wiring."""
import base64
import importlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True  # Lambda directories are CDK assets
sys.path.insert(0, str(ROOT / "infra/lambdas/profile_events"))

contract = importlib.import_module("contract")
l1 = importlib.import_module("l1")

PID = "p-" + "a" * 32
SID = "s-1"
CATALOG = {
    "tt1": {"title": "Paddington 2", "genres": ["Comedy", "Family"], "cast": ["Ben Whishaw", "Hugh Grant"]},
    "tt2": {"title": "Heat", "genres": ["Crime"], "cast": ["Al Pacino"]},
}


def ev(etype, n, payload, *, sid=SID, ts=None, device="phone", surface="vod_player"):
    ts = ts or f"2026-10-04T12:{n // 60:02d}:{n % 60:02d}.000Z"
    return {"event_id": f"e{n}-{etype}", "schema_version": 1, "event_type": etype, "profile_id": PID,
            "session_id": sid, "device": device, "surface": surface, "client_ts": ts, "server_ts": ts,
            "payload": payload}


def client(event):
    return {k: v for k, v in event.items() if k not in {"server_ts"}}


# --- contract ------------------------------------------------------------

def test_valid_events_pass():
    for e in [ev("play_start", 0, {"item_id": "tt1", "content_type": "movie", "position_s": 0}),
              ev("progress", 1, {"item_id": "tt1", "position_s": 30, "watched_s": 30}),
              ev("play_end", 2, {"item_id": "tt1", "reason": "closed"}),
              ev("impression", 3, {"item_id": "md-1", "rank": 2}, surface="clips_feed"),
              ev("live_join", 5, {"game_id": "g-soccer", "video_s": 12.5}, surface="sports"),
              ev("feedback", 6, {"item_id": "md0000004", "value": "more", "content_type": "microdrama"},
                 surface="clips_feed"),
              ev("play_start", 7, {"item_id": "tt1", "content_type": "title_card", "parent_item_id": "tt1"},
                 surface="clips_feed")]:
        assert contract.validate(client(e)) is None, e["event_type"]


def test_feedback_values_are_closed():
    e = client(ev("feedback", 0, {"item_id": "tt1", "value": "love it"}, surface="clips_feed"))
    assert contract.validate(e) == "payload.value"


@pytest.mark.parametrize("mutate,reason", [
    (lambda e: e.update(server_ts="x"), "unknown fields"),               # server-set field from client
    (lambda e: e.update(booth=True), "unknown fields"),
    (lambda e: e["payload"].update(query="find me horror"), "unknown payload"),  # no free text
    (lambda e: e.update(event_type="purchase"), "event_type"),
    (lambda e: e.update(event_type="agent_reject"), "event_type"),   # rejections live in LTM
    (lambda e: e.update(device="fridge"), "device"),
    (lambda e: e.update(profile_id="has spaces"), "profile_id"),
    (lambda e: e["payload"].update(item_id="<script>"), "payload.item_id"),
    (lambda e: e["payload"].pop("content_type"), "payload.content_type missing"),
    (lambda e: e["payload"].update(position_s=True), "payload.position_s"),
    (lambda e: e.update(schema_version=2), "schema_version"),
])
def test_invalid_events_are_rejected(mutate, reason):
    e = client(ev("play_start", 0, {"item_id": "tt1", "content_type": "movie", "position_s": 0}))
    mutate(e)
    assert reason in contract.validate(e)


# --- L1 fold -------------------------------------------------------------

def fold(events, state=None):
    return l1.fold(state, events, CATALOG.get)


def test_heartbeats_fold_into_one_view_with_facts():
    state, applied = fold([
        ev("play_start", 0, {"item_id": "tt1", "content_type": "movie", "position_s": 0, "duration_s": 6000}),
        *[ev("progress", 30 * i, {"item_id": "tt1", "position_s": 30 * i, "watched_s": 30}) for i in range(1, 11)],
        ev("play_end", 301, {"item_id": "tt1", "reason": "closed", "position_s": 300}),
    ])
    assert applied == 12 and state["seq"] == 12
    assert state["views"] == {}
    [r] = state["recent"]
    assert r["item"] == "tt1" and r["title"] == "Paddington 2" and r["reason"] == "closed"
    assert r["completion"] == 0.05 and r["watched_s"] == 300 and r["plays"] == 1
    assert state["format_mix"] == {"short_min": 0.0, "long_min": 5.0}
    assert state["entities"]["genre:comedy"]["label"] == "Comedy"
    assert set(state["entities"]) == {"genre:comedy", "genre:family", "person:ben_whishaw", "person:hugh_grant"}
    assert "intent" not in state and "mode" not in state  # facts only


def test_short_form_uses_parent_title_for_entities_and_counts_short_minutes():
    state, _ = fold([
        ev("play_start", 0, {"item_id": "md-7", "content_type": "microdrama", "parent_item_id": "tt2"}, surface="clips_feed"),
        ev("play_end", 20, {"item_id": "md-7", "reason": "ended", "watched_s": 20}, surface="clips_feed"),
    ])
    [r] = state["recent"]
    assert r["completion"] == 1.0 and r["title"] == "Heat" and r["parent"] == "tt2"
    assert state["format_mix"]["short_min"] == pytest.approx(0.33, abs=0.01)
    assert "person:al_pacino" in state["entities"]


def test_rewatch_keeps_one_recent_entry_and_counts_plays():
    seq = []
    for k in range(3):
        seq += [ev("play_start", 10 * k, {"item_id": "md-7", "content_type": "microdrama"}),
                ev("play_end", 10 * k + 5, {"item_id": "md-7", "reason": "ended", "watched_s": 5})]
    state, _ = fold(seq)
    assert len(state["recent"]) == 1 and state["recent"][0]["plays"] == 3


def test_entity_attention_decays_with_20_minute_half_life():
    state, _ = fold([ev("progress", 0, {"item_id": "tt2", "watched_s": 60, "content_type": "movie"},
                        ts="2026-10-04T12:00:00.000Z"),
                     ev("impression", 1, {"item_id": "x"}, ts="2026-10-04T12:20:00.000Z", surface="clips_feed")])
    assert state["entities"]["genre:crime"]["m"] == pytest.approx(0.5, abs=0.01)


def test_view_without_heartbeat_for_10_minutes_is_closed():
    state, _ = fold([ev("play_start", 0, {"item_id": "tt2", "content_type": "movie"}, ts="2026-10-04T12:00:00.000Z"),
                     ev("impression", 1, {"item_id": "x"}, ts="2026-10-04T12:11:00.000Z", surface="clips_feed")])
    assert state["views"] == {} and state["recent"][0]["reason"] == "timeout"


def test_new_session_starts_fresh():
    state, _ = fold([ev("play_end", 0, {"item_id": "tt1", "reason": "ended", "content_type": "movie"})])
    state, _ = fold([ev("list_add", 1, {"item_id": "tt2"}, sid="s-2")], state)
    assert state["session_id"] == "s-2" and state["recent"] == [] and state["list_adds"] == ["tt2"]


def test_duplicate_delivery_is_applied_once():
    batch = [ev("progress", 0, {"item_id": "tt1", "watched_s": 30, "content_type": "movie"})]
    state, _ = fold(batch)
    state, applied = fold(batch, state)
    assert applied == 0 and state["format_mix"]["long_min"] == 0.5


def test_live_lists_and_impressions():
    state, _ = fold([ev("live_join", 0, {"game_id": "g-soccer", "video_s": 100}, surface="sports"),
                     ev("list_add", 1, {"item_id": "tt1"}, surface="title_detail"),
                     ev("list_add", 2, {"item_id": "tt2"}, surface="title_detail"),
                     ev("list_remove", 3, {"item_id": "tt1"}, surface="title_detail"),
                     ev("agent_accept", 4, {"item_id": "tt2"}, surface="agent"),
                     ev("impression", 5, {"item_id": "md-1"}, surface="clips_feed"),
                     ev("impression", 6, {"item_id": "md-2"}, surface="clips_feed")])
    assert state["live"]["game_id"] == "g-soccer"
    assert state["list_adds"] == ["tt2"] and state["agent_accepts"] == ["tt2"]
    assert state["impressions"] == {"clips_feed": 2}
    assert state["expires_at"] == int(l1.parse_ts("2026-10-04T12:00:06Z") + 4 * 3600)


def test_one_event_cannot_credit_hours_of_watching():
    state, _ = fold([ev("progress", 0, {"item_id": "tt1", "watched_s": 99999, "content_type": "movie"})])
    assert state["format_mix"]["long_min"] == 2.0


def test_cast_credits_become_person_entities_without_the_role():
    ents = l1.entities_for({"genres": [], "cast": ["Will Arnett as (Batman, Bruce Wayne)", "Hugh Grant"]})
    assert ents == {"person:will_arnett": "Will Arnett", "person:hugh_grant": "Hugh Grant"}


# --- ingestion -----------------------------------------------------------

@pytest.fixture
def ingest(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    return importlib.import_module("ingest")


def members(account, profile):
    return {("acct-1", PID): (True, True), ("acct-1", "p-" + "b" * 32): (True, False)}.get((account, profile), (False, True))


def test_ingest_stamps_server_fields_and_checks_membership(ingest):
    other = "p-" + "c" * 32
    no_consent = "p-" + "b" * 32
    good = client(ev("play_start", 0, {"item_id": "tt1", "content_type": "movie"}))
    body = {"events": [good, dict(good, profile_id=other, event_id="e2"),
                       dict(good, profile_id=no_consent, event_id="e3"), {"bad": 1}]}
    records, dropped, rejected = ingest.process(body, "acct-1", booth=True, lookup=members)
    assert [r["event_id"] for r in records] == [good["event_id"]]
    assert records[0]["account_id"] == "acct-1" and records[0]["booth"] is True
    assert records[0]["server_ts"].endswith("Z")
    assert dropped == 1
    assert [r["index"] for r in rejected] == [1, 3]


def test_ingest_rejects_oversized_batches(ingest):
    e = client(ev("list_add", 0, {"item_id": "tt1"}))
    with pytest.raises(ValueError):
        ingest.process({"events": [e] * 51}, "acct-1", False, lookup=members)
    with pytest.raises(ValueError):
        ingest.process({"events": []}, "acct-1", False, lookup=members)


def test_ingest_requires_identity(ingest):
    assert ingest.lambda_handler({"body": "{}"}, None)["statusCode"] == 401


@pytest.mark.parametrize("groups,expected", [("booth", True), ("[booth, admin]", True), (["booth"], True),
                                             ("admins", False), ("", False)])
def test_booth_flag_comes_from_cognito_group(ingest, groups, expected):
    assert ingest.is_booth({"cognito:groups": groups}) is expected


# --- processor -----------------------------------------------------------

class FakeTable:
    def __init__(self, conflicts=0):
        self.item, self.conflicts, self.puts = None, conflicts, 0

    def get_item(self, Key, ConsistentRead=False):
        return {"Item": json.loads(json.dumps(self.item), parse_float=__import__("decimal").Decimal)} if self.item else {}

    def put_item(self, Item, ConditionExpression, ExpressionAttributeValues=None):
        from botocore.exceptions import ClientError
        if self.conflicts:
            self.conflicts -= 1
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        expected = (ExpressionAttributeValues or {}).get(":old")
        assert (self.item or {}).get("seq") == expected
        self.puts += 1
        self.item = json.loads(json.dumps(Item, default=float))


@pytest.fixture
def processor(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    mod = importlib.import_module("processor")
    mod._catalog.clear()
    mod.prefetch_catalog(["tt1", "tt2", "md-1"], fetch=lambda ids: {i: CATALOG[i] for i in ids if i in CATALOG})
    return mod


def test_processor_writes_behavior_item_and_retries_seq_conflicts(processor):
    table = FakeTable(conflicts=1)
    events = [ev("play_start", 0, {"item_id": "tt1", "content_type": "movie"}),
              ev("progress", 30, {"item_id": "tt1", "watched_s": 30})]
    assert processor.update_profile(PID, events, table=table) == 2
    assert table.item["sk"] == "SESSION#behavior" and table.item["profile_id"] == PID
    assert table.item["seq"] == 2 and table.item["views"]["tt1"]["title"] == "Paddington 2"
    assert processor.update_profile(PID, [ev("play_end", 60, {"item_id": "tt1", "reason": "closed"})], table=table) == 1
    assert table.item["seq"] == 3 and table.puts == 2


def test_processor_skips_write_for_duplicates(processor):
    table = FakeTable()
    events = [ev("list_add", 0, {"item_id": "tt1"})]
    processor.update_profile(PID, events, table=table)
    assert processor.update_profile(PID, events, table=table) == 0 and table.puts == 1


def test_processor_reports_earliest_failed_record(processor, monkeypatch):
    def boom(profile_id, events, table=None):
        raise RuntimeError("down")
    monkeypatch.setattr(processor, "update_profile", boom)
    monkeypatch.setattr(processor, "prefetch_catalog", lambda ids: None)
    recs = [{"kinesis": {"sequenceNumber": str(n), "data": base64.b64encode(json.dumps(
        ev("list_add", n, {"item_id": "tt1"})).encode()).decode()}} for n in (7, 5, 9)]
    assert processor.lambda_handler({"Records": recs}, None) == {"batchItemFailures": [{"itemIdentifier": "5"}]}


# --- synthesized wiring ----------------------------------------------------

def application_template():
    sys.path.insert(0, str(ROOT / "infra/deployment"))
    from config import load_config, stack_name
    cfg = load_config()
    path = Path(cfg["artifactDir"]) / "cdk.out" / (stack_name(cfg, "Application") + ".template.json")
    if not path.exists():
        pytest.skip("Synthesize infra/deployment/app.py first")
    return cfg, json.loads(path.read_text())


def of_type(app, t):
    return [r["Properties"] for r in app["Resources"].values() if r["Type"] == t]


def test_events_route_requires_cognito():
    _, app = application_template()
    resources = {k: v["Properties"]["PathPart"] for k, v in app["Resources"].items() if v["Type"] == "AWS::ApiGateway::Resource"}
    methods = [m for m in of_type(app, "AWS::ApiGateway::Method") if m["HttpMethod"] != "OPTIONS"
               and resources.get(m.get("ResourceId", {}).get("Ref")) == "events"]
    assert [(m["HttpMethod"], m["AuthorizationType"]) for m in methods] == [("POST", "COGNITO_USER_POOLS")]


def test_stream_firehose_and_processor_mapping():
    cfg, app = application_template()
    [stream] = of_type(app, "AWS::Kinesis::Stream")
    assert stream["StreamModeDetails"] == {"StreamMode": "ON_DEMAND"} and stream["RetentionPeriodHours"] == 168
    assert stream["StreamEncryption"]["EncryptionType"] == "KMS"
    [fh] = of_type(app, "AWS::KinesisFirehose::DeliveryStream")
    s3cfg = fh["ExtendedS3DestinationConfiguration"]
    assert s3cfg["CompressionFormat"] == "GZIP" and s3cfg["BufferingHints"] == {"IntervalInSeconds": 60, "SizeInMBs": 64}
    [esm] = of_type(app, "AWS::Lambda::EventSourceMapping")
    assert esm["BatchSize"] == 200 and esm["MaximumBatchingWindowInSeconds"] == 2
    assert esm["FunctionResponseTypes"] == ["ReportBatchItemFailures"] and esm["BisectBatchOnFunctionError"] is True
    assert esm["MaximumRetryAttempts"] == 3 and esm["ParallelizationFactor"] == 1
    assert "DestinationConfig" in esm
    [q] = [q for q in of_type(app, "AWS::SQS::Queue")]
    assert q.get("SqsManagedSseEnabled", True)
