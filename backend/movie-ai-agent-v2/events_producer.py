"""Server-side producer: the agent's `impression` events on the profile-events stream.

docs/unified-profile-design.md §7.2: the agent writes straight to Kinesis
with its runtime role. One impression per title in a response, with
`slate_id = <session_id>:<turn>`. Written on a background thread and never
allowed to fail a turn. Only household members (opaque `p-…` ids) are sent,
the same rule the ingestion Lambda applies to client events.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from config import AWS_REGION, PROFILE_EVENTS_STREAM

logger = logging.getLogger(__name__)

_PROFILE_ID = re.compile(r"^p-[0-9a-f]{32}$")
_client = None
_pool: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _now():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def impression_events(profile_id: str, session_id: str, turn: int, title_ids: list[str]) -> list[dict]:
    ts = _now()
    return [{"event_id": str(uuid.uuid4()), "schema_version": 1, "event_type": "impression",
             "profile_id": profile_id, "account_id": None, "session_id": session_id,
             "device": None, "surface": "agent", "client_ts": ts, "server_ts": ts, "booth": False,
             "payload": {"item_id": tid, "rank": rank, "slate_id": f"{session_id}:{turn}",
                         "content_type": "movie"}}
            for rank, tid in enumerate(title_ids, 1)]


def _put(records: list[dict]) -> None:
    global _client
    try:
        if _client is None:
            import boto3
            _client = boto3.client("kinesis", region_name=AWS_REGION)
        resp = _client.put_records(StreamName=PROFILE_EVENTS_STREAM, Records=[
            {"Data": json.dumps(r).encode(), "PartitionKey": r["profile_id"]} for r in records])
        if resp.get("FailedRecordCount"):
            logger.warning(f"impressions: {resp['FailedRecordCount']} of {len(records)} not written")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"impressions not written: {e}")


def emit_impressions(profile_id: str, session_id: str, turn: int, title_ids: list[str]) -> None:
    global _pool
    if not (PROFILE_EVENTS_STREAM and title_ids and session_id and _PROFILE_ID.match(profile_id or "")):
        return
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="events-put")
    _pool.submit(_put, impression_events(profile_id, session_id, turn, title_ids))
