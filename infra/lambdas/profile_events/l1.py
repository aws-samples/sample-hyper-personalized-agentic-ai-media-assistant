"""L1 behaviour state (`SESSION#behavior`): pure fold of events into session facts.

docs/unified-profile-design.md §5.3 and §7.4 step 5, with one deliberate change:
the item holds measured facts only. There is no stored `intent`/`mode` label;
each consumer (the agent, home-row ranking) interprets the facts when it reads
them.

Facts kept:
- `views`: playbacks still open, keyed by item (heartbeats folded in);
- `recent`: finished views, newest first: completion, seconds watched, how it
  ended, how many plays;
- `format_mix`: minutes watched, short-form vs long-form (by content type);
- `entities`: catalog entities (genre, cast) with attention minutes decayed
  on a 20-minute half-life, so what the session keeps returning to rises;
- `live`, `list_adds`, `agent_accepts`, `impressions` (counts per surface);
- `feedback`: explicit feed-card feedback (like / more / dislike), newest first;
- `tz_offset_min`: the viewer's latest UTC offset; each finished view carries
  its local `bucket` (weekday|weekend x daytime|evening, see time_bucket);
- `seen`: recent event ids, so a retried batch is applied once.

No I/O here: catalog lookups are passed in as a function, so the fold is
unit-testable and identical in the processor and any replay job.
"""

import math
from datetime import datetime, timezone

from contract import is_short_form

RECENT_MAX = 30
LIST_MAX = 10
FEEDBACK_MAX = 20
SEEN_MAX = 300
ENTITIES_MAX = 20
CAST_PER_TITLE = 3
HALF_LIFE_S = 20 * 60          # attention decay for session entities
VIEW_TIMEOUT_S = 10 * 60       # an open view with no heartbeat for this long is closed
MAX_WATCH_STEP_S = 120         # one event never credits more than this much watch time
SESSION_TTL_S = 4 * 3600


def parse_ts(value):
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


DAY_START_H = 5      # local 05:00 starts a day; earlier viewing belongs to the evening before
EVENING_H = 17       # local 17:00 starts the evening


def time_bucket(epoch, tz_offset_min):
    """'weekday|daytime' | 'weekday|evening' | 'weekend|daytime' | 'weekend|evening'
    in the viewer's local time, or None without an offset. Derived from the
    clock only. Viewing after midnight counts as the previous day's evening,
    so a Sunday-night film that runs past midnight stays a weekend evening."""
    if tz_offset_min is None:
        return None
    local = datetime.fromtimestamp(epoch + tz_offset_min * 60 - DAY_START_H * 3600, timezone.utc)
    day = "weekend" if local.weekday() >= 5 else "weekday"
    part = "daytime" if local.hour < EVENING_H - DAY_START_H else "evening"
    return f"{day}|{part}"


def slug(text):
    return "_".join(str(text).lower().split())


def person_name(credit):
    """Catalog cast credits read 'Name as (Role)'; the entity is the person."""
    return str(credit).split(" as (", 1)[0].strip()


def entities_for(meta):
    """Catalog entities of one title: {key: label}. `meta` = {genres, cast}."""
    out = {}
    for g in (meta or {}).get("genres") or []:
        out["genre:" + slug(g)] = g
    for credit in ((meta or {}).get("cast") or [])[:CAST_PER_TITLE]:
        name = person_name(credit)
        if name:
            out["person:" + slug(name)] = name
    return out


def empty_state(session_id):
    return {"session_id": session_id, "seq": 0, "device": None, "views": {}, "recent": [],
            "format_mix": {"short_min": 0.0, "long_min": 0.0}, "entities": {},
            "live": None, "list_adds": [], "agent_accepts": [], "feedback": [], "impressions": {},
            "seen": [], "last_ts": None}


def _decay(m, from_ts, to_ts):
    if to_ts <= from_ts:
        return m
    return m * math.pow(2.0, -(to_ts - from_ts) / HALF_LIFE_S)


def _credit(state, view, seconds, ts, catalog):
    """Add watched seconds to format mix and to the view's entities."""
    if seconds <= 0:
        return
    seconds = min(seconds, MAX_WATCH_STEP_S)
    view["watched_s"] = round(view.get("watched_s", 0) + seconds, 1)
    key = "short_min" if is_short_form(view.get("type")) else "long_min"
    state["format_mix"][key] = round(state["format_mix"][key] + seconds / 60.0, 2)
    meta = catalog(view.get("parent") or view["item"])
    for ekey, label in entities_for(meta).items():
        cur = state["entities"].get(ekey) or {"m": 0.0, "t": ts, "label": label}
        cur["m"] = round(_decay(cur["m"], cur["t"], ts) + seconds / 60.0, 3)
        cur["t"] = ts
        state["entities"][ekey] = cur


def _open(state, p, ts, surface, catalog):
    item = p["item_id"]
    view = state["views"].get(item)
    if view is None:
        meta = catalog(p.get("parent_item_id") or item) or {}
        view = {"item": item, "type": p.get("content_type"), "parent": p.get("parent_item_id"),
                "title": meta.get("title"), "duration_s": p.get("duration_s"),
                "position_s": p.get("position_s", 0), "watched_s": 0, "plays": 0,
                "surface": surface, "started": ts, "last_seen": ts}
        state["views"][item] = view
    for key, field in (("type", "content_type"), ("duration_s", "duration_s"), ("parent", "parent_item_id")):
        if p.get(field) and not view.get(key):
            view[key] = p[field]
    return view


def _close(state, view, reason, ts):
    state["views"].pop(view["item"], None)
    duration = view.get("duration_s") or 0
    position = view.get("position_s") or 0
    completion = round(min(1.0, position / duration), 3) if duration else None
    if reason == "ended":
        completion = 1.0
    entry = {"item": view["item"], "type": view.get("type"), "title": view.get("title"),
             "parent": view.get("parent"), "completion": completion,
             "watched_s": view.get("watched_s", 0), "plays": max(1, view.get("plays", 0)),
             "duration_s": view.get("duration_s"),
             "reason": reason, "surface": view.get("surface"), "ts": ts,
             "bucket": time_bucket(ts, state.get("tz_offset_min"))}
    # One entry per item: a rewatch replaces the older one and adds its plays.
    older = [r for r in state["recent"] if r["item"] == view["item"]]
    if older:
        entry["plays"] += older[0].get("plays", 1)
    state["recent"] = ([entry] + [r for r in state["recent"] if r["item"] != view["item"]])[:RECENT_MAX]


def _mark_watched(state, item, ts, surface, catalog):
    """A `recent` entry for a title the viewer marked as watched: complete, but
    with no watched seconds, so format mix and attention minutes are untouched.
    An open view of the same title is superseded by the mark."""
    state["views"].pop(item, None)
    meta = catalog(item) or {}
    older = [r for r in state["recent"] if r["item"] == item]
    entry = {"item": item, "type": None, "title": meta.get("title"), "parent": None,
             "completion": 1.0, "watched_s": 0,
             "plays": max(1, older[0].get("plays", 1)) if older else 1,
             "duration_s": None, "reason": "marked", "surface": surface, "ts": ts,
             "bucket": time_bucket(ts, state.get("tz_offset_min"))}
    state["recent"] = ([entry] + [r for r in state["recent"] if r["item"] != item])[:RECENT_MAX]


def _push(lst, value, cap):
    return ([value] + [v for v in lst if v != value])[:cap]


def apply(state, event, catalog):
    """Apply one validated, server-stamped event to `state` (mutated, returned)."""
    ts = parse_ts(event["server_ts"])
    p = event["payload"]
    etype = event["event_type"]
    state["device"] = event.get("device") or state.get("device")
    if event.get("tz_offset_min") is not None:
        state["tz_offset_min"] = event["tz_offset_min"]
    state["last_ts"] = max(ts, state.get("last_ts") or ts)
    if etype == "play_start":
        view = _open(state, p, ts, event.get("surface"), catalog)
        view["plays"] = view.get("plays", 0) + 1
        view["position_s"] = p.get("position_s", view.get("position_s", 0))
        view["last_seen"] = ts
    elif etype == "progress":
        view = _open(state, p, ts, event.get("surface"), catalog)
        view["plays"] = max(1, view.get("plays", 0))
        _credit(state, view, p.get("watched_s", 0), ts, catalog)
        view["position_s"] = p.get("position_s", view.get("position_s", 0))
        view["last_seen"] = ts
    elif etype == "play_end":
        view = _open(state, p, ts, event.get("surface"), catalog)
        _credit(state, view, p.get("watched_s", 0), ts, catalog)
        view["position_s"] = p.get("position_s", view.get("position_s", 0))
        _close(state, view, p["reason"], ts)
    elif etype == "impression":
        surface = event.get("surface") or "unknown"
        state["impressions"][surface] = state["impressions"].get(surface, 0) + 1
    elif etype == "mark_watched":
        _mark_watched(state, p["item_id"], ts, event.get("surface"), catalog)
    elif etype == "list_add":
        state["list_adds"] = _push(state["list_adds"], p["item_id"], LIST_MAX)
    elif etype == "list_remove":
        state["list_adds"] = [i for i in state["list_adds"] if i != p["item_id"]]
    elif etype == "agent_accept":
        state["agent_accepts"] = _push(state["agent_accepts"], p["item_id"], LIST_MAX)
    elif etype == "live_join":
        state["live"] = {"game_id": p["game_id"], "video_s": p.get("video_s"), "ts": ts}
    elif etype == "feedback":
        entry = {"item": p["item_id"], "value": p["value"], "type": p.get("content_type"),
                 "parent": p.get("parent_item_id"), "ts": ts}
        state["feedback"] = ([entry] + list(state.get("feedback") or []))[:FEEDBACK_MAX]
    return state


def close_stale(state, now):
    for view in list(state["views"].values()):
        if now - view.get("last_seen", now) >= VIEW_TIMEOUT_S:
            _close(state, view, "timeout", view.get("last_seen", now))


def prune_entities(state, now):
    """Decay every entity to `now` and keep the top ENTITIES_MAX."""
    ents = {}
    for key, e in state["entities"].items():
        m = _decay(e["m"], e["t"], now)
        if m >= 0.01:
            ents[key] = {"m": round(m, 3), "t": now, "label": e.get("label")}
    top = sorted(ents.items(), key=lambda kv: -kv[1]["m"])[:ENTITIES_MAX]
    state["entities"] = dict(top)


def fold(state, events, catalog):
    """Fold one profile's events (any order) into its L1 state.

    Returns (new_state, applied_count). A different `session_id` starts a fresh
    session: L1 never carries facts across sessions.
    """
    applied = 0
    for event in sorted(events, key=lambda e: (e["server_ts"], e.get("client_ts", ""))):
        sid = event["session_id"]
        if state is None or state.get("session_id") != sid:
            state = empty_state(sid)
        if event["event_id"] in state["seen"]:
            continue
        close_stale(state, parse_ts(event["server_ts"]))
        apply(state, event, catalog)
        state["seen"] = ([event["event_id"]] + state["seen"])[:SEEN_MAX]
        applied += 1
    if state is not None and applied:
        state["seq"] = state.get("seq", 0) + applied
        prune_entities(state, state["last_ts"])
        state["expires_at"] = int(state["last_ts"] + SESSION_TTL_S)
    return state, applied
