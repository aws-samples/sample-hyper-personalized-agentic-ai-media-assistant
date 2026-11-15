#!/usr/bin/env python3
"""Build the synthetic demo match: SYN-MAT-0001, Harbor City FC vs Redcliffe Athletic.

Deterministic generator (no randomness, no network) for the data the live
sports demo runs on. The broadcast is the 60 s football clip shipped in
data/media/football.zip; this script writes a match "around" it:

    data/match_events.json      normalized event feed      -> events table
    data/match_meta.json        clock anchors, clubs, roster -> events table META item
    data/match_commentary.vtt   English play-by-play captions -> commentary table

Everything here is FICTIONAL: teams, players, competition and venue were made
up for this demo. Shirt numbers follow what is visible in the clip.

Timeline
--------
The clip is the broadcast of a late stretch of the second half, in real time
(1 video second = 1 match second, 25 frames per second):

    video_s = 0      second half 32:00 (the 78th minute)
    video_s = 24.00  Tobiah Lark (#7) shoots, keeper Marek Zielonka saves
    video_s = 25.84  Kofi Asante-Brook (#11) scores the rebound -> 2-1
    video_s ~ 38-51  net-camera replay of the goal (not a new event)
    video_s = 60     end of clip, 79th minute

Everything before video_s = 0 already happened when the broadcast starts
(kick-off, a 1-1 first hour, cards, substitutions...), so those events have
NEGATIVE video_s and no clip window. They give the match assistant real,
spoiler-safe context from the first second. The two in-clip highlights carry a
`clip` window (the build-up to the goal); pre-clip highlights carry clip=null.

The feed stops at the end of the clip, like a live feed read at that moment:
there are no events after video_s = 60 (no final whistle, no final score), so
no route can reveal how the match ends. The second half's clock window is the
planned 45 minutes + 4 minutes stoppage.

Usage:
    python build_synthetic_match.py            # (re)write the three data files
    python build_synthetic_match.py --check    # exit 1 if they are stale
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EVENTS_FILE = DATA / "match_events.json"
META_FILE = DATA / "match_meta.json"
VTT_FILE = DATA / "match_commentary.vtt"

MATCH_ID = "SYN-MAT-0001"
HZ = 25
CLIP_DURATION_S = 60.0

# --- clock -------------------------------------------------------------------
KICKOFF_UTC = datetime(2026, 5, 16, 15, 0, 0, tzinfo=timezone.utc)
FIRST_HALF_LEN_S = (45 + 2) * 60           # 2 minutes stoppage
SECOND_HALF_KICKOFF_UTC = KICKOFF_UTC + timedelta(minutes=63)
SECOND_HALF_LEN_S = (45 + 4) * 60          # planned 4 minutes stoppage
# Where the clip starts: 32:00 into the second half (the 78th minute).
CLIP_START_IN_SECOND_HALF_S = 32 * 60

FIRST_FRAME = {"firstHalf": 10000, "secondHalf": 100000}
HALF_START_UTC = {"firstHalf": KICKOFF_UTC, "secondHalf": SECOND_HALF_KICKOFF_UTC}
HALF_LEN_S = {"firstHalf": FIRST_HALF_LEN_S, "secondHalf": SECOND_HALF_LEN_S}
# video_s of each half's first frame (negative: both kick-offs are before the clip).
ANCHOR_VIDEO_S = {
    "secondHalf": -float(CLIP_START_IN_SECOND_HALF_S),
    "firstHalf": -float(CLIP_START_IN_SECOND_HALF_S)
    - (SECOND_HALF_KICKOFF_UTC - KICKOFF_UTC).total_seconds(),
}

# --- teams & roster (fictional) ----------------------------------------------
COMPETITION = "Northshore Championship"
VENUE = "Harborside Arena"

TEAMS = {
    "home": {"id": "SYN-CLU-HCF", "name": "Harbor City FC", "short_name": "Harbor City",
             "three_letter_code": "HCF"},
    "guest": {"id": "SYN-CLU-RCA", "name": "Redcliffe Athletic", "short_name": "Redcliffe",
              "three_letter_code": "RCA"},
}
KITS = {"home": "blue shirts, blue shorts", "guest": "white shirts with red numbers; goalkeeper in green"}

# key -> (side, shirt number, first name, last name, position, starter)
ROSTER = {
    # Harbor City FC (blue)
    "brandvold": ("home", 1, "Ilya", "Brandvold", "GK", True),
    "quintal": ("home", 2, "Mateo", "Quintal", "DF", True),
    "senda": ("home", 3, "Haruto", "Senda", "DF", True),
    "okafor_hale": ("home", 4, "Desmond", "Okafor-Hale", "DF", True),
    "vesterberg": ("home", 5, "Jonah", "Vesterberg", "DF", True),
    "ashdown": ("home", 6, "Callum", "Ashdown", "MF", True),
    "lark": ("home", 7, "Tobiah", "Lark", "FW", True),
    "eijsden": ("home", 8, "Bram", "Eijsden", "MF", True),
    "dahlquist": ("home", 9, "Soren", "Dahlquist", "FW", True),
    "venturelli": ("home", 10, "Luca", "Venturelli", "MF", True),
    "asante_brook": ("home", 11, "Kofi", "Asante-Brook", "FW", True),
    "rostami_vale": ("home", 14, "Amir", "Rostami-Vale", "MF", False),
    "arandela": ("home", 17, "Felipe", "Arandela", "MF", False),
    "corrigan": ("home", 20, "Niall", "Corrigan", "DF", False),
    # Redcliffe Athletic (white, red numbers)
    "zielonka": ("guest", 13, "Marek", "Zielonka", "GK", True),
    "delacroix_ward": ("guest", 2, "Hugo", "Delacroix-Ward", "DF", True),
    "nordvik_ames": ("guest", 3, "Elias", "Nordvik-Ames", "DF", True),
    "thalberg": ("guest", 4, "Oskar", "Thalberg", "DF", True),
    "adeyinka_rhodes": ("guest", 5, "Samuel", "Adeyinka-Rhodes", "DF", True),
    "monteverde": ("guest", 6, "Rafael", "Monteverde", "MF", True),
    "moorcroft": ("guest", 8, "Declan", "Moorcroft", "MF", True),
    "ibe_castro": ("guest", 9, "Marlon", "Ibe-Castro", "FW", True),
    "vasko": ("guest", 10, "Teodor", "Vasko", "MF", True),
    "whitlock": ("guest", 11, "Jarrah", "Whitlock", "FW", True),
    "ostergaard": ("guest", 12, "Finn", "Ostergaard", "MF", True),
    "szabo_kent": ("guest", 15, "Ruben", "Szabo-Kent", "MF", False),
    "rahimzadeh": ("guest", 18, "Idris", "Rahimzadeh", "FW", False),
    "lyle": ("guest", 21, "Cormac", "Lyle", "DF", False),
}


def player_id(key: str) -> str:
    side, number = ROSTER[key][0], ROSTER[key][1]
    return f"SYN-OBJ-{TEAMS[side]['three_letter_code']}{number:02d}"


def player_name(key: str) -> str:
    return f"{ROSTER[key][2]} {ROSTER[key][3]}"


# --- the event script ----------------------------------------------------------
# (half, seconds into the half, type, subtype, acting side, actor, extra)
# In-clip events use clip(video_s) for the time so they line up with the picture.
def clip(video_s: float) -> float:
    return CLIP_START_IN_SECOND_HALF_S + video_s


H1, H2 = "firstHalf", "secondHalf"
SCRIPT = [
    (H1, 0.0, "KickOff", None, "guest", "ibe_castro", {}),
    (H1, 3 * 60 + 12.4, "Foul", "foul", "home", "ashdown", {"fouled": "vasko"}),
    (H1, 6 * 60 + 40.2, "ShotAtGoal", "ShotWide", "home", "dahlquist",
     {"xg": 0.0812, "chance": "none", "assist": "venturelli", "shot": "rightLeg"}),
    (H1, 10 * 60 + 5.6, "CornerKick", None, "home", "venturelli", {"corner_side": "left"}),
    (H1, 14 * 60 + 22.0, "ShotAtGoal", "SavedShot", "guest", "whitlock",
     {"xg": 0.2104, "chance": "chance", "assist": "moorcroft", "shot": "leftLeg"}),
    (H1, 17 * 60 + 48.8, "Offside", None, "guest", "ibe_castro", {}),
    (H1, 22 * 60 + 31.2, "ShotAtGoal", "SuccessfulShot", "guest", "ibe_castro",
     {"xg": 0.3417, "chance": "chance", "assist": "whitlock", "shot": "rightLeg"}),
    (H1, 26 * 60 + 9.0, "Foul", "foul", "guest", "vasko", {"fouled": "eijsden"}),
    (H1, 30 * 60 + 44.6, "Foul", "foul", "guest", "vasko", {"fouled": "venturelli"}),
    (H1, 30 * 60 + 58.0, "Card", "yellow", "guest", "vasko", {}),
    (H1, 33 * 60 + 15.4, "ShotAtGoal", "BlockedShot", "home", "venturelli",
     {"xg": 0.0633, "chance": "none", "assist": "eijsden", "shot": "leftLeg"}),
    (H1, 37 * 60 + 2.8, "CornerKick", None, "guest", "moorcroft", {"corner_side": "right"}),
    (H1, 40 * 60 + 36.2, "ShotAtGoal", "ShotWide", "guest", "delacroix_ward",
     {"xg": 0.0389, "chance": "none", "shot": "rightLeg"}),
    (H1, 43 * 60 + 51.0, "ShotAtGoal", "SavedShot", "home", "lark",
     {"xg": 0.1876, "chance": "chance", "assist": "quintal", "shot": "rightLeg"}),
    (H1, 45 * 60 + 40.4, "Foul", "foul", "home", "okafor_hale", {"fouled": "ibe_castro"}),
    (H1, FIRST_HALF_LEN_S - 0.4, "FinalWhistle", None, None, None, {}),
    (H2, 0.0, "KickOff", None, "home", "dahlquist", {}),
    (H2, 3 * 60 + 26.8, "CornerKick", None, "home", "venturelli", {"corner_side": "right"}),
    (H2, 6 * 60 + 55.2, "ShotAtGoal", "BlockedShot", "guest", "ostergaard",
     {"xg": 0.0721, "chance": "none", "assist": "vasko", "shot": "rightLeg"}),
    (H2, 9 * 60 + 13.6, "Foul", "foul", "guest", "nordvik_ames", {"fouled": "lark"}),
    (H2, 10 * 60 + 2.0, "Card", "yellow", "guest", "nordvik_ames", {}),
    (H2, 12 * 60 + 30.0, "Substitution", None, "home", None, {"in": "rostami_vale", "out": "ashdown"}),
    (H2, 15 * 60 + 18.4, "CornerKick", None, "home", "venturelli", {"corner_side": "left"}),
    (H2, 15 * 60 + 24.0, "ShotAtGoal", "SuccessfulShot", "home", "okafor_hale",
     {"xg": 0.1932, "chance": "chance", "assist": "venturelli", "shot": "head"}),
    (H2, 18 * 60 + 40.0, "Substitution", None, "guest", None, {"in": "szabo_kent", "out": "vasko"}),
    (H2, 21 * 60 + 7.2, "ShotAtGoal", "SavedShot", "guest", "whitlock",
     {"xg": 0.1544, "chance": "chance", "assist": "szabo_kent", "shot": "leftLeg"}),
    (H2, 24 * 60 + 33.6, "Offside", None, "home", "dahlquist", {}),
    (H2, 26 * 60 + 50.0, "Foul", "foul", "home", "senda", {"fouled": "whitlock"}),
    (H2, 28 * 60 + 12.8, "ShotAtGoal", "ShotWide", "home", "asante_brook",
     {"xg": 0.0957, "chance": "none", "assist": "eijsden", "shot": "leftLeg"}),
    (H2, 29 * 60 + 5.0, "Substitution", None, "guest", None, {"in": "rahimzadeh", "out": "ibe_castro"}),
    (H2, 30 * 60 + 20.0, "Substitution", None, "home", None, {"in": "arandela", "out": "venturelli"}),
    (H2, 31 * 60 + 18.4, "Foul", "foul", "guest", "ostergaard", {"fouled": "quintal"}),
    # --- inside the clip (video_s 0..60) ---
    (H2, clip(24.0), "ShotAtGoal", "SavedShot", "home", "lark",
     {"xg": 0.2688, "chance": "chance", "assist": "quintal", "shot": "rightLeg",
      "clip": (17.3, 29.0)}),
    (H2, clip(25.84), "ShotAtGoal", "SuccessfulShot", "home", "asante_brook",
     {"xg": 0.6120, "chance": "sitter", "shot": "rightLeg", "origin": "rebound",
      "clip": (17.3, 31.0)}),
]

# Commentary captions for the clip (video seconds). The clip has no audio; this
# is the caption track the assistant reads as the "broadcast commentary".
COMMENTARY = [
    (0.4, 4.0, "Seventy-eight minutes gone here at the Harborside Arena, and it is still one apiece."),
    (4.2, 8.2, "Harbor City keeping it on the floor around the centre circle, nice and patient."),
    (8.4, 11.8, "Ostergaard tries to shield it for Redcliffe, but he is crowded out."),
    (12.0, 15.2, "Nordvik-Ames steps across, Quintal reads it and nicks the ball away."),
    (15.4, 17.2, "Thalberg has to turn and chase now."),
    (17.4, 20.4, "Harbor City spread it wide, they have numbers going forward."),
    (20.6, 22.9, "Tobiah Lark is away, he cuts inside, into the box..."),
    (23.0, 24.1, "Lark shoots!"),
    (24.2, 25.3, "Saved by Zielonka! But it's loose..."),
    (25.4, 27.4, "And Asante-Brook bundles it in! Harbor City lead!"),
    (27.6, 31.0, "Kofi Asante-Brook, first to the rebound, and it is two-one Harbor City."),
    (31.2, 34.8, "Arms out wide, he races away, and the home end is bouncing."),
    (35.0, 37.8, "Zielonka with his hands on his head. He did so well to stop the first one."),
    (38.0, 41.8, "The whole team piles on, Dahlquist, Lark, Okafor-Hale and Vesterberg in the huddle."),
    (42.0, 45.6, "Let's see it again from the camera inside the net."),
    (45.8, 49.6, "Lark's effort is palmed away, and Asante-Brook reacts quickest to slide it home."),
    (49.8, 53.6, "Nothing the goalkeeper could do about the second chance."),
    (53.8, 57.6, "Captain Bram Eijsden catching his breath. A big lead to protect now."),
    (57.8, 59.8, "Redcliffe have it all to do with a little over ten minutes left."),
]


# --- helpers ---------------------------------------------------------------------
def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def iso_ms(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}+00:00"


def minute_of(half: str, sec: float) -> tuple[int, int | None, str]:
    base = 0 if half == H1 else 45
    if sec >= 45 * 60 and sec > 0:
        stoppage = int((sec - 45 * 60) // 60) + 1
        return base + 45, stoppage, f"{base + 45}.+{stoppage}"
    m = base + int(sec // 60) + 1
    return m, None, f"{m}."


def frame_of(half: str, sec: float) -> int:
    return FIRST_FRAME[half] + int(round(sec * HZ))


def video_s_of(half: str, sec: float) -> float:
    return round(ANCHOR_VIDEO_S[half] + sec, 2)


def frame_for_video_s(video_s: float) -> int:
    return FIRST_FRAME[H2] + int(round((video_s - ANCHOR_VIDEO_S[H2]) * HZ))


def team_attrs(side: str, prefix: str = "") -> dict:
    t = TEAMS[side]
    return {f"Team{prefix}Id": t["id"], f"Team{prefix}Name": t["name"],
            f"Team{prefix}ShortName": t["short_name"],
            f"{'Team' + prefix if prefix else ''}ThreeLetterCode": t["three_letter_code"],
            f"Team{prefix}Role": side}


def person_attrs(key: str, prefix: str = "Player") -> dict:
    return {f"{prefix}Id": player_id(key), f"{prefix}FirstName": ROSTER[key][2],
            f"{prefix}LastName": ROSTER[key][3], f"{prefix}Alias": player_name(key)}


def build_events() -> list[dict]:
    events = []
    score = {"home": 0, "guest": 0}
    for seq, (half, sec, etype, subtype, side, actor, extra) in enumerate(SCRIPT, start=1):
        when = HALF_START_UTC[half] + timedelta(seconds=sec)
        minute, stoppage, label = minute_of(half, sec)
        eid = f"SYN-EVT-{seq:04d}"
        attrs = {"GameSection": half, "MinuteOfPlay": label}
        resolved: dict = {}
        is_hl, hl_type, xg, chance, clip_win = False, None, None, None, None
        pid = pname = None

        if etype == "Foul":
            fouled = extra["fouled"]
            other = "guest" if side == "home" else "home"
            attrs.update(team_attrs(side, "Fouler"))
            attrs.update(person_attrs(actor, "Fouler"))
            attrs.update(team_attrs(other, "Fouled"))
            attrs.update(person_attrs(fouled, "Fouled"))
            attrs["FoulType"] = subtype
            resolved = {"FoulerResolvedName": player_name(actor), "FouledResolvedName": player_name(fouled)}
            pid, pname = player_id(actor), player_name(actor)
        elif etype == "Substitution":
            attrs.update(person_attrs(extra["in"], "PlayerIn"))
            attrs.update(person_attrs(extra["out"], "PlayerOut"))
            attrs.update(team_attrs(side))
            resolved = {"PlayerInResolvedName": player_name(extra["in"]),
                        "PlayerOutResolvedName": player_name(extra["out"])}
            pid, pname = player_id(extra["in"]), player_name(extra["in"])
        elif etype == "FinalWhistle":
            attrs["FinalResult"] = f"{score['home']}:{score['guest']}"
        else:
            if side:
                attrs.update(team_attrs(side))
            if actor:
                attrs.update(person_attrs(actor))
                resolved = {"PlayerResolvedName": player_name(actor)}
                pid, pname = player_id(actor), player_name(actor)

        if etype == "Card":
            attrs["CardColor"] = subtype
            attrs["CardRating"] = subtype
        if etype == "CornerKick":
            attrs["Side"] = extra["corner_side"]
        if etype == "ShotAtGoal":
            is_hl = True
            hl_type = {"SuccessfulShot": "goal", "SavedShot": "save"}.get(subtype, "shot")
            xg, chance = extra["xg"], extra["chance"]
            attrs.update({"ShotResult": subtype, "xG": f"{xg:.4f}", "ChanceEvaluation": chance,
                          "TypeOfShot": extra["shot"], "Origin": extra.get("origin", "Other"),
                          "InsideBox": "true"})
            if extra.get("assist"):
                attrs.update(person_attrs(extra["assist"], "AssistPlayer"))
                resolved["AssistPlayerResolvedName"] = player_name(extra["assist"])
            if subtype == "SuccessfulShot":
                score[side] += 1
                attrs["Result"] = f"{score['home']}:{score['guest']}"
            if "clip" in extra:
                a, b = extra["clip"]
                clip_win = {"in_frame": frame_for_video_s(a), "out_frame": frame_for_video_s(b),
                            "in_video_s": a, "out_video_s": b}
        attrs.update({"EventId": eid, "EventTime": iso_ms(when)})

        events.append({
            "event_id": eid,
            "type": etype,
            "subtype": subtype,
            "game_section": half,
            "minute_label": label,
            "minute": minute,
            "stoppage": stoppage,
            "event_time_utc": iso(when),
            "frame_id": frame_of(half, sec),
            "video_s": video_s_of(half, sec),
            "team_id": TEAMS[side]["id"] if side else None,
            "team_name": TEAMS[side]["name"] if side else None,
            "team_side": side,
            "player_id": pid,
            "player_name": pname,
            "is_highlight": is_hl,
            "highlight_type": hl_type,
            "xg": xg,
            "chance_evaluation": chance,
            "clip": clip_win,
            "resolved_names": resolved,
            "attributes": attrs,
        })
    return events


def halves() -> dict:
    out = {}
    for half in (H1, H2):
        start = HALF_START_UTC[half]
        frames = HALF_LEN_S[half] * HZ + 1
        out[half] = {
            "first_frame_id": FIRST_FRAME[half],
            "last_frame_id": FIRST_FRAME[half] + frames - 1,
            "frames": frames,
            "first_timestamp_utc": iso(start),
            "last_timestamp_utc": iso(start + timedelta(seconds=HALF_LEN_S[half])),
            "anchor_video_s": ANCHOR_VIDEO_S[half],
        }
    return out


def match_block() -> dict:
    return {
        "match_id": MATCH_ID,
        "title": f"{TEAMS['home']['name']}:{TEAMS['guest']['name']}",
        "competition": COMPETITION,
        "venue": VENUE,
        "season": "2025/2026",
        "match_status": "live",
        "planned_kick_off_utc": iso(KICKOFF_UTC),
        "kick_off_utc": iso(KICKOFF_UTC),
        "synthetic": True,
    }


def build() -> tuple[dict, dict, str]:
    events = build_events()
    goals_so_far = [e for e in events if e["subtype"] == "SuccessfulShot"]
    h1_goals = [e for e in goals_so_far if e["game_section"] == H1]
    feed = {
        "schemaVersion": "1.1",
        "generatedBy": "backend/live-sports-agent/build_synthetic_match.py",
        "source_feed": "synthetic demo feed (fictional match, no real teams or players)",
        "match": match_block(),
        "teams": TEAMS,
        "game_sections": [
            {"Name": H1, "Result": f"{sum(e['team_side'] == 'home' for e in h1_goals)}:"
                                   f"{sum(e['team_side'] == 'guest' for e in h1_goals)}",
             "KickOff": iso_ms(KICKOFF_UTC),
             "FinalWhistle": iso_ms(KICKOFF_UTC + timedelta(seconds=FIRST_HALF_LEN_S)),
             "AdditionalTime": "2"},
            # Still being played when the feed was read: no result / final whistle.
            {"Name": H2, "KickOff": iso_ms(SECOND_HALF_KICKOFF_UTC)},
        ],
        "clock": {"hz": HZ, "frame_map": "frame_id via match_meta.json half anchors; "
                                        "video_s = seconds into the football broadcast clip"},
        "event_count": len(events),
        "events": events,
    }
    meta = {
        "schemaVersion": "1.0",
        "derivedBy": "backend/live-sports-agent/build_synthetic_match.py",
        "match": {"match_id": MATCH_ID, "home_team_name": TEAMS["home"]["name"],
                  "away_team_name": TEAMS["guest"]["name"]},
        "clock": {
            "hz": HZ,
            "clip_duration_s": CLIP_DURATION_S,
            "note": "1 video second = 1 match second. video_s = 0 is 32:00 into the second "
                    "half; everything earlier has negative video_s.",
            "halves": halves(),
        },
        "clubs": {
            TEAMS[s]["id"]: {"side": "home" if s == "home" else "away", "team_name": TEAMS[s]["name"],
                             "kit": KITS[s]}
            for s in ("home", "guest")
        },
        "goals": [{"frame_id": e["frame_id"], "game_section": e["game_section"],
                   "timestamp_utc": e["event_time_utc"], "video_s": e["video_s"],
                   "scored_by_club": e["team_id"]} for e in goals_so_far],
        "players": [
            {"player_id": player_id(k), "club_id": TEAMS[v[0]]["id"], "side": v[0],
             "jersey_number": v[1], "name": player_name(k), "role": v[4], "starter": v[5],
             "face_id": None}
            for k, v in ROSTER.items()
        ],
    }
    vtt = ["WEBVTT", ""]
    for i, (a, b, text) in enumerate(COMMENTARY):
        vtt += [str(i), f"{vtt_ts(a)} --> {vtt_ts(b)}", text, ""]
    return feed, meta, "\n".join(vtt)


def vtt_ts(s: float) -> str:
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def render() -> dict[Path, str]:
    feed, meta, vtt = build()
    return {
        EVENTS_FILE: json.dumps(feed, ensure_ascii=False, indent=2) + "\n",
        META_FILE: json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        VTT_FILE: vtt,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="fail if the data files are stale")
    args = ap.parse_args()
    files = render()
    if args.check:
        stale = [p.name for p, text in files.items()
                 if not p.exists() or p.read_text(encoding="utf-8") != text]
        if stale:
            sys.exit(f"stale: {stale}; run build_synthetic_match.py")
        print("synthetic match data is up to date")
        return
    DATA.mkdir(exist_ok=True)
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(HERE)}")


if __name__ == "__main__":
    main()
