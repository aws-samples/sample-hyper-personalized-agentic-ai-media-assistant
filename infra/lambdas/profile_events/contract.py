"""ProfileEvent v1 envelope (docs/unified-profile-design.md §7.1).

Validated in code rather than in a schema registry: the contract is small and
closed, and the ingestion Lambda is its only client-facing entry point.

Rules enforced here:
- every field is typed and bounded; unknown envelope or payload keys are
  rejected, so free text cannot ride along (conversation text stays in
  AgentCore Memory);
- there is no `agent_reject`: a viewer turning picks down is said in the
  conversation, which reaches AgentCore long-term memory (TastePreferences);
- `server_ts`, `booth` and `account_id` are server-set and never taken from
  the client.
"""

import re

SCHEMA_VERSION = 1
MAX_BATCH = 50

_ID = re.compile(r"^[A-Za-z0-9:_.\-]{1,128}$")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")

CONTENT_TYPES = {"movie", "episode", "series", "microdrama", "highlight", "trailer",
                 "scene", "bts", "live", "title_card"}
# Short-form vs long-form follows the content type (a closed catalog field).
# `title_card` is a catalog title shown as a poster card in the clips feed: a
# preview of the title (its `parent_item_id`), not a viewing of it.
SHORT_FORM = {"microdrama", "highlight", "trailer", "scene", "bts", "title_card"}
FEEDBACK_VALUES = {"like", "more", "dislike"}
DEVICES = {"phone", "tv", "tablet", "desktop"}
SURFACES = {"vod_player", "clips_feed", "highlights_feed", "sports", "title_detail",
            "agent", "home"}
END_REASONS = {"ended", "closed", "next", "swipe"}

# Per event type: payload key -> (kind, required). Kinds: id, num, content_type,
# end_reason, feedback.
_PLAYBACK_OPTIONAL = {"position_s": ("num", False), "duration_s": ("num", False),
                      "content_type": ("content_type", False), "parent_item_id": ("id", False)}
PAYLOADS = {
    "play_start": {"item_id": ("id", True), **_PLAYBACK_OPTIONAL,
                   "content_type": ("content_type", True)},
    "progress": {"item_id": ("id", True), "watched_s": ("num", True), **_PLAYBACK_OPTIONAL},
    "play_end": {"item_id": ("id", True), "reason": ("end_reason", True),
                 "watched_s": ("num", False), **_PLAYBACK_OPTIONAL},
    "impression": {"item_id": ("id", True), "rank": ("num", False), "slate_id": ("id", False),
                   "content_type": ("content_type", False)},
    "agent_accept": {"item_id": ("id", True), "slate_id": ("id", False)},
    "list_add": {"item_id": ("id", True)},
    # The viewer says they have seen the title (Title Detail "Mark as watched"):
    # scored as a finished view, with no watch minutes behind it.
    "mark_watched": {"item_id": ("id", True)},
    "list_remove": {"item_id": ("id", True)},
    "live_join": {"game_id": ("id", True), "video_s": ("num", False)},
    "highlight_open": {"item_id": ("id", True), "game_id": ("id", False)},
    # Explicit feedback on a feed card: like / more like this / less like this.
    "feedback": {"item_id": ("id", True), "value": ("feedback", True),
                 "content_type": ("content_type", False), "parent_item_id": ("id", False)},
}
EVENT_TYPES = set(PAYLOADS)

_CLIENT_FIELDS = {"event_id", "schema_version", "event_type", "profile_id", "session_id",
                  "device", "surface", "client_ts", "payload", "tz_offset_min"}
TZ_OFFSET_MAX_MIN = 14 * 60   # UTC-12 .. UTC+14


def _check(kind, value):
    if kind == "id":
        return isinstance(value, str) and bool(_ID.match(value))
    if kind == "num":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1e7
    if kind == "content_type":
        return value in CONTENT_TYPES
    if kind == "end_reason":
        return value in END_REASONS
    if kind == "feedback":
        return value in FEEDBACK_VALUES
    return False


def validate(event):
    """Return None when `event` is a valid client event, else a short reason."""
    if not isinstance(event, dict):
        return "not an object"
    extra = set(event) - _CLIENT_FIELDS
    if extra:
        return f"unknown fields: {sorted(extra)}"
    if event.get("schema_version") != SCHEMA_VERSION:
        return "schema_version"
    for key in ("event_id", "profile_id", "session_id"):
        if not _check("id", event.get(key)):
            return key
    if event.get("event_type") not in EVENT_TYPES:
        return "event_type"
    if event.get("device") not in DEVICES:
        return "device"
    if event.get("surface") not in SURFACES:
        return "surface"
    if not (isinstance(event.get("client_ts"), str) and _TS.match(event["client_ts"])):
        return "client_ts"
    if "tz_offset_min" in event:
        tz = event["tz_offset_min"]   # local time minus UTC, in minutes (optional)
        if not (isinstance(tz, int) and not isinstance(tz, bool) and abs(tz) <= TZ_OFFSET_MAX_MIN):
            return "tz_offset_min"
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return "payload"
    spec = PAYLOADS[event["event_type"]]
    extra = set(payload) - set(spec)
    if extra:
        return f"unknown payload fields: {sorted(extra)}"
    for key, (kind, required) in spec.items():
        if key not in payload:
            if required:
                return f"payload.{key} missing"
            continue
        if not _check(kind, payload[key]):
            return f"payload.{key}"
    return None


def is_short_form(content_type):
    return content_type in SHORT_FORM
