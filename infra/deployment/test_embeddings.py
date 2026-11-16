"""Exercise both supported embedding wire protocols without making AWS calls."""
import ast
import io
import json
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]

def load_embed(model, response):
    tree = ast.parse((ROOT / "backend/movie-ai-agent-v2/retrieval.py").read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "embed_query")
    client = Mock()
    client.invoke_model.return_value = {"body": io.BytesIO(json.dumps(response).encode())}
    scope = {"json": json, "EMBEDDING_MODEL_ID": model, "EMBEDDING_DIMENSION": 1024,
             "EMBED_QUERY_PURPOSE": "TEXT_RETRIEVAL", "_get_bedrock": lambda: client}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "embed_query", "exec"), scope)
    return scope["embed_query"], client

def test_titan_query_protocol():
    embed, client = load_embed("amazon.titan-embed-text-v2:0", {"embedding": [0.1, 0.2]})
    assert embed("space adventures") == [0.1, 0.2]
    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    assert body == {"inputText": "space adventures", "dimensions": 1024, "normalize": True}

def test_existing_nova_query_protocol():
    embed, client = load_embed("amazon.nova-2-multimodal-embeddings-v1:0", {"embeddings": [{"embedding": [0.3]}]})
    assert embed("space adventures") == [0.3]
    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    assert body["taskType"] == "SINGLE_EMBEDDING"
    assert body["singleEmbeddingParams"]["embeddingPurpose"] == "TEXT_RETRIEVAL"
    assert body["singleEmbeddingParams"]["embeddingDimension"] == 1024
