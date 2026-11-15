"""Highlight selection: each clip's genre sentence lives in S3 metadata, and
get_video_url shows the clip whose sentence is closest (raw cosine) to the
profile's taste vector. The demo ships no highlight clips, so the tests
configure a synthetic one (VOD_HIGHLIGHTS) for a micro-drama."""
import base64
import importlib.util
import io
import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "infra/layers/shared/python"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

TITLE, BASE = "md0000004", "neon-runner"
PREFIX = f"clips/highlights/vod/horizontal/{BASE}_"
ROMCOM_KEY, ACTION_KEY = PREFIX + "rom_com_lover.mp4", PREFIX + "action_fan.mp4"
ROMCOM, ACTION = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]
DESCRIPTIONS = {ROMCOM_KEY: "A romantic comedy scene.", ACTION_KEY: "An action scene."}
VECTORS = {"A romantic comedy scene.": ROMCOM, "An action scene.": ACTION}


def encode(vec):
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode()


class FakeS3:
    def __init__(self, keys, metadata):
        self.keys, self.metadata, self.heads = keys, metadata, []

    def list_objects_v2(self, Bucket, Prefix):
        return {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        return {"Metadata": {"description": self.metadata[Key]} if Key in self.metadata else {}}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed/{Params['Key']}"


class FakeBedrock:
    def __init__(self):
        self.calls = []

    def invoke_model(self, modelId, body, accept, contentType):
        text = json.loads(body)["singleEmbeddingParams"]["text"]["value"]
        self.calls.append(text)
        return {"body": io.BytesIO(json.dumps({"embeddings": [{"embedding": VECTORS[text]}]}).encode())}


class FakeTable:
    def __init__(self, items):
        self.items = items

    def get_item(self, Key):
        return {"Item": self.items.get((Key["user_id"], Key["data_type"]))} if (Key["user_id"], Key["data_type"]) in self.items else {}


@pytest.fixture
def fn(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("PROFILES_TABLE", "profiles")
    monkeypatch.setenv("HIGHLIGHT_BUCKET", "media-bucket")
    monkeypatch.setenv("VOD_HIGHLIGHTS", json.dumps({TITLE: BASE}))
    spec = importlib.util.spec_from_file_location("get_video_url_fn", ROOT / "infra/lambdas/get_video_url/lambda_function.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._bedrock = FakeBedrock()
    module._s3 = FakeS3([ACTION_KEY, ROMCOM_KEY], DESCRIPTIONS)
    module.resolve_profile_pk = lambda event, user_id, profile: profile if profile.startswith("p-") else None
    return module


def set_taste(fn, profile, centroids):
    items = {(profile, "AFFINITIES"): {"centroids": [{"v": encode(v), "w": w} for w, v in centroids]}}
    fn._dynamodb = type("D", (), {"Table": lambda self, name: FakeTable(items)})()


def call(fn, **params):
    resp = fn.lambda_handler({"queryStringParameters": {"title_id": TITLE, "highlight": "1", **params}}, None)
    return resp["statusCode"], json.loads(resp["body"])


def test_romance_taste_gets_the_romcom_clip(fn):
    set_taste(fn, "p-romance", [(1.0, [0.9, 0.3, 0.1])])
    status, body = call(fn, profile_id="p-romance")
    assert status == 200 and body["clip"].endswith("rom_com_lover.mp4")
    assert body["url"] == "https://signed/" + ROMCOM_KEY


def test_action_taste_gets_the_action_clip(fn):
    set_taste(fn, "p-action", [(1.0, [0.2, 0.9, 0.1])])
    assert call(fn, profile_id="p-action")[1]["clip"].endswith("action_fan.mp4")


def test_centroids_are_weight_averaged(fn):
    # A light romance centroid and a heavy action one: action wins on the average.
    set_taste(fn, "p-mixed", [(0.2, ROMCOM), (0.8, ACTION)])
    assert call(fn, profile_id="p-mixed")[1]["clip"].endswith("action_fan.mp4")


def test_single_clip_is_returned_without_scoring(fn):
    fn._s3 = FakeS3([ROMCOM_KEY], {})
    set_taste(fn, "p-kid", [])
    status, body = call(fn, profile_id="p-kid")
    assert status == 200 and body["clip"].endswith("rom_com_lover.mp4")
    assert fn._s3.heads == [] and fn._bedrock.calls == []


def test_no_taste_falls_back_to_first_clip(fn):
    set_taste(fn, "p-new", [])
    status, body = call(fn, profile_id="p-new")
    assert status == 200 and body["clip"].endswith("action_fan.mp4")  # first by key
    assert fn._bedrock.calls == []


def test_undescribed_clip_is_never_preferred(fn):
    fn._s3 = FakeS3([ACTION_KEY, ROMCOM_KEY], {ACTION_KEY: "An action scene."})
    set_taste(fn, "p-romance", [(1.0, ROMCOM)])
    assert call(fn, profile_id="p-romance")[1]["clip"].endswith("action_fan.mp4")


def test_descriptions_are_embedded_once_per_container(fn):
    set_taste(fn, "p-romance", [(1.0, ROMCOM)])
    call(fn, profile_id="p-romance")
    call(fn, profile_id="p-romance")
    assert sorted(fn._bedrock.calls) == sorted(DESCRIPTIONS.values())


def test_profile_outside_the_household_is_refused(fn):
    assert call(fn, profile_id="someone-else")[0] == 403


def test_unknown_title_has_no_highlight(fn):
    resp = fn.lambda_handler({"queryStringParameters": {"title_id": "tt0000001", "highlight": "1"}}, None)
    assert resp["statusCode"] == 404


def test_annotate_writes_metadata_once():
    import annotate_highlights as ah

    class S3:
        class exceptions:
            ClientError = KeyError

        def __init__(self):
            self.meta, self.copies = {}, []

        def head_object(self, Bucket, Key):
            if Key.endswith("missing.mp4"):
                raise KeyError(Key)
            return {"ContentType": "video/mp4", "Metadata": dict(self.meta.get(Key, {}))}

        def copy_object(self, Bucket, Key, CopySource, MetadataDirective, ContentType, Metadata):
            assert CopySource == {"Bucket": Bucket, "Key": Key} and MetadataDirective == "REPLACE"
            assert ContentType == "video/mp4"
            self.meta[Key] = Metadata
            self.copies.append(Key)

    s3 = S3()
    first = ah.annotate(s3, "bucket", {"clip.mp4": "A scene.", "missing.mp4": "x"})
    assert [a for _, a in first] == ["written", "written", "missing", "missing"]
    second = ah.annotate(s3, "bucket", {"clip.mp4": "A scene."})
    assert [a for _, a in second] == ["unchanged", "unchanged"] and len(s3.copies) == 2


def test_shipped_highlights_are_tears_of_steel_and_consistent():
    """The seed sentences, the notebook's cuts, the stack setting and the
    frontend list all name the same Tears of Steel clips (HIGHLIGHT_MAX_S at most)."""
    import annotate_highlights as ah
    sys.path.insert(0, str(ROOT / "notebooks/tears-of-steel"))
    import tears_of_steel as tos
    expected = {f"{tos.HIGHLIGHT_BASE}_{v}.mp4": h["description"] for v, h in tos.HIGHLIGHTS.items()}
    assert ah.load_descriptions() == expected
    assert set(tos.HIGHLIGHTS) == {"action_fan", "rom_com_lover"}
    assert all(0 < h["end_s"] - h["start_s"] <= tos.HIGHLIGHT_MAX_S for h in tos.HIGHLIGHTS.values())
    assert tos.HIGHLIGHT_PREFIX == ah.PREFIX
    assert '{"tt2285752": "tears-of-steel"}' in (ROOT / "infra/deployment/stacks.py").read_text()
    assert "new Set<string>(['tt2285752'])" in (ROOT / "frontend/src/lib/vodHighlights.ts").read_text()


def test_no_highlights_without_configuration(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.delenv("VOD_HIGHLIGHTS", raising=False)
    spec = importlib.util.spec_from_file_location("get_video_url_bare", ROOT / "infra/lambdas/get_video_url/lambda_function.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.VOD_HIGHLIGHTS == {} and module.HIGHLIGHT_PREFIX == "clips/highlights/vod/"
    resp = module.lambda_handler({"queryStringParameters": {"title_id": TITLE, "highlight": "1"}}, None)
    assert resp["statusCode"] == 404
