/**
 * Match-state client — the live scoreboard / stats / highlights for the sports
 * player. Talks to the deployed match-state routes on the recs REST API
 * (`/state`, `/meta`, `/highlights`, `/reel`), which wrap the match event feed and
 * are bounded by the video playhead (`video_s`, seconds) so nothing spoils.
 *
 * Auth mirrors api.ts: the Cognito ID token in the `Authorization` header. When
 * the API base URL isn't configured, callers fall back to the mock game data.
 */

import { env } from './env';
import { getIdToken } from './cognito';

/** True when the REST API is configured (else callers use mock game data). */
export function matchStateConfigured(): boolean {
  return Boolean(env.apiBaseUrl);
}

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getIdToken();
  return token ? { Authorization: token } : {};
}

async function get<T>(path: string): Promise<T | null> {
  if (!env.apiBaseUrl) return null;
  try {
    const res = await fetch(`${env.apiBaseUrl}${path}`, { headers: await authHeaders() });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

// ---- Response shapes (subset we use) --------------------------------------

export interface MatchTeam {
  id: string;
  name: string;
  short_name: string;
  three_letter_code: string;
}

export interface MatchMeta {
  teams: { home: MatchTeam; guest: MatchTeam };
  hz: number;
  halves: Record<string, { start_video_s: number; end_video_s: number; first_frame_id: number }>;
}

export interface TeamStat {
  goals: number;
  shots: number;
  on_target: number;
  saves: number;
  corners: number;
  fouls: number;
  offsides: number;
}

/**
 * A notable player for the Players tab — bounded to the playhead. Only players
 * with a goal, shot, or card appear (no form/star/hot). `line` is a
 * pre-formatted stat summary (e.g. "2 Goals · 3 Shots"). `face_url` is a
 * presigned circle-avatar URL, or null when we have no face for this player
 * (the demo ships none)
 * (the UI then shows a team-color initial fallback).
 */
export interface PlayerCard {
  player_id: string;
  name: string;
  team: 'home' | 'away';
  goals: number;
  shots: number;
  cards: number;
  /** Extra tallies (added by the match-state engine). Optional for back-compat. */
  on_target?: number;
  assists?: number;
  fouls?: number;
  offsides?: number;
  line: string;
  face_id: string | null;
  face_url: string | null;
}

/**
 * One play-by-play row (newest-first) up to the playhead. `kind` drives the
 * marker style: start | goal | save | shot | foul | card | corner | offside | sub.
 */
export interface PbpRow {
  clock: string; // minute label, e.g. "45+2'"
  label: string; // headline, e.g. "Goal"
  detail: string; // description, e.g. "Kofi Asante-Brook scores"
  team: 'home' | 'away';
  kind: string;
}

export interface MatchStateSnapshot {
  frame_id: number;
  period: string; // preMatch | firstHalf | halfTime | secondHalf | fullTime
  score: Record<string, number>; // keyed by team short_name
  team_stats: Record<string, TeamStat>; // keyed by team short_name
  players?: PlayerCard[]; // notable players (goals/shots/cards), ordered
  play_by_play?: PbpRow[]; // full chronological up to playhead, newest-first
}

export interface HighlightClip {
  in_frame: number;
  out_frame: number;
  in_video_s: number;
  out_video_s: number;
}

export interface Highlight {
  minute: string; // "26." / minute label
  type: string; // goal | shot | save | penalty
  player: string | null;
  team: string | null; // team short_name
  xg: number | null;
  /** The event moment on the broadcast timeline. Negative for moments before
   *  the broadcast started (they have no clip and cannot be played). */
  video_s: number;
  frame_id?: number;
  clip: HighlightClip | null; // highlight window on the broadcast (start/end), or null
  /** Still for this moment (served with the frontend), or null when there is
   *  none — the UI then shows a gradient poster. */
  thumbnail_url?: string | null;
}

/** One-time UI setup: teams + per-half video_s ranges. */
export function getMeta(): Promise<MatchMeta | null> {
  return get<MatchMeta>('/meta');
}

/** Scoreboard/stats snapshot bounded to the playhead. */
export function getState(videoS: number): Promise<MatchStateSnapshot | null> {
  return get<MatchStateSnapshot>(`/state?video_s=${Math.max(0, videoS).toFixed(2)}`);
}

interface HighlightsResponse {
  highlights: Highlight[];
}

// The highlight list is static for the match; cache it for the page session.
let highlightsCache: { at: number; ttlMs: number; data: Promise<HighlightsResponse | null> } | null = null;

function fetchHighlights(): Promise<HighlightsResponse | null> {
  const now = Date.now();
  if (highlightsCache && now - highlightsCache.at < highlightsCache.ttlMs) return highlightsCache.data;
  const data = get<HighlightsResponse>('/highlights');
  highlightsCache = { at: now, ttlMs: 50 * 60 * 1000, data };
  data.then((d) => {
    if (!d) highlightsCache = null; // don't cache a failure
  });
  return data;
}

/** Full-match goal/highlight list (for the jump-to-goal rail). */
export async function getHighlights(): Promise<Highlight[]> {
  const data = await fetchHighlights();
  return data?.highlights ?? [];
}

export interface ReelSegment {
  seq: number;
  start_video_s: number;
  end_video_s: number;
  duration_s: number;
  label: string;
}

/** Merged "catch me up" reel up to the playhead. */
export async function getReel(videoS: number): Promise<ReelSegment[]> {
  const data = await get<{ segments: ReelSegment[] }>(`/reel?video_s=${Math.max(0, videoS).toFixed(2)}`);
  return data?.segments ?? [];
}

/**
 * The feed position to jump to for a highlight: the clip's real start
 * (`clip.in_video_s`, an ~8s pre-roll the backend computed) when present,
 * otherwise the event moment minus a small lead-in so you catch the build-up
 * rather than landing on the goal itself.
 */
export const JUMP_LEAD_IN_S = 6;
export function highlightSeekTarget(h: Highlight): number {
  if (h.clip) return Math.max(0, h.clip.in_video_s);
  return Math.max(0, h.video_s - JUMP_LEAD_IN_S);
}
