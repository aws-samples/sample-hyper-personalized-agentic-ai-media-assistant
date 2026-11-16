#!/usr/bin/env python3
"""Deterministic live match-state engine over the normalized match event feed.

Given a playhead (frame_id), computes the match state using only events at or
before that frame -- so it never reveals the future (no spoilers). This is the
factual backbone the agent's tools read from; the LLM never counts anything.

Everything is read from the DynamoDB event store: the match structure from the
match's META item, and the events up to the playhead per query. Nothing is read
from the feed files at runtime; load_events_dynamodb.py loads them offline.

Usage:
    python match_state.py --table <prefix>-events --frame 130000
    python match_state.py --table <prefix>-events --frame 130000 --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime

ON_TARGET = {"SuccessfulShot", "SavedShot"}
DEFAULT_MATCH_ID = "SYN-MAT-0001"


def iso_to_ms(s: str) -> int:
    """Parse an ISO-8601 UTC timestamp to epoch milliseconds.

    Tolerant of trailing 'Z' and variable-length fractional seconds
    (the feed mixes e.g. '.32000Z' and '.167000+00:00')."""
    s = s.strip().replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)", s)
    base, frac, tz = m.group(1), m.group(2) or ".0", (m.group(3) or "+00:00")
    frac = (frac + "000000")[:7]  # '.' + exactly 6 digits
    return int(datetime.fromisoformat(base + frac + tz).timestamp() * 1000)


class MatchState:
    def __init__(self, meta: dict, store):
        """`meta` is the match's META item (match, teams, clock, players).
        `store` exposes events_upto(bound_ms) -> list[event dict]."""
        self.teams = meta["teams"]
        self.match = meta["match"]
        self.halves = meta["clock"]["halves"]
        self.hz = meta["clock"]["hz"]
        self.store = store
        # Precompute half boundary timestamps (ms) for frame -> now_ms mapping.
        self._h1_start_ms = iso_to_ms(self.halves["firstHalf"]["first_timestamp_utc"])
        self._h1_end_ms = iso_to_ms(self.halves["firstHalf"]["last_timestamp_utc"])
        self._h2_start_ms = iso_to_ms(self.halves["secondHalf"]["first_timestamp_utc"])
        self._h2_end_ms = iso_to_ms(self.halves["secondHalf"]["last_timestamp_utc"])
        # Half a frame in ms: the store bound is ms(frame) + this, so an event
        # whose true timestamp rounds to frame F is included exactly when the
        # playhead reaches F -- byte-identical to the frame_id <= now filter.
        self._half_frame_ms = int(round(1000 / (2 * self.hz)))
        # side -> display name
        self.side_name = {
            "home": self.teams["home"]["short_name"],
            "guest": self.teams["guest"]["short_name"],
        }
        # player_id -> optional face id, used to resolve a face-circle avatar
        # (<face_id>-circle.png in the deployer's own faces bucket). The demo
        # ships no faces, so every player falls back to a team-color initial.
        self.face_by_player = {
            p["player_id"]: str(p["face_id"])
            for p in meta.get("players", [])
            if p.get("player_id") and p.get("face_id")
        }

    def _now_ms(self, frame_id: int) -> int:
        """Wall-clock (epoch ms) of the playhead frame -- the query bound for the
        event store. Monotonic across the match; clamps across the half-time gap."""
        h1, h2 = self.halves["firstHalf"], self.halves["secondHalf"]
        if frame_id <= h1["last_frame_id"]:
            # pre-match & first half (extrapolates before kickoff -> no events)
            return int(self._h1_start_ms + (frame_id - h1["first_frame_id"]) / self.hz * 1000)
        if frame_id < h2["first_frame_id"]:
            return self._h1_end_ms  # half-time: only first-half events are "past"
        if frame_id <= h2["last_frame_id"]:
            return int(self._h2_start_ms + (frame_id - h2["first_frame_id"]) / self.hz * 1000)
        return self._h2_end_ms  # full time

    def _past(self, frame_id: int) -> list:
        """Events at or before the playhead, read from the store."""
        return self.store.events_upto(self._now_ms(frame_id) + self._half_frame_ms)

    def _period(self, frame_id: int) -> str:
        for name, h in self.halves.items():
            if h["first_frame_id"] <= frame_id <= h["last_frame_id"]:
                return name
        # between halves or after full time
        if frame_id < self.halves["firstHalf"]["first_frame_id"]:
            return "preMatch"
        if frame_id > self.halves["secondHalf"]["last_frame_id"]:
            return "fullTime"
        return "halfTime"

    def clock(self, frame_id: int) -> dict:
        """Current match clock for a playhead frame: period + minute label.

        Mirrors the UI scoreboard clock so the agent's sense of "now" matches
        what's on screen. Spoiler-safe: derived purely from the frame position.
        """
        period = self._period(frame_id)
        if period == "firstHalf":
            sec = (frame_id - self.halves["firstHalf"]["first_frame_id"]) / self.hz
            m = int(sec // 60) + 1
            return {"period": period, "minute": m, "label": f"{m}'"}
        if period == "secondHalf":
            sec = (frame_id - self.halves["secondHalf"]["first_frame_id"]) / self.hz
            m = 45 + int(sec // 60) + 1
            return {"period": period, "minute": m, "label": f"{m}'"}
        if period == "halfTime":
            return {"period": period, "minute": 45, "label": "half-time"}
        if period == "preMatch":
            return {"period": period, "minute": 0, "label": "pre-match (kickoff not yet)"}
        return {"period": period, "minute": 90, "label": "full-time"}

    def frame_for_video_s(self, video_s: float) -> int:
        """Convert a broadcast-reel second (the UI video playhead) to a frame_id."""
        best = None
        for h in self.halves.values():
            first_f, anchor = h["first_frame_id"], h["anchor_video_s"]
            frame = round(first_f + (video_s - anchor) * self.hz)
            # clamp into this half; pick the half whose window contains video_s
            lo, hi = h["first_frame_id"], h["last_frame_id"]
            if lo <= frame <= hi:
                return frame
            # remember nearest edge as fallback (e.g., half-time gap)
            cand = min(max(frame, lo), hi)
            if best is None or abs(frame - cand) < best[1]:
                best = (cand, abs(frame - cand))
        return best[0] if best else 0

    def player_stats(self, frame_id: int, query: str) -> dict:
        """Per-player tallies up to the playhead. Matches player name case-insensitively."""
        q = query.strip().lower()
        past = self._past(frame_id)

        def hit(name: str | None) -> bool:
            return bool(name) and q in name.lower()

        matched_name = None
        stats = {"goals": 0, "shots": 0, "assists": 0, "fouls_committed": 0,
                 "fouls_suffered": 0, "offsides": 0, "cards": 0}
        for e in past:
            rn = e["resolved_names"]
            if e["type"] == "ShotAtGoal" and hit(e["player_name"]):
                matched_name = matched_name or e["player_name"]
                stats["shots"] += 1
                if e["subtype"] == "SuccessfulShot":
                    stats["goals"] += 1
            if hit(rn.get("AssistPlayerResolvedName")):
                stats["assists"] += 1
            if e["type"] == "Foul":
                if hit(rn.get("FoulerResolvedName")):
                    matched_name = matched_name or rn["FoulerResolvedName"]
                    stats["fouls_committed"] += 1
                if hit(rn.get("FouledResolvedName")):
                    stats["fouls_suffered"] += 1
            if e["type"] == "Offside" and hit(e["player_name"]):
                stats["offsides"] += 1
            if e["type"] == "Card" and hit(e["player_name"]):
                stats["cards"] += 1

        return {"player": matched_name or query, "matched": matched_name is not None, **stats}

    def _notable_players(self, past: list) -> list:
        """Per-player cards for players with a goal, shot, or card up to the
        playhead. Ordered goals -> shots -> cards. `team` is 'home'/'away' to
        match the UI; `face_id` (may be None) drives the face avatar."""
        agg: dict = {}

        def bucket(e):
            pid = e.get("player_id")
            key = pid or e.get("player_name")
            if not key:
                return None
            a = agg.get(key)
            if a is None:
                side = e.get("team_side")
                a = agg[key] = {
                    "player_id": pid,
                    "name": e.get("player_name"),
                    "team": "home" if side == "home" else "away",
                    "goals": 0, "shots": 0, "cards": 0,
                    "on_target": 0, "assists": 0, "fouls": 0, "offsides": 0,
                    "face_id": self.face_by_player.get(pid),
                }
            return a

        for e in past:
            t = e["type"]
            if t == "ShotAtGoal":
                a = bucket(e)
                if a:
                    a["shots"] += 1
                    if e["subtype"] in ON_TARGET:
                        a["on_target"] += 1
                    if e["subtype"] == "SuccessfulShot":
                        a["goals"] += 1
            elif t == "Card":
                a = bucket(e)
                if a:
                    a["cards"] += 1

        # Enrich the listed players with a few more stats, matched by name (the
        # actor for assists/fouls lives in resolved_names, not player_id, so a
        # name pass is simplest). Only players already surfaced get enriched.
        by_name = {a["name"].lower(): a for a in agg.values() if a.get("name")}

        def add(name, field):
            a = by_name.get((name or "").lower())
            if a:
                a[field] += 1

        for e in past:
            rn = e.get("resolved_names") or {}
            t = e["type"]
            if rn.get("AssistPlayerResolvedName"):
                add(rn["AssistPlayerResolvedName"], "assists")
            if t == "Foul" and rn.get("FoulerResolvedName"):
                add(rn["FoulerResolvedName"], "fouls")
            elif t == "Offside":
                add(e.get("player_name"), "offsides")

        players = list(agg.values())
        for a in players:
            parts = []
            if a["goals"]:
                parts.append(f"{a['goals']} Goal" + ("s" if a["goals"] > 1 else ""))
            if a["shots"]:
                parts.append(f"{a['shots']} Shot" + ("s" if a["shots"] > 1 else ""))
            if a["cards"]:
                parts.append(f"{a['cards']} Card" + ("s" if a["cards"] > 1 else ""))
            a["line"] = " · ".join(parts)
        players.sort(key=lambda x: (-x["goals"], -x["shots"], -x["cards"], x["name"] or ""))
        return players

    def _minute_clock(self, e: dict) -> str:
        """Minute label like "26'" from the event's minute/minute_label."""
        m = e.get("minute")
        if isinstance(m, int):
            return f"{m}'"
        lbl = (e.get("minute_label") or "").rstrip(".")
        return f"{lbl}'" if lbl else ""

    def _pbp_row(self, e: dict):
        """One play-by-play row: {clock, label, detail, team, kind}. Returns None
        for event types we don't surface."""
        t = e["type"]
        st = e.get("subtype")
        side = e.get("team_side")
        team = "home" if side == "home" else "away"
        team_name = self.side_name.get(side, "")
        player = e.get("player_name") or ""
        rn = e.get("resolved_names") or {}
        clock = self._minute_clock(e)

        if t == "KickOff":
            return {"clock": clock, "label": "Kick-off", "detail": "Match underway",
                    "team": team, "kind": "start"}
        if t == "ShotAtGoal":
            if st == "SuccessfulShot":
                return {"clock": clock, "label": f"GOAL — {team_name}",
                        "detail": f"{player} scores", "team": team, "kind": "goal"}
            if st == "SavedShot":
                return {"clock": clock, "label": "Shot saved",
                        "detail": f"{player}'s effort kept out", "team": team, "kind": "save"}
            label = "Shot blocked" if st == "BlockedShot" else "Shot off target"
            return {"clock": clock, "label": label, "detail": player,
                    "team": team, "kind": "shot"}
        if t == "Foul":
            fouler = rn.get("FoulerResolvedName") or player
            fouled = rn.get("FouledResolvedName")
            detail = f"{fouler} on {fouled}" if fouled else fouler
            return {"clock": clock, "label": "Foul", "detail": detail,
                    "team": team, "kind": "foul"}
        if t == "Card":
            color = (st or "").capitalize()
            return {"clock": clock, "label": f"{color} card".strip(), "detail": player,
                    "team": team, "kind": "card"}
        if t == "CornerKick":
            return {"clock": clock, "label": "Corner", "detail": team_name,
                    "team": team, "kind": "corner"}
        if t == "Offside":
            return {"clock": clock, "label": "Offside", "detail": player,
                    "team": team, "kind": "offside"}
        if t == "Penalty":
            return {"clock": clock, "label": "Penalty", "detail": team_name,
                    "team": team, "kind": "goal"}
        if t == "Substitution":
            pin = rn.get("PlayerInResolvedName")
            pout = rn.get("PlayerOutResolvedName")
            detail = f"{pin} on for {pout}" if pin and pout else (pin or pout or "")
            return {"clock": clock, "label": "Substitution", "detail": detail,
                    "team": team, "kind": "sub"}
        return None

    def _play_by_play(self, past: list) -> list:
        """Full chronological event feed up to the playhead, newest first."""
        rows = [r for r in (self._pbp_row(e) for e in past) if r]
        rows.reverse()  # newest at the top, like a live ticker
        return rows

    def at(self, frame_id: int, recent_n: int = 5) -> dict:
        past = self._past(frame_id)

        def blank():
            return {"goals": 0, "shots": 0, "on_target": 0, "saves": 0,
                    "corners": 0, "fouls": 0, "offsides": 0}

        stats = {"home": blank(), "guest": blank()}
        highlights, subs, cards = [], [], []

        for e in past:
            side = e["team_side"]
            t = e["type"]
            if side in stats:
                other = "guest" if side == "home" else "home"
                if t == "ShotAtGoal":
                    stats[side]["shots"] += 1
                    if e["subtype"] in ON_TARGET:
                        stats[side]["on_target"] += 1
                    if e["subtype"] == "SuccessfulShot":
                        stats[side]["goals"] += 1
                    if e["subtype"] == "SavedShot":  # credited to the DEFENDING keeper's team
                        stats[other]["saves"] += 1
                elif t == "CornerKick":
                    stats[side]["corners"] += 1
                elif t == "Foul":
                    stats[side]["fouls"] += 1
                elif t == "Offside":
                    stats[side]["offsides"] += 1

            if e["is_highlight"]:
                highlights.append({
                    "minute": e["minute_label"], "type": e["highlight_type"],
                    "player": e["player_name"], "team": self.side_name.get(side),
                    "xg": e["xg"], "frame_id": e["frame_id"], "video_s": e["video_s"],
                    "clip": e["clip"],
                })
            if t == "Substitution":
                subs.append({"minute": e["minute_label"], "team": self.side_name.get(side),
                             "in": e["resolved_names"].get("PlayerInResolvedName"),
                             "out": e["resolved_names"].get("PlayerOutResolvedName")})
            if t == "Card":
                cards.append({"minute": e["minute_label"], "player": e["player_name"],
                              "team": self.side_name.get(side), "color": e["subtype"]})

        return {
            "frame_id": frame_id,
            "period": self._period(frame_id),
            "score": {
                self.side_name["home"]: stats["home"]["goals"],
                self.side_name["guest"]: stats["guest"]["goals"],
            },
            "team_stats": {self.side_name[s]: stats[s] for s in ("home", "guest")},
            "recent_events": [
                {"minute": e["minute_label"], "type": e["type"], "subtype": e["subtype"],
                 "player": e["player_name"], "team": self.side_name.get(e["team_side"])}
                for e in past[-recent_n:]
            ],
            # Notable player cards + full chronological play-by-play, both bounded
            # to the playhead. `players[].face_url` is filled in by the Lambda
            # (presigned) since the engine has no S3 client.
            "players": self._notable_players(past),
            "play_by_play": self._play_by_play(past),
            "highlights": highlights,
            "substitutions": subs,
            "cards": cards,
        }


def load(store) -> MatchState:
    """MatchState over a loaded event store (structure from its META item)."""
    return MatchState(store.meta(), store)


def load_dynamodb(table: str, match_id: str, session) -> MatchState:
    from event_store import DynamoEventStore
    return load(DynamoEventStore(table, match_id, session))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", type=int, required=True)
    ap.add_argument("--table", default=os.environ.get("EVENTS_TABLE", "hpm-demo-events"))
    ap.add_argument("--match-id", default=os.environ.get("MATCH_ID", DEFAULT_MATCH_ID))
    ap.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION"))
    ap.add_argument("--json", action="store_true", help="print full snapshot as JSON")
    args = ap.parse_args()

    import boto3
    state = load_dynamodb(args.table, args.match_id, boto3.Session(region_name=args.region))
    snap = state.at(args.frame)

    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return

    s = snap["score"]
    names = list(s)
    print(f"Frame {snap['frame_id']} | {snap['period']}")
    print(f"Score: {names[0]} {s[names[0]]} - {s[names[1]]} {names[1]}")
    print(f"\nStats: {'':<10}{names[0]:>10}{names[1]:>10}")
    for k in ("shots", "on_target", "saves", "corners", "fouls", "offsides"):
        a, b = snap["team_stats"][names[0]][k], snap["team_stats"][names[1]][k]
        print(f"  {k:<10}{a:>10}{b:>10}")
    print(f"\nHighlights so far ({len(snap['highlights'])}):")
    for h in snap["highlights"]:
        tag = h["type"].upper()
        print(f"  {h['minute']:<7} {tag:<8} {h['player'] or '-':<20} ({h['team']})")


if __name__ == "__main__":
    main()
