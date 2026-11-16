#!/usr/bin/env python3
"""Build the chronological catch-up highlight reel from the match-state engine.

Selects qualifying highlights up to the playhead, merges overlapping/adjacent
clip windows (so the same seconds aren't replayed and near-simultaneous events
like a penalty and its goal become one segment), and emits a reel manifest in
match order. Ordering is always chronological -- never by xG. No LLM.

Clip media URLs (horizontal / vertical / subtitles) are left null; the video
lane (Elemental Inference or a DVR-window cut) fills them in, matched by video_s.

Usage:
    python build_reel.py --table <prefix>-events --frame 176346
    python build_reel.py --table <prefix>-events --frame 140000 --types goal,save,penalty --min-xg 0.1 --json
"""
from __future__ import annotations

import argparse
import json
import os

from match_state import DEFAULT_MATCH_ID, load_dynamodb

# Highlight types eligible for the reel by default (goals, saves, shots, penalties).
DEFAULT_TYPES = {"goal", "save", "shot", "penalty"}


def select(highlights: list[dict], types: set[str], min_xg: float | None,
           chances_only: bool) -> list[dict]:
    out = []
    for h in highlights:
        if h["type"] not in types:
            continue
        # xG floor applies only to plain shots; goals/saves/penalties always pass.
        if h["type"] == "shot":
            if chances_only and h.get("chance_evaluation") not in ("chance", "sitter"):
                # chance_evaluation isn't on the highlight dict yet; see note below
                pass
            if min_xg is not None and (h["xg"] is None or h["xg"] < min_xg):
                continue
        if h["clip"] is None:
            continue
        out.append(h)
    return out


def merge(selected: list[dict], merge_gap_s: float, hz: int = 25) -> list[dict]:
    """Merge segments whose windows overlap or sit within merge_gap_s of each other."""
    segments: list[dict] = []
    for h in selected:
        c = h["clip"]
        moment = {"minute": h["minute"], "type": h["type"], "player": h["player"],
                  "team": h["team"], "xg": h["xg"]}
        if segments and c["in_frame"] <= segments[-1]["end_frame"] + merge_gap_s * hz:
            seg = segments[-1]
            seg["end_frame"] = max(seg["end_frame"], c["out_frame"])
            seg["end_video_s"] = max(seg["end_video_s"], c["out_video_s"])
            seg["moments"].append(moment)
        else:
            segments.append({
                "start_frame": c["in_frame"], "end_frame": c["out_frame"],
                "start_video_s": c["in_video_s"], "end_video_s": c["out_video_s"],
                "moments": [moment],
            })
    return segments


def label(seg: dict) -> str:
    order = {"goal": 0, "penalty": 1, "save": 2, "shot": 3}
    m = sorted(seg["moments"], key=lambda x: order.get(x["type"], 9))[0]
    return f"{m['type'].title()} - {m['player']} ({m['team']})"


def build(state, frame: int, types: set[str], min_xg: float | None,
          merge_gap_s: float) -> dict:
    # `state` is the caller's DynamoDB-backed MatchState, so the reel reads from
    # the same store as /state and the agent.
    snap = state.at(frame)
    selected = select(snap["highlights"], types, min_xg, chances_only=False)
    segments = merge(selected, merge_gap_s)

    for i, seg in enumerate(segments):
        seg["seq"] = i + 1
        seg["duration_s"] = round(seg["end_video_s"] - seg["start_video_s"], 2)
        seg["label"] = label(seg)
        seg["clip_media"] = {"horizontal": None, "vertical": None, "subtitles": None}

    total = round(sum(s["duration_s"] for s in segments), 1)
    return {
        "playhead_frame": frame,
        "period": snap["period"],
        "score": snap["score"],
        "segment_count": len(segments),
        "total_duration_s": total,
        "selection": {"types": sorted(types), "min_xg": min_xg, "merge_gap_s": merge_gap_s},
        "then": "transition_to_live",
        "segments": segments,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", type=int, required=True)
    ap.add_argument("--table", default=os.environ.get("EVENTS_TABLE", "hpm-demo-events"))
    ap.add_argument("--match-id", default=os.environ.get("MATCH_ID", DEFAULT_MATCH_ID))
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION"))
    ap.add_argument("--types", default=",".join(sorted(DEFAULT_TYPES)),
                    help="comma-separated highlight types to include")
    ap.add_argument("--min-xg", type=float, default=None, help="xG floor for plain shots")
    ap.add_argument("--merge-gap-s", type=float, default=2.0,
                    help="merge segments within this many seconds of each other")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    import boto3
    state = load_dynamodb(args.table, args.match_id, boto3.Session(region_name=args.region))
    reel = build(state, args.frame, set(args.types.split(",")), args.min_xg, args.merge_gap_s)

    if args.json:
        print(json.dumps(reel, ensure_ascii=False, indent=2))
        return

    s = reel["score"]; names = list(s)
    print(f"Reel @ frame {reel['playhead_frame']} ({reel['period']}) | "
          f"{names[0]} {s[names[0]]}-{s[names[1]]} {names[1]}")
    print(f"{reel['segment_count']} segments, {reel['total_duration_s']}s total, then -> live\n")
    for seg in reel["segments"]:
        extra = f"  (+{len(seg['moments'])-1} merged)" if len(seg["moments"]) > 1 else ""
        print(f"  {seg['seq']:>2}. {seg['label']:<34} {seg['duration_s']:>5}s  "
              f"v{seg['start_video_s']}-{seg['end_video_s']}{extra}")


if __name__ == "__main__":
    main()
