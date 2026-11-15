"""L1 behaviour facts (`SESSION#behavior`) for the agent: read per turn, interpreted here.

The stream processor writes measured facts only (views, format mix, attention
per entity, live state). The agent turns them into:
- `viewed_ids`: catalog titles played in this session, excluded from new picks
  (a title asked for by name is still returned, as with other exclusions);
- `context_line`: one factual sentence added to the rerank's soft guidance, so
  the model weighs what the viewer is doing right now. No mode labels.

Only facts from the same session id as this conversation are used, so a
previous session's viewing never leaks in.
"""

from __future__ import annotations

import logging

from config import AWS_REGION, INTERACTIONS_TABLE

logger = logging.getLogger(__name__)

SK = "SESSION#behavior"
RECENT_IN_LINE = 5
ENTITIES_IN_LINE = 4


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def for_session(item: dict | None, session_id: str) -> dict | None:
    """`item` when it was written for `session_id`, else None."""
    if not item or not session_id or item.get("session_id") != session_id:
        return None
    return item


def read(profile_id: str, session_id: str) -> dict | None:
    """The member's SESSION#behavior item when it belongs to `session_id`."""
    if not (INTERACTIONS_TABLE and profile_id and session_id):
        return None
    import boto3
    try:
        item = boto3.resource("dynamodb", region_name=AWS_REGION).Table(INTERACTIONS_TABLE).get_item(
            Key={"profile_id": profile_id, "sk": SK}).get("Item")
    except Exception as e:  # noqa: BLE001 -- behaviour facts are optional
        logger.warning(f"session behaviour read failed for {profile_id}: {e}")
        return None
    return for_session(item, session_id)


def viewed_ids(item: dict | None) -> list[str]:
    """Catalog titles played this session (open or finished), newest first.
    A `title_card` (a poster card in the clips feed) previews a title without
    playing it, so it is not excluded."""
    if not item:
        return []
    ids = []
    for v in list((item.get("views") or {}).values()) + list(item.get("recent") or []):
        if v.get("type") == "title_card":
            continue
        for i in (v.get("item"), v.get("parent")):
            if i and str(i).startswith("tt") and i not in ids:
                ids.append(i)
    return ids


def _describe(v: dict, open_: bool) -> str:
    name = v.get("title") or "an untitled clip"
    kind = v.get("type") or "title"
    if kind == "title_card":
        name = f"the poster card for {name} in the clips feed"
    elif kind != "movie" and v.get("parent"):
        name = f"a {kind} from {name}"
    else:
        name = f"{name} ({kind})"
    bits = []
    if open_:
        bits.append("watching now")
    completion = v.get("completion")
    if completion is not None:
        bits.append(f"{round(_num(completion) * 100)}% watched")
    watched = _num(v.get("watched_s"))
    if watched:
        bits.append(f"{watched / 60:.1f} min" if watched >= 60 else f"{int(watched)} s")
    plays = int(_num(v.get("plays")))
    if plays > 1:
        bits.append(f"played {plays}x")
    if v.get("reason") and not open_:
        bits.append({"ended": "finished", "closed": "closed", "swipe": "swiped away",
                     "next": "moved on", "timeout": "stopped"}.get(v["reason"], v["reason"]))
    return f"{name}: {', '.join(bits)}" if bits else name


def context_line(item: dict | None) -> str:
    """One factual sentence about this session's viewing, or '' when there is none."""
    if not item:
        return ""
    parts = []
    mix = item.get("format_mix") or {}
    short, long_ = _num(mix.get("short_min")), _num(mix.get("long_min"))
    if short or long_:
        parts.append(f"watched {short:.1f} min of short-form and {long_:.1f} min of long-form"
                     f" on {item.get('device') or 'an unknown device'}")
    views = [_describe(v, True) for v in (item.get("views") or {}).values()]
    views += [_describe(v, False) for v in (item.get("recent") or [])]
    if views:
        parts.append("recent: " + "; ".join(views[:RECENT_IN_LINE]))
    ents = sorted((item.get("entities") or {}).values(), key=lambda e: -_num(e.get("m")))
    ents = [f"{e.get('label')} ({_num(e.get('m')):.1f})" for e in ents[:ENTITIES_IN_LINE] if e.get("label")]
    if ents:
        parts.append("most attention (decayed minutes): " + ", ".join(ents))
    live = item.get("live") or {}
    if live.get("game_id"):
        parts.append(f"joined live game {live['game_id']}")
    if not parts:
        return ""
    return "This session's viewing (facts, weigh them softly): " + " | ".join(parts) + "."
