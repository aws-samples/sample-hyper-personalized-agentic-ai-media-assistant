"""Tears of Steel notebooks: document building matches what analysis_agent.py reads."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTEBOOKS = ROOT / "notebooks/tears-of-steel"
sys.path.insert(0, str(NOTEBOOKS))
import tears_of_steel as tos  # noqa: E402

AGENT = (ROOT / "backend/movie-ai-agent-v2/analysis_agent.py").read_text()

BDA = {
    "chapters": [
        {"chapter_index": 0, "summary": "Thom and Celia argue on a bridge.",
         "start_timestamp_millis": 0, "end_timestamp_millis": 30000, "shot_indices": [0, 1],
         "transcript": {"representation": {"text": "You're a jerk, Thom."}},
         "audio_segments": [{"text": "You're a jerk, Thom.", "start_timestamp_millis": 23000, "end_timestamp_millis": 24500}]},
        {"chapter_index": 1, "summary": "Robots attack Amsterdam.",
         "start_timestamp_millis": 30000, "end_timestamp_millis": 60000, "shot_indices": [2],
         "audio_segments": []},
    ],
    "shots": [{"start_timestamp_millis": 0, "end_timestamp_millis": 10000},
              {"start_timestamp_millis": 10000, "end_timestamp_millis": 30000},
              {"start_timestamp_millis": 30000, "end_timestamp_millis": 60000}],
}


def test_chapter_docs_number_from_one_and_append_transcript():
    docs = tos.chapter_docs(BDA)
    assert [d["chapter"] for d in docs] == [1, 2]
    assert docs[0]["summary"] == "Thom and Celia argue on a bridge.\nYou're a jerk, Thom."
    assert docs[1]["summary"] == "Robots attack Amsterdam." and docs[1]["chapter_transcription"] == []
    assert all(d["video_name"] == "tears-of-steel.mp4" for d in docs)


def test_segment_and_audio_docs_carry_chapter_and_timestamps():
    segs = tos.segment_docs(BDA, {1: ["Someone", "Someone"]})
    assert [(s["chapter"], s["chapter_segment"]) for s in segs] == [(1, 1), (1, 2), (2, 3)]
    assert segs[1]["celebrities"] == ["Someone"] and segs[0]["celebrities"] == []
    audio = tos.audio_docs(BDA)
    assert audio == [{"video_name": "tears-of-steel.mp4", "chapter": 1, "chapter_segment": 1,
                      "text": "You're a jerk, Thom.", "start_timestamp_millis": 23000, "end_timestamp_millis": 24500}]
    assert tos.shot_midpoints(BDA) == {0: 5.0, 1: 20.0, 2: 45.0}


def test_documents_have_every_field_the_agent_reads():
    """Each _source list in analysis_agent.py must be served by the documents."""
    needed = set()
    for fields in re.findall(r'"_source": \[([^\]]*)\]', AGENT):
        needed |= set(re.findall(r'"(\w+)"', fields))
    needed -= {"principalCastMembers"}  # catalog index, not a scene index
    have = set(tos.chapter_docs(BDA)[0]) | set(tos.segment_docs(BDA)[0]) | set(tos.audio_docs(BDA)[0])
    assert needed <= have, needed - have


def test_names_match_the_agent_frontend_and_seed():
    assert f'"{tos.INDEXES["chapters"]}"' in AGENT and f'"{tos.INDEXES["audio"]}"' in AGENT
    assert f"{tos.TITLE_ID}: '{tos.VIDEO_NAME}'" in (ROOT / "frontend/src/lib/sceneAgentClient.ts").read_text()
    seed = json.loads((ROOT / "infra/deployment/seed/title_video_mapping.json").read_text())["items"]
    row = next(r for r in seed if r["title_id"] == tos.TITLE_ID)
    assert row["video_name"] == tos.VIDEO_NAME and row["video_s3_url"].endswith("/" + tos.VIDEO_KEY)


def test_bulk_body_deletes_old_copies_then_indexes():
    lines = tos.bulk_body("chapters", [{"a": 1}], ["x"]).splitlines()
    assert json.loads(lines[0]) == {"delete": {"_index": "chapters", "_id": "x"}}
    assert json.loads(lines[1]) == {"index": {"_index": "chapters"}} and json.loads(lines[2]) == {"a": 1}


def test_script_cleanup_and_kb_metadata():
    text = "INT. BRIDGE\n\nCONTINUED:\nTHOM\n(CONTINUED)\nYou're a jerk."
    assert tos.clean_script(text) == "INT. BRIDGE\nTHOM\nYou're a jerk."
    assert tos.kb_metadata() == {"metadataAttributes": {"video_name": "tears-of-steel.mp4"}}
    assert '"key": "video_name"' in AGENT  # the KB retrieval filters on the same attribute


def test_notebooks_are_clean_and_import_the_helpers():
    for nb in sorted(NOTEBOOKS.glob("[0-9]*.ipynb")):
        data = json.loads(nb.read_text())
        code = [c for c in data["cells"] if c["cell_type"] == "code"]
        assert all(not c.get("outputs") and c.get("execution_count") is None for c in code), nb.name
        src = "".join("".join(c["source"]) for c in code)
        assert "import tears_of_steel as tos" in src and "<name>.json" in src, nb.name
        assert not re.search(r"\b\d{12}\b", src), nb.name  # no account ids
