#!/usr/bin/env python3
"""Cut the key-moment thumbnails for the synthetic match from the broadcast clip.

For every highlight inside the clip (events with a `clip` window in
data/match_events.json) this grabs one still a moment after the event and
writes two small JPGs, named by the event's frame_id:

    frontend/public/sports/highlights/horizontal/<frame_id>.jpg   480x270 (16:9)
    frontend/public/sports/highlights/vertical/<frame_id>.jpg     270x480 (9:16 centre crop)

They ship with the frontend (served from the site root), so no bucket is
involved: the match-state Lambda returns `/sports/highlights/horizontal/<frame_id>.jpg`
as `thumbnail_url`, and the vertical rail/feed use the vertical still.
Highlights before the clip have no picture and keep the gradient poster.

Usage (ffmpeg on PATH or --ffmpeg):
    unzip data/media/football.zip -d /tmp/football
    python cut_highlight_thumbs.py --video /tmp/football/football/horizontal/football.mp4
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FEED = HERE / "data/match_events.json"
OUT = ROOT / "frontend/public/sports/highlights"
# Grab slightly after the event so the still shows the outcome (keeper's dive,
# ball in the net) rather than the instant of the strike.
AFTER_EVENT_S = 0.2


def grab(ffmpeg: str, video: str, at_s: float, vf: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at_s:.2f}",
                    "-i", video, "-frames:v", "1", "-vf", vf, "-q:v", "4", str(out)], check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--video", required=True, help="the 16:9 football clip (any resolution)")
    ap.add_argument("--feed", default=str(FEED))
    ap.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    args = ap.parse_args()

    events = json.loads(Path(args.feed).read_text())["events"]
    moments = [e for e in events if e.get("is_highlight") and e.get("clip")]
    for e in moments:
        at = e["video_s"] + AFTER_EVENT_S
        name = f"{e['frame_id']}.jpg"
        grab(args.ffmpeg, args.video, at, "scale=480:270", OUT / "horizontal" / name)
        grab(args.ffmpeg, args.video, at, "crop=ih*9/16:ih,scale=270:480", OUT / "vertical" / name)
        print(f"{e['minute_label']:<6} {e['highlight_type']:<5} v{e['video_s']:>6} -> {name}")
    print(f"wrote {len(moments) * 2} stills under {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
