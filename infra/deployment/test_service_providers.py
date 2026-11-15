import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / name / "handler.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_web_grounding_preserves_unique_citation_urls(monkeypatch):
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("MODEL_REGION", "eu-west-1")
    provider = module("web_grounding")
    citation = {"location": {"web": {"url": "https://www.fifa.com"}}}
    client = Mock()
    client.converse.return_value = {"output": {"message": {"content": [
        {"text": "Football facts."}, {"citationsContent": {"citations": [citation, citation]}}]}}}
    monkeypatch.setattr(provider.boto3, "client", lambda *a, **k: client)
    assert provider.handler({"query": "FIFA", "maxResults": 5}, None) == {
        "results": [{"text": "Football facts.", "url": "https://www.fifa.com"}]}


def test_log_provider_cannot_target_original_runtimes(monkeypatch):
    monkeypatch.setenv("RUNTIME_PREFIX", "test_dev")
    provider = module("runtime_logs")
    with pytest.raises(ValueError, match="Only this deployment"):
        provider.handler({"RequestType": "Create", "ResourceProperties": {"RuntimeId": "other_agent-123"}}, None)


def test_log_provider_retains_on_delete(monkeypatch):
    monkeypatch.setenv("RUNTIME_PREFIX", "test_dev")
    provider = module("runtime_logs")
    client = Mock()
    monkeypatch.setattr(provider.boto3, "client", client)
    provider.handler({"RequestType": "Delete", "ResourceProperties": {"RuntimeId": "test_dev_movie-abc123"}}, None)
    client.assert_not_called()
