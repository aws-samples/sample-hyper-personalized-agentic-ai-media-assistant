import { env } from './env';
import { getIdToken } from './cognito';
import { getAgentSessionId } from './agentSession';

/**
 * Client side of the behavioral pipeline (docs/unified-profile-design.md §7.1-7.2).
 *
 * Events are queued and POSTed to `/events` in batches: every 5 s, at 50
 * events, and when the page is hidden or closed. Only facts are sent (ids,
 * content types, positions, seconds watched); never free text. `server_ts`,
 * `booth` and `account_id` are set by the ingestion Lambda.
 *
 * Only household members (opaque `p-…` profile ids) emit: the static legacy
 * cards have no profile the backend can attribute events to. Every event
 * carries the shared agent session id, so the agent reads the same session's
 * behaviour facts.
 */

export type EventType =
  | 'play_start' | 'progress' | 'play_end' | 'impression' | 'agent_accept'
  | 'list_add' | 'list_remove' | 'live_join' | 'highlight_open' | 'feedback' | 'mark_watched';

export type Surface =
  | 'vod_player' | 'clips_feed' | 'highlights_feed' | 'sports' | 'title_detail' | 'agent' | 'home';

/** `title_card`: a catalog title shown as a poster card in the clips feed (a preview, not a viewing). */
export type ContentType =
  | 'movie' | 'episode' | 'series' | 'microdrama' | 'highlight' | 'trailer' | 'scene' | 'bts' | 'live'
  | 'title_card';

/** Explicit feed-card feedback: like, more like this, less like this. */
export type FeedbackValue = 'like' | 'more' | 'dislike';

type Device = 'phone' | 'tv';
type Payload = Record<string, string | number>;

interface ProfileEvent {
  event_id: string;
  schema_version: 1;
  event_type: EventType;
  profile_id: string;
  session_id: string;
  device: Device;
  surface: Surface;
  client_ts: string;
  /** Local time minus UTC, in minutes, so the processor can tell weekday/weekend and daytime/evening. */
  tz_offset_min: number;
  payload: Payload;
}

const FLUSH_MS = 5000;
const MAX_BATCH = 50;
const MAX_QUEUE = 500;
const PROFILE_ID_RE = /^p-[0-9a-f]{32}$/;

let profileId: string | null = null;
let device: Device = 'phone';
let queue: ProfileEvent[] = [];
let timer: number | null = null;
let token: string | null = null;

/** Called by the session provider when the active profile changes. */
export function setEventProfile(id: string | null): void {
  if (id !== profileId) void flush();
  profileId = id && PROFILE_ID_RE.test(id) ? id : null;
}

/** Called by the device provider; the contract uses phone|tv. */
export function setEventDevice(mode: 'mobile' | 'tv'): void {
  device = mode === 'tv' ? 'tv' : 'phone';
}

function enabled(): boolean {
  return Boolean(env.apiBaseUrl && profileId);
}

/** Queue one event. Values must be ids, numbers or contract enums. */
export function track(type: EventType, surface: Surface, payload: Payload): void {
  if (!enabled()) return;
  queue.push({
    event_id: crypto.randomUUID(),
    schema_version: 1,
    event_type: type,
    profile_id: profileId as string,
    session_id: getAgentSessionId(),
    device,
    surface,
    client_ts: new Date().toISOString(),
    tz_offset_min: -new Date().getTimezoneOffset(),
    payload,
  });
  if (queue.length > MAX_QUEUE) queue = queue.slice(-MAX_QUEUE);
  if (queue.length >= MAX_BATCH) void flush();
  else if (timer === null) timer = window.setTimeout(() => void flush(), FLUSH_MS);
}

/** Send queued events. `keepalive` lets the request outlive a closing page. */
export async function flush(keepalive = false): Promise<void> {
  if (timer !== null) {
    window.clearTimeout(timer);
    timer = null;
  }
  if (!queue.length || !env.apiBaseUrl) return;
  if (!keepalive) {
    try {
      token = (await getIdToken()) ?? token;
    } catch {
      /* keep the last token */
    }
  }
  if (!token) return;
  while (queue.length) {
    const batch = queue.splice(0, MAX_BATCH);
    try {
      const res = await fetch(`${env.apiBaseUrl}/events`, {
        method: 'POST',
        keepalive,
        headers: { 'Content-Type': 'application/json', Authorization: token },
        body: JSON.stringify({ events: batch }),
      });
      // 5xx / throttling: requeue once for the next flush; 4xx is not retried.
      if (res.status >= 500 || res.status === 429) {
        queue = batch.concat(queue);
        break;
      }
    } catch {
      if (!keepalive) queue = batch.concat(queue);
      break;
    }
  }
  if (queue.length && timer === null && !keepalive) {
    timer = window.setTimeout(() => void flush(), FLUSH_MS);
  }
}

if (typeof window !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') void flush(true);
  });
  window.addEventListener('pagehide', () => void flush(true));
}
