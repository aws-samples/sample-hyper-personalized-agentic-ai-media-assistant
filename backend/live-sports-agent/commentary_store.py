#!/usr/bin/env python3
"""Commentary (WebVTT) store for the live match assistant.

The broadcast commentary arrives as WebVTT cues -- exactly how a live caption
track comes off AWS Elemental Live (the demo ships a synthetic caption track). Each cue carries a start/end time on the
*media timeline* (seconds from the start of the broadcast reel), which is the
same clock as the request's `video_s` playhead. So, unlike the event feed
(which carries real UTC timestamps and is keyed by wall-clock), commentary is
keyed by media-time milliseconds and read bounded by the playhead:

    window(playhead_ms, lookback_ms)
        -> Query(match_id = MID AND commentary_sk BETWEEN lo AND playhead)

This is spoiler-safe: only cues whose start is at or before the current playhead
are ever returned, and each cue is bounded individually, so there is no leakage
of words spoken after "now" (word-level cues are joined at read time).

Sort key `commentary_sk` is a composite "<15-digit media-ms>#<4-digit seq>"
string. Zero-padding makes lexicographic order equal numeric time order, so a
simple range query works; `seq` disambiguates cues that share a millisecond.
"""
from __future__ import annotations

import re
from pathlib import Path

from boto3.dynamodb.conditions import Key

_ARROW = "-->"


def sk_for(start_ms: int, seq: int) -> str:
    """Composite, time-ordered, unique sort key for a cue."""
    return f"{int(start_ms):015d}#{seq:04d}"


def sk_lo(bound_ms: int) -> str:
    """Inclusive lower bound: any cue with start_ms >= bound_ms sorts at/above
    the bare padded ms string."""
    return f"{int(bound_ms):015d}"


def sk_hi(bound_ms: int) -> str:
    """Inclusive upper bound: the '#9999' suffix sorts above every real
    '<ms>#<seq>' sharing the same ms, so a cue starting exactly at the playhead
    is included."""
    return f"{int(bound_ms):015d}#9999"


def _ts_to_ms(ts: str) -> int:
    """Parse a WebVTT timestamp ('HH:MM:SS.mmm' or 'MM:SS.mmm') to milliseconds."""
    ts = ts.strip()
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, rest = parts
    elif len(parts) == 2:
        h, m, rest = "0", parts[0], parts[1]
    else:
        raise ValueError(f"bad VTT timestamp: {ts!r}")
    sec, _, frac = rest.partition(".")
    frac = (frac + "000")[:3]
    return int(h) * 3600000 + int(m) * 60000 + int(sec) * 1000 + int(frac)


def parse_vtt(path: Path | str, offset_ms: int = 0) -> list[dict]:
    """Parse a WebVTT file into time-ordered cues.

    Returns dicts: {seq, start_ms, end_ms, start_s, end_s, text}. `offset_ms`
    shifts every cue onto the `video_s` timeline if the caption track and the
    broadcast reel do not share an origin (default 0 -- they do here)."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    cues: list[dict] = []
    block: list[str] = []

    def flush(block: list[str]) -> None:
        arrow = next((k for k, ln in enumerate(block) if _ARROW in ln), None)
        if arrow is None:
            return
        start_raw, _, end_raw = block[arrow].partition(_ARROW)
        # cue settings (e.g. "align:start") follow the end time on the same line
        end_raw = end_raw.strip().split(" ")[0]
        text = " ".join(ln.strip() for ln in block[arrow + 1:] if ln.strip())
        if not text:
            return
        start_ms = _ts_to_ms(start_raw) + offset_ms
        end_ms = _ts_to_ms(end_raw) + offset_ms
        seq = len(cues)
        cues.append({
            "seq": seq,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_s": round(start_ms / 1000, 3),
            "end_s": round(end_ms / 1000, 3),
            "text": text,
        })

    for raw in list(lines) + [""]:
        line = raw.strip()
        if line == "":
            if block:
                flush(block)
                block = []
            continue
        if line.upper() == "WEBVTT" or line.startswith("NOTE"):
            block = []
            continue
        block.append(raw)
    return cues


def coalesce(cues: list[dict]) -> str:
    """Join word/phrase-level cues into a single readable passage.

    Collapses the tiny WebVTT fragments into flowing text; spoiler-safe because
    the caller has already bounded `cues` by the playhead."""
    text = " ".join(c["text"].strip() for c in cues if c.get("text"))
    return re.sub(r"\s+", " ", text).strip()


class DynamoCommentaryStore:
    """Reads coalesced commentary from the DynamoDB commentary table, bounded by
    the playhead (spoiler-safe) and by a lookback window (cheap, recent color)."""

    def __init__(self, table_name: str, match_id: str, session):
        self.table = session.resource("dynamodb").Table(table_name)
        self.match_id = match_id

    def window(self, playhead_ms: int, lookback_ms: int) -> list[dict]:
        """Cues with (playhead_ms - lookback_ms) <= start_ms <= playhead_ms,
        in chronological order."""
        lo = max(0, int(playhead_ms) - int(lookback_ms))
        cond = (Key("match_id").eq(self.match_id)
                & Key("commentary_sk").between(sk_lo(lo), sk_hi(int(playhead_ms))))
        items: list[dict] = []
        resp = self.table.query(KeyConditionExpression=cond)
        items.extend(resp["Items"])
        while "LastEvaluatedKey" in resp:
            resp = self.table.query(KeyConditionExpression=cond,
                                    ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp["Items"])
        # top-level attrs are enough for serving; no per-cue payload needed
        return [{"start_s": float(it.get("start_s", 0)),
                 "end_s": float(it.get("end_s", 0)),
                 "text": it.get("text", "")} for it in items]

    def count(self) -> int:
        cond = Key("match_id").eq(self.match_id)
        resp = self.table.query(KeyConditionExpression=cond, Select="COUNT")
        total = resp.get("Count", 0)
        while "LastEvaluatedKey" in resp:
            resp = self.table.query(KeyConditionExpression=cond, Select="COUNT",
                                    ExclusiveStartKey=resp["LastEvaluatedKey"])
            total += resp.get("Count", 0)
        return total
