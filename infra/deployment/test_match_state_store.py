"""The live match-state engine reads only from its event store (no feed files)."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COPIES = [ROOT / "infra/lambdas/match_state", ROOT / "backend/live-sports-agent"]

META = {
    "match": {"match_id": "M1"},
    "teams": {"home": {"short_name": "Home FC"}, "guest": {"short_name": "Away FC"}},
    "clock": {"hz": 25, "halves": {
        "firstHalf": {"first_frame_id": 1000, "last_frame_id": 1000 + 25 * 60,
                      "first_timestamp_utc": "2026-01-01T18:00:00Z", "last_timestamp_utc": "2026-01-01T18:01:00Z",
                      "anchor_video_s": 10.0},
        "secondHalf": {"first_frame_id": 5000, "last_frame_id": 5000 + 25 * 60,
                       "first_timestamp_utc": "2026-01-01T18:20:00Z", "last_timestamp_utc": "2026-01-01T18:21:00Z",
                       "anchor_video_s": 200.0}}},
    "players": [],
}


def load_module(directory, name):
    sys.path.insert(0, str(directory))
    try:
        for mod in ("match_state", "event_store"):
            sys.modules.pop(mod, None)
        spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(directory))


class FakeStore:
    def __init__(self, events, fail=False):
        self.events, self.fail, self.bounds = events, fail, []

    def meta(self):
        return META

    def events_upto(self, bound_ms):
        self.bounds.append(bound_ms)
        if self.fail:
            raise RuntimeError("table down")
        return [e for e in self.events if e["_ms"] < bound_ms]


@pytest.mark.parametrize("directory", COPIES, ids=["lambda", "agent"])
def test_structure_and_events_come_from_the_store(directory):
    ms = load_module(directory, "match_state")
    store = FakeStore([])
    state = ms.load(store)
    assert state.side_name == {"home": "Home FC", "guest": "Away FC"}
    assert state.clock(1000 + 25 * 30) == {"period": "firstHalf", "minute": 1, "label": "1'"}
    state.at(1000 + 25 * 30)
    assert store.bounds, "events were not read from the store"
    assert not hasattr(state, "events"), "no in-memory feed copy"


@pytest.mark.parametrize("directory", COPIES, ids=["lambda", "agent"])
def test_store_errors_are_not_hidden_by_a_file_fallback(directory):
    ms = load_module(directory, "match_state")
    state = ms.load(FakeStore([], fail=True))
    with pytest.raises(RuntimeError, match="table down"):
        state.at(1200)


@pytest.mark.parametrize("directory", COPIES, ids=["lambda", "agent"])
def test_meta_item_is_never_counted_or_returned_as_an_event(directory):
    es = load_module(directory, "event_store")
    assert es.META_SK > es.sk_for(999_999_999_999_999, 9999)   # above every event key
    assert es.sk_for(0, 0) < es._EVENTS_END < es.META_SK         # count() stops before META


def test_runtimes_do_not_ship_or_read_the_feed_files():
    for path in [ROOT / "infra/lambdas/match_state/lambda_function.py",
                 ROOT / "backend/live-sports-agent/agentcore_app.py",
                 ROOT / "backend/live-sports-agent/Dockerfile"]:
        text = path.read_text()
        for name in ("match_events", "match_meta", "match_commentary.vtt"):
            assert name not in text, (path.name, name)
    stacks = (ROOT / "infra/deployment/stacks.py").read_text()
    assert '"live-sports-agent/data/**"' in stacks
    assert not (ROOT / "infra/lambdas/match_state/data").exists(), "the Lambda bundles no feed copy"
