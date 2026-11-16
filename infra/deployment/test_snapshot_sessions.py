"""Voice fallbacks must not reuse state captured by a Runtime V2 snapshot."""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

spec = importlib.util.spec_from_file_location("voice_bridge_test",
    Path(__file__).resolve().parents[2] / "backend/movie-voice-agent/movie_agent.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


def test_fallback_session_is_fresh_for_each_direct_call(monkeypatch):
    monkeypatch.setattr(bridge, "_config", lambda: {
        "arn": "test", "region": "eu-west-1", "default_profile": "test", "timeout": 1})
    http = MagicMock()
    http.return_value.__enter__.return_value = []
    monkeypatch.setattr(bridge.urllib.request, "urlopen", http)
    for _ in range(2):
        bridge._invoke_agent("recommendation", "test", None, "test-token")
    requests = [call.args[0] for call in http.call_args_list]
    ids = [json.loads(request.data)["session_id"] for request in requests]
    assert ids[0] != ids[1]
    assert all(len(value) >= 33 for value in ids)
    for request, value in zip(requests, ids):
        assert request.get_header("X-amzn-bedrock-agentcore-runtime-session-id") == value


def test_explicit_connection_session_preserves_continuity(monkeypatch):
    monkeypatch.setattr(bridge, "_config", lambda: {
        "arn": "test", "region": "eu-west-1", "default_profile": "test", "timeout": 1})
    http = MagicMock()
    http.return_value.__enter__.return_value = []
    monkeypatch.setattr(bridge.urllib.request, "urlopen", http)
    connection_id = bridge.new_session_id()
    for _ in range(2):
        bridge._invoke_agent("recommendation", "test", None, "test-token", session_id=connection_id)
    assert all(json.loads(call.args[0].data)["session_id"] == connection_id for call in http.call_args_list)
