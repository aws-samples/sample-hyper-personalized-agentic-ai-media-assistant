"""Helpers for the Tears of Steel notebooks (scene analysis and script).

The notebooks in this folder enable playback and scene-level analysis of
"Tears of Steel" (c) Blender Foundation | mango.blender.org, CC BY 3.0, in a
deployment of this repository. They follow the v1 sample's notebooks
(aws-samples/sample-hyper-personalized-agentic-ai-media-assistant,
backend/tools/bda-video.ipynb and script_analysis.ipynb), pointed at the
resources the CDK stacks create, so nothing is configured by hand.

Pure functions (document building, script cleaning) are unit-tested in
infra/deployment/test_tears_of_steel.py; the AWS helpers are exercised by
running the notebooks.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "infra/deployment"))

TITLE_ID = "tt2285752"
VIDEO_NAME = "tears-of-steel.mp4"  # the scene indexes, the KB metadata and the video mapping all key on this name
VIDEO_KEY = f"clips/movies/{VIDEO_NAME}"
BDA_PREFIX = "bda/tears-of-steel"
SCRIPT_NAME = "tears-of-steel_script.txt"
INDEXES = {"chapters": "chapters", "segments": "segments", "audio": "audio-segments"}

# Title-page highlight cuts (the preview get_video_url picks per profile taste).
# Chosen from the BDA chapter summaries: the cathedral battle (chapters 30-31,
# "four individuals fire their devices", "engages in combat with this entity")
# and the reunion of Thom and Celia (chapter 25: "You broke my heart." "I know."
# "You forgot me on Earth."). About 10 seconds each. Times are seconds in
# the film, which do not change between BDA runs.
HIGHLIGHT_BASE = "tears-of-steel"
HIGHLIGHT_PREFIX = "clips/highlights/vod/"  # = stacks.py HIGHLIGHT_PREFIX
HIGHLIGHT_MAX_S = 15  # keeps each clip a few MB
# Demo resolution, chosen on purpose: horizontal clips are cut at 960x400 (the
# film is 2.4:1) and about 10 s long, so each is only 1-3.5 MB and loads fast in
# the title-page header (rendered about 760 px wide). This is lower than you
# would ship in production; raise it (e.g. 1280 or 1920) for a real catalog.
HIGHLIGHT_WIDTH = 960
HIGHLIGHTS = {
    "action_fan": {"start_s": 469.0, "end_s": 479.0,
                   "description": "An action scene: a giant combat robot charges through a ruined cathedral "
                                  "and a squad of soldiers opens fire on it."},
    "rom_com_lover": {"start_s": 383.0, "end_s": 393.0,
                      "description": "A romantic scene: two former lovers reunite on an Amsterdam street; she "
                                     "tells him he broke her heart, and he admits it."},
}


def highlight_key(variant: str, orientation: str) -> str:
    return f"{HIGHLIGHT_PREFIX}{orientation}/{HIGHLIGHT_BASE}_{variant}.mp4"

# Official Blender download mirrors (listed on https://mango.blender.org/download/).
# The 1080p film ships zipped there; the checksum pins the archive we tested.
DOWNLOADS = [
    "https://ftp.nluug.nl/pub/graphics/blender/demo/movies/ToS/tears_of_steel_1080p.mov.zip",
    "https://ftp.halifax.rwth-aachen.de/blender/demo/movies/ToS/tears_of_steel_1080p.mov.zip",
]
DOWNLOAD_SHA256 = "d87a41de040d3814dbde143e9ab85ef122caf22265f660b0bebf476cd8b357a5"


# --------------------------------------------------------------------------- pure

def chapter_docs(result: dict, video_name: str = VIDEO_NAME) -> list[dict]:
    """One `chapters` document per BDA chapter: its summary plus its transcript,
    as the v1 notebook builds it (chapters numbered from 1)."""
    docs = []
    for idx, ch in enumerate(result.get("chapters", [])):
        transcript = ((ch.get("transcript") or {}).get("representation") or {}).get("text", "")
        summary = ch.get("summary", "") + (f"\n{transcript}" if transcript else "")
        docs.append({"video_name": video_name, "chapter": idx + 1, "summary": summary,
                     "chapter_transcription": [transcript] if transcript else [],
                     "start_timestamp_millis": int(ch["start_timestamp_millis"]),
                     "end_timestamp_millis": int(ch["end_timestamp_millis"])})
    return docs


def audio_docs(result: dict, video_name: str = VIDEO_NAME) -> list[dict]:
    """One `audio-segments` document per spoken segment, numbered within its chapter."""
    docs = []
    for ch in result.get("chapters", []):
        for idx, seg in enumerate(ch.get("audio_segments") or []):
            docs.append({"video_name": video_name, "chapter": int(ch["chapter_index"]) + 1,
                         "chapter_segment": idx + 1, "text": seg.get("text", ""),
                         "start_timestamp_millis": int(seg["start_timestamp_millis"]),
                         "end_timestamp_millis": int(seg["end_timestamp_millis"])})
    return docs


def segment_docs(result: dict, celebrities: dict[int, list[str]] | None = None,
                 video_name: str = VIDEO_NAME) -> list[dict]:
    """One `segments` document per shot, with the celebrities recognized in it
    (`celebrities` maps a shot index to names; empty when detection is skipped)."""
    celebrities = celebrities or {}
    shots = result.get("shots", [])
    docs = []
    for ch in result.get("chapters", []):
        for shot_index in ch.get("shot_indices") or []:
            shot = shots[shot_index]
            docs.append({"video_name": video_name, "chapter": int(ch["chapter_index"]) + 1,
                         "chapter_segment": shot_index + 1,
                         "start_timestamp_millis": int(shot["start_timestamp_millis"]),
                         "end_timestamp_millis": int(shot["end_timestamp_millis"]),
                         "celebrities": sorted(set(celebrities.get(shot_index, [])))})
    return docs


def shot_midpoints(result: dict) -> dict[int, float]:
    """Shot index -> the middle of the shot in seconds (one key frame per shot)."""
    return {i: (s["start_timestamp_millis"] + s["end_timestamp_millis"]) / 2000
            for i, s in enumerate(result.get("shots", []))}


def clean_script(text: str) -> str:
    """The v1 notebook's clean-up: drop blank lines and screenplay CONTINUED markers."""
    return "\n".join(line for line in text.split("\n")
                     if line.strip() and not line.startswith(("CONTINUED:", "(CONTINUED)")))


def kb_metadata(video_name: str = VIDEO_NAME) -> dict:
    """Knowledge Base metadata sidecar: the agent filters retrieval on video_name."""
    return {"metadataAttributes": {"video_name": video_name}}


def delete_query(video_name: str = VIDEO_NAME) -> dict:
    return {"size": 10000, "_source": False, "query": {"term": {"video_name": video_name}}}


def bulk_body(index: str, docs: list[dict], delete_ids=()) -> str:
    """NDJSON _bulk body. The collection assigns document ids, so re-runs delete
    this video's old documents first."""
    lines = [json.dumps({"delete": {"_index": index, "_id": i}}) for i in delete_ids]
    for doc in docs:
        lines += [json.dumps({"index": {"_index": index}}), json.dumps(doc, ensure_ascii=False)]
    return "\n".join(lines) + "\n"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- AWS

class Deployment:
    """A deployed environment: config, boto3 session and stack outputs."""

    def __init__(self, config_path: str, profile: str | None = None):
        import boto3
        from config import load_config, stack_name
        self.cfg = load_config(config_path)
        self.region = self.cfg["region"]
        self.session = boto3.Session(profile_name=profile, region_name=self.region)
        account = self.session.client("sts").get_caller_identity()["Account"]
        if account != self.cfg["account"]:
            raise SystemExit(f"Credentials are for account {account}, the config is for {self.cfg['account']}")
        self.outputs = {}
        for suffix in ("Application", "Frontend"):
            try:
                stack = self.session.client("cloudformation").describe_stacks(
                    StackName=stack_name(self.cfg, suffix))["Stacks"][0]
            except Exception:  # noqa: BLE001 -- the frontend stack is not needed here
                continue
            self.outputs.update({o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])})
        self.media_bucket = self.cfg["mediaBucket"]
        self.prefix = self.cfg["prefix"]
        print(f"Deployment {self.prefix} in {self.region}, content bucket {self.media_bucket}")

    def client(self, name, **kw):
        return self.session.client(name, **kw)

    # ---- search -------------------------------------------------------------
    def search(self):
        from load_catalog import Search
        return Search(self.session, self.outputs["SearchEndpoint"], self.region)

    def replace_documents(self, index: str, docs: list[dict]) -> int:
        """Delete this video's documents from `index`, then index `docs`."""
        search = self.search()
        old = [h["_id"] for h in search.call("POST", f"/{index}/_search", delete_query())["hits"]["hits"]]
        errors = []
        for i in range(0, max(len(old), len(docs)), 200):
            res = search.call("POST", "/_bulk", bulk_body(index, docs[i:i + 200], old[i:i + 200]),
                              content_type="application/x-ndjson")
            errors += [it for it in res.get("items", [])
                       if next(iter(it.values())).get("status", 200) >= 300
                       and next(iter(it.values())).get("status") != 404]
        if errors:
            raise RuntimeError(f"{index}: {len(errors)} bulk error(s), first: {json.dumps(errors[0])[:500]}")
        print(f"{index}: replaced {len(old)} document(s) with {len(docs)}")
        return len(docs)

    # ---- S3 -----------------------------------------------------------------
    def upload(self, path: Path, bucket: str, key: str, content_type: str | None = None,
               metadata: dict | None = None):
        extra = {"ContentType": content_type} if content_type else {}
        if metadata:
            extra["Metadata"] = metadata
        self.client("s3").upload_file(str(path), bucket, key, ExtraArgs=extra or None)
        print(f"uploaded s3://{bucket}/{key}")

    def read_json(self, s3_uri: str) -> dict:
        bucket, key = s3_uri[len("s3://"):].split("/", 1)
        return json.loads(self.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read())

    # ---- Bedrock Data Automation -------------------------------------------
    def bda_project(self, name: str, standard_output: dict) -> str:
        """ARN of a BDA project with this name, created on first use."""
        bda = self.client("bedrock-data-automation")
        # Without the filter only LIVE projects are listed; this one is DEVELOPMENT.
        for page in bda.get_paginator("list_data_automation_projects").paginate(projectStageFilter="ALL"):
            for p in page.get("projects", []):
                if p.get("projectName") == name:
                    return p["projectArn"]
        arn = bda.create_data_automation_project(
            projectName=name, projectDescription="Tears of Steel analysis (hyper-personalized movie assistant)",
            projectStage="DEVELOPMENT", standardOutputConfiguration=standard_output)["projectArn"]
        print("created BDA project", arn)
        return arn

    def run_bda(self, project_arn: str, input_uri: str, output_uri: str, poll_s: int = 15) -> dict:
        """Run one BDA job and return its standard output JSON."""
        runtime = self.client("bedrock-data-automation-runtime")
        account = self.cfg["account"]
        arn = runtime.invoke_data_automation_async(
            inputConfiguration={"s3Uri": input_uri}, outputConfiguration={"s3Uri": output_uri},
            dataAutomationConfiguration={"dataAutomationProjectArn": project_arn, "stage": "DEVELOPMENT"},
            dataAutomationProfileArn=f"arn:aws:bedrock:{self.region}:{account}:data-automation-profile/us.data-automation-v1",
        )["invocationArn"]
        print("BDA job started:", arn)
        while True:
            status = runtime.get_data_automation_status(invocationArn=arn)
            if status["status"] in ("Success", "ServiceError", "ClientError"):
                break
            print(time.strftime("%H:%M:%S"), status["status"], flush=True)
            time.sleep(poll_s)
        if status["status"] != "Success":
            raise RuntimeError(f"BDA job {status['status']}: {status.get('errorMessage')}")
        job = self.read_json(status["outputConfiguration"]["s3Uri"])
        return self.read_json(job["output_metadata"][0]["segment_metadata"][0]["standard_output_path"])

    # ---- playback -----------------------------------------------------------
    def seed_video_mapping(self):
        """Write the Tears of Steel row of seed/title_video_mapping.json (playable title)."""
        from seed_video_mapping import items
        row = next(r for r in items(self.media_bucket) if r["title_id"] == TITLE_ID)
        row.pop("optional", None)
        table = self.session.resource("dynamodb").Table(self.outputs["VideosTableName"])
        table.put_item(Item=row)
        print(f"video mapping: {TITLE_ID} -> {row['video_s3_url']}")

    # ---- Knowledge Base ----------------------------------------------------
    def ingest_knowledge_base(self, poll_s: int = 15) -> dict:
        agent = self.client("bedrock-agent")
        kb_id = self.outputs["KnowledgeBaseId"]
        sources = agent.list_data_sources(knowledgeBaseId=kb_id)["dataSourceSummaries"]
        ds_id = sources[0]["dataSourceId"]
        job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)["ingestionJob"]
        while job["status"] not in ("COMPLETE", "FAILED", "STOPPED"):
            time.sleep(poll_s)
            job = agent.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id,
                                          ingestionJobId=job["ingestionJobId"])["ingestionJob"]
            print(time.strftime("%H:%M:%S"), job["status"], flush=True)
        if job["status"] != "COMPLETE":
            raise RuntimeError(f"ingestion {job['status']}: {job.get('failureReasons')}")
        print("ingestion statistics:", job.get("statistics"))
        return job


# --------------------------------------------------------------------------- media

def ffmpeg() -> str:
    """An ffmpeg binary: the system one, else the one bundled with imageio-ffmpeg."""
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def download_film(workdir: Path) -> Path:
    """Download the 1080p film from a Blender mirror, verify it, return the .mov path."""
    import zipfile
    workdir.mkdir(parents=True, exist_ok=True)
    archive = workdir / "tears_of_steel_1080p.mov.zip"
    if not (archive.exists() and sha256(archive) == DOWNLOAD_SHA256):
        for url in DOWNLOADS:
            try:
                print("downloading", url, "(about 560 MB)", flush=True)
                urllib.request.urlretrieve(url, archive)
                if sha256(archive) == DOWNLOAD_SHA256:
                    break
                print("checksum mismatch, trying the next mirror")
            except Exception as exc:  # noqa: BLE001 -- try the next mirror
                print(f"download failed ({exc}), trying the next mirror")
        else:
            raise RuntimeError("No mirror served the expected file. Download the film from "
                               "https://mango.blender.org/download/ and pass its path instead.")
    with zipfile.ZipFile(archive) as z:
        name = next(n for n in z.namelist() if n.endswith(".mov"))
        z.extract(name, workdir)
    return workdir / name


def to_mp4(src: Path, dst: Path) -> Path:
    """Browser-playable MP4: keep the H.264 video, convert the audio to AAC, moov atom first."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                    "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(dst)], check=True)
    return dst


def cut_highlight(film: Path, start_s: float, end_s: float, orientation: str, dst: Path) -> Path:
    """A highlight clip: horizontal keeps the film's 2.4:1 frame at HIGHLIGHT_WIDTH
    (the title-page header is narrower than that); vertical is a centre crop
    scaled to 1080x1920. H.264 + AAC, moov atom first."""
    if end_s - start_s > HIGHLIGHT_MAX_S:
        raise ValueError(f"highlight longer than {HIGHLIGHT_MAX_S}s")
    vf = f"scale={HIGHLIGHT_WIDTH}:-2" if orientation == "horizontal" else "crop=ih*9/16:ih,scale=1080:1920"
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start_s:.3f}",
                    "-i", str(film), "-t", f"{end_s - start_s:.3f}", "-vf", vf,
                    "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)], check=True)
    return dst


def frame_jpeg(video: Path, seconds: float, width: int = 960) -> bytes:
    """One JPEG frame at `seconds`."""
    out = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}", "-i", str(video),
                          "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "-"],
                         check=True, capture_output=True)
    return out.stdout
