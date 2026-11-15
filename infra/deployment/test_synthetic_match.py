"""The synthetic demo match: generated data loads into the engine and stays spoiler-safe."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "backend/live-sports-agent"
DATA = AGENT / "data"
COPIES = [ROOT / "infra/lambdas/match_state", AGENT]
GOAL_VIDEO_S = 25.84


def load_module(directory, name):
    sys.path.insert(0, str(directory))
    try:
        for mod in ("match_state", "event_store", "build_reel"):
            sys.modules.pop(mod, None)
        spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(directory))


FEED = json.loads((DATA / "match_events.json").read_text())
META_FILE = json.loads((DATA / "match_meta.json").read_text())


class FileStore:
    """The DynamoDB store's contract over the generated files, built with the
    loader's own META/item code so the test covers what gets loaded."""

    def __init__(self, ms):
        loader = load_module(AGENT, "load_events_dynamodb")
        self._meta = json.loads(loader.meta_item(FEED, META_FILE, FEED["match"]["match_id"])["payload"])
        self._events = sorted(FEED["events"], key=lambda e: ms.iso_to_ms(e["event_time_utc"]))
        self._ms = ms
        self.bounds = []

    def meta(self):
        return self._meta

    def events_upto(self, bound_ms):
        self.bounds.append(bound_ms)
        return [e for e in self._events if self._ms.iso_to_ms(e["event_time_utc"]) < bound_ms]


@pytest.fixture(params=COPIES, ids=["lambda", "agent"])
def state(request):
    ms = load_module(request.param, "match_state")
    return ms.load(FileStore(ms))


def snap(state, video_s):
    return state.at(state.frame_for_video_s(video_s))


def test_generator_output_is_current():
    r = subprocess.run([sys.executable, str(AGENT / "build_synthetic_match.py"), "--check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_feed_shape_and_ids():
    assert FEED["match"]["match_id"] == "SYN-MAT-0001" == load_module(AGENT, "match_state").DEFAULT_MATCH_ID
    assert FEED["teams"]["home"]["three_letter_code"] == "HCF"
    assert FEED["teams"]["guest"]["three_letter_code"] == "RCA"
    assert FEED["event_count"] == len(FEED["events"])
    keys = {"event_id", "type", "subtype", "game_section", "minute_label", "minute", "stoppage",
            "event_time_utc", "frame_id", "video_s", "team_id", "team_name", "team_side", "player_id",
            "player_name", "is_highlight", "highlight_type", "xg", "chance_evaluation", "clip",
            "resolved_names", "attributes"}
    roster = {p["player_id"] for p in META_FILE["players"]}
    for e in FEED["events"]:
        assert set(e) == keys, e["event_id"]
        assert e["player_id"] is None or e["player_id"] in roster
        assert e["player_id"] is None or e["player_id"].startswith("SYN-OBJ-")
    assert all(p["face_id"] is None for p in META_FILE["players"])   # no faces shipped


def test_only_in_clip_highlights_have_a_clip_window():
    for e in FEED["events"]:
        if e["clip"]:
            assert e["is_highlight"] and 0 <= e["clip"]["in_video_s"] <= e["video_s"] <= e["clip"]["out_video_s"] <= 60
        if e["video_s"] < 0:
            assert e["clip"] is None
    assert not [e for e in FEED["events"] if e["video_s"] > 60], "nothing after the clip"
    assert not [e for e in FEED["events"] if e["type"] == "FinalWhistle" and e["game_section"] == "secondHalf"]


def test_clock_maps_the_clip_onto_the_second_half(state):
    assert state.clock(state.frame_for_video_s(0)) == {"period": "secondHalf", "minute": 78, "label": "78'"}
    assert state.clock(state.frame_for_video_s(60))["label"] == "79'"
    # negative playheads (before the broadcast) still map onto the match
    assert state.clock(state.frame_for_video_s(-5700))["label"] == "1'"
    # the half-time gap clamps to the nearest half edge (the second-half kick-off)
    assert state.clock(state.frame_for_video_s(-2000))["label"] == "46'"


def test_score_before_the_clip_goal_and_exact_change_at_the_goal_frame(state):
    assert snap(state, 0)["score"] == {"Harbor City": 1, "Redcliffe": 1}
    assert snap(state, -3000)["score"] == {"Harbor City": 0, "Redcliffe": 1}
    goal_frame = state.frame_for_video_s(GOAL_VIDEO_S)
    assert state.at(goal_frame - 1)["score"] == {"Harbor City": 1, "Redcliffe": 1}
    assert state.at(goal_frame)["score"] == {"Harbor City": 2, "Redcliffe": 1}
    assert snap(state, 60)["score"] == {"Harbor City": 2, "Redcliffe": 1}


def test_nothing_after_the_playhead_leaks(state):
    for video_s in (-4000, 0, 10, 24.0, GOAL_VIDEO_S - 0.04, 40, 60):
        frame = state.frame_for_video_s(video_s)
        s = state.at(frame, recent_n=100)
        assert all(h["video_s"] <= video_s + 0.02 for h in s["highlights"]), video_s
        assert all(h["frame_id"] <= frame for h in s["highlights"])
        assert len(s["recent_events"]) == sum(1 for e in FEED["events"] if e["frame_id"] <= frame)
        assert state.player_stats(frame, "Asante-Brook")["goals"] == (1 if video_s >= GOAL_VIDEO_S else 0)
        if "play_by_play" in s:   # the Lambda copy also builds the UI feed
            goals = [r["detail"] for r in s["play_by_play"] if r["kind"] == "goal"]
            assert ("Kofi Asante-Brook scores" in goals) == (video_s >= GOAL_VIDEO_S)
            assert {p["name"] for p in s["players"]} <= {p["name"] for p in META_FILE["players"]}


def test_reel_skips_pre_clip_highlights(state):
    reel = load_module(ROOT / "infra/lambdas/match_state", "build_reel")
    out = reel.build(state, state.frame_for_video_s(60), reel.DEFAULT_TYPES, None, 2.0)
    assert out["segment_count"] == 1
    seg = out["segments"][0]
    assert seg["start_video_s"] == 17.3 and seg["end_video_s"] == 31.0
    assert out["score"] == {"Harbor City": 2, "Redcliffe": 1}
    assert reel.build(state, state.frame_for_video_s(0), reel.DEFAULT_TYPES, None, 2.0)["segments"] == []


def test_commentary_track_covers_the_clip():
    sys.path.insert(0, str(AGENT))
    try:
        sys.modules.pop("commentary_store", None)
        import commentary_store as cs
    finally:
        sys.path.remove(str(AGENT))
    cues = cs.parse_vtt(DATA / "match_commentary.vtt")
    assert cues and all(0 <= c["start_s"] < c["end_s"] <= 60 for c in cues)
    goal_cue = next(c for c in cues if "bundles it in" in c["text"])
    assert goal_cue["start_s"] < GOAL_VIDEO_S + 2
    assert all("Asante-Brook" not in c["text"] for c in cues if c["start_s"] < GOAL_VIDEO_S - 1)


def test_highlight_stills_exist_for_every_clip_highlight():
    for e in FEED["events"]:
        if e["clip"]:
            for orientation in ("horizontal", "vertical"):
                assert (ROOT / f"frontend/public/sports/highlights/{orientation}/{e['frame_id']}.jpg").exists()


def test_agent_prompt_names_the_loaded_match_and_has_no_hardcoded_endpoints():
    app = (AGENT / "agentcore_app.py").read_text()
    for banned in ("Dortmund", "Bayern", "gateway.bedrock-agentcore", ".amazonaws.com/mcp"):
        assert banned not in app
    assert 'state.teams["home"]["name"]' in app and "_ensure_loaded()" in app
    assert 'os.environ.get("WEBSEARCH_GATEWAY_URL", "")' in app
