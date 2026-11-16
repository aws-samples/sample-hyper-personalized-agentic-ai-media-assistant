import importlib.util
from pathlib import Path
from unittest.mock import Mock, MagicMock
from urllib.error import HTTPError

import pytest

spec = importlib.util.spec_from_file_location("index_handler", Path(__file__).parent / "index_provider/handler.py")
handler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler)


def test_delete_retains_index(monkeypatch):
    monkeypatch.setenv("COLLECTION_ENDPOINT", "https://new-collection.example")
    client = Mock()
    monkeypatch.setattr(handler, "request", client)
    handler.handler({"RequestType": "Delete", "ResourceProperties": {"IndexName": "catalog"}}, None)
    client.assert_not_called()


def test_create_retries_access_propagation_and_preserves_mapping(monkeypatch):
    monkeypatch.setenv("COLLECTION_ENDPOINT", "https://new-collection.example")
    monkeypatch.setattr(handler.time, "sleep", lambda _: None)
    client = Mock(side_effect=[HTTPError("", 403, "propagating", {}, None),
        HTTPError("", 404, "missing", {}, None), {}, {}])
    monkeypatch.setattr(handler, "request", client)
    fields = {"rating": {"type": "float"}, "timestamp": {"type": "long"}}
    handler.handler({"RequestType": "Create", "ResourceProperties": {"IndexName": "catalog", "Mappings": fields}}, None)
    assert client.call_args_list[2].args == ("PUT", "/catalog", {
        "settings": {"index.knn": True}, "mappings": {"properties": fields}})


def test_rejects_unexpected_index(monkeypatch):
    with pytest.raises(ValueError, match="Unexpected index"):
        handler.handler({"RequestType": "Create", "ResourceProperties": {"IndexName": "old-index"}}, None)


def test_aoss_payload_hash_is_present_in_signature(monkeypatch):
    from botocore.credentials import Credentials
    import hashlib
    monkeypatch.setenv("COLLECTION_ENDPOINT", "https://new-collection.example")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    session = Mock()
    session.get_credentials.return_value = Credentials("test", "test")
    monkeypatch.setattr(handler.boto3, "Session", lambda: session)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b"{}"
    http = Mock(return_value=response)
    monkeypatch.setattr(handler.urllib.request, "urlopen", http)
    handler.request("PUT", "/catalog", {"settings": {"index.knn": True}})
    request = http.call_args.args[0]
    assert request.get_header("X-amz-content-sha256") == hashlib.sha256(request.data).hexdigest()
    assert "x-amz-content-sha256" in request.get_header("Authorization")
