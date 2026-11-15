import type { Clip } from '../data/types';
import { env } from './env';
import { getAccessToken, getIdToken } from './cognito';
import { getAgentSessionId } from './agentSession';
import { getHighlights } from './sportsStateClient';
import { groupHighlightsToClips, highlightClipUrl, highlightThumbUrl } from './highlightClips';
import type { Followed } from './followed';
import { SOCCER_GAME } from '../data/sports';

/**
 * Clips feed data source.
 *
 * Household members get a personalized feed from the clips feed API
 * (POST /clips/session and /clips/refill): microdramas and catalog titles
 * (shown as poster cards), ranked from this session's card reports and the
 * member's long-term taste. Nothing about the order is hard-coded here.
 *
 * Microdrama video: on the token-protected media CloudFront distribution when
 * VITE_MEDIA_BASE_URL is set (the API returns the CDN path; the ?token= access
 * token is all the edge validator needs, and <video src> is not CORS-gated),
 * otherwise the presigned S3 URL the API returns.
 *
 * A member who follows players (or a team) in the live match also gets those
 * vertical match highlights at the top of the feed (followedHighlightClips),
 * built here from the match feed rather than ranked by the feed API.
 *
 * Profiles outside the household get the static micro-drama feed
 * (public/clips/microdramas.json).
 */

const FEED_URL = '/clips/microdramas.json';
const PROFILE_ID_RE = /^p-[0-9a-f]{32}$/;
const MEDIA_BASE = env.mediaBaseUrl?.replace(/\/+$/, '') ?? '';

interface FeedFile {
  clips: Clip[];
}

/** Append the access token the media edge validator requires (?token=). */
function withToken(url: string, token: string | null): string {
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

/**
 * Resolve a clip's video URL against the media CDN and attach the token.
 * The static feed stores relative paths (e.g. "microdramas/one-bar.mp4"). Absolute URLs are left
 * as-is. Falls back to a same-origin `/clips/` path when no CDN is configured.
 */
function resolveClipSrc(src: string, token: string | null): string {
  if (/^https?:\/\//i.test(src)) return withToken(src, token);
  const name = src.replace(/^\/+/, '').replace(/^clips\//, '');
  const base = MEDIA_BASE ? `${MEDIA_BASE}/${name}` : `/clips/${name}`;
  return withToken(base, token);
}

const feedCache = new Map<string, Clip[]>();

async function loadFeed(url: string): Promise<Clip[]> {
  const cached = feedCache.get(url);
  if (cached) return cached;
  // Same-origin fetch — no token, no CORS.
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load clips feed: ${res.status}`);
  const data = (await res.json()) as FeedFile;
  const clips = data.clips ?? [];
  feedCache.set(url, clips);
  return clips;
}

/** Attach the media token to each clip's video src. */
function resolveClips(clips: Clip[], token: string | null): Clip[] {
  return clips.map((clip) =>
    clip.video ? { ...clip, video: { ...clip.video, src: resolveClipSrc(clip.video.src, token) } } : clip,
  );
}

/** mm:ss for a clip's on-screen duration. */
function fmtDuration(seconds: number): string {
  const s = Math.max(1, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** Followed-highlight cards: built client-side, so never reported to the feed API. */
export const isHighlightCard = (clip: Clip): boolean => clip.kind === 'highlight';

/**
 * Vertical match highlights of the profile's followed players (or, with no
 * followed player in the match, its followed team) as clip-feed cards. Pulls
 * the /highlights feed, groups it into key moments (same recipe as the Home
 * "Because you follow" rail), and resolves each moment's window of the
 * vertical match broadcast + its still.
 */
export async function followedHighlightClips(followed: Followed): Promise<Clip[]> {
  const players = followed.players;
  const teams = followed.teams.filter((t) => t.gameId === 'g-soccer');
  if (!players.length && !teams.length) return [];
  const groups = groupHighlightsToClips(await getHighlights());
  let picked = groups
    .map((g) => ({ g, who: players.find((p) => g.members.some((m) => m.player && p.names.includes(m.player))) }))
    .filter((x) => x.who);
  if (!picked.length) {
    picked = groups
      .map((g) => ({ g, who: teams.find((t) => g.members.some((m) => m.team && t.names.includes(m.team))) }))
      .filter((x) => x.who);
  }

  const clips = await Promise.all(
    picked.map(async ({ g, who }) => {
      const src = await highlightClipUrl(g, 'vertical');
      const poster = highlightThumbUrl(g, 'vertical');
      if (!src || !who) return null;
      const m = g.members.find((x) => x.player && who.names.includes(x.player)) ?? g.lead;
      const minute = m.minute.replace(/\.$/, '');
      const type = m.type.charAt(0).toUpperCase() + m.type.slice(1);
      const clip: Clip = {
        id: `follow-${g.n}`,
        title: `${m.player ?? who.name} — ${minute}' ${type}`,
        source: `${SOCCER_GAME.league} · ${SOCCER_GAME.home.city} vs ${SOCCER_GAME.away.city}`,
        kind: 'highlight',
        video: { type: 'file', src },
        poster: poster ?? undefined,
        likes: '',
        duration: fmtDuration(g.outVideoS - g.inVideoS),
        tag: `Because you follow ${who.name}`,
        hueA: SOCCER_GAME.home.hue,
        hueB: SOCCER_GAME.away.hue,
      };
      return clip;
    }),
  );
  return clips.filter((c): c is Clip => c !== null);
}

/** Static default feed for profiles outside the household. Household members
 *  use the clips feed API instead (startClipFeed). */
export async function getClipFeed(): Promise<Clip[]> {
  // Token is only needed for the video URLs (media distribution), not the feed.
  const token = await getAccessToken();
  return resolveClips(await loadFeed(FEED_URL), token);
}

// --- personalized feed (household members) -------------------------------------

/** True when `profileId` can use the clips feed API (a household member). */
export function usesClipFeedApi(profileId?: string | null): profileId is string {
  return Boolean(env.apiBaseUrl && profileId && PROFILE_ID_RE.test(profileId));
}

/** One card report: cumulative milliseconds on screen and the latest feedback. */
export interface CardReport {
  item_id: string;
  dwell_ms: number;
  feedback?: 'none' | 'like' | 'more' | 'dislike';
}

export interface ClipFeedPage {
  /** New cards to append. */
  cards: Clip[];
  /** How many of the queued (not yet seen) cards to keep before `cards`. */
  keep: number;
}

interface FeedCard {
  id: string;
  kind: 'microdrama' | 'title_card';
  title: string;
  genres: string[];
  plot?: string | null;
  year?: number | null;
  rating?: number | null;
  maturity?: string | null;
  cast: string[];
  posterUrl?: string | null;
  videoPath?: string | null;
  videoUrl?: string | null;
  reason: string;
  explanation?: { summary?: string };
}

/** Stable placeholder hues per card, so poster fills differ between cards. */
function hues(id: string): [number, number] {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return [h, (h + 70) % 360];
}

function toClip(c: FeedCard, mediaToken: string | null): Clip {
  const [hueA, hueB] = hues(c.id);
  const src = c.videoPath && MEDIA_BASE ? resolveClipSrc(c.videoPath, mediaToken) : (c.videoUrl ?? undefined);
  const isVideo = c.kind === 'microdrama' && Boolean(src);
  return {
    id: c.id,
    title: c.title,
    source: isVideo ? 'streamAI Originals' : [c.year, c.maturity?.toUpperCase()].filter(Boolean).join(' · '),
    kind: isVideo ? 'microdrama' : 'title_card',
    video: isVideo && src ? { type: 'file', src } : undefined,
    likes: '',
    duration: '',
    hueA,
    hueB,
    genres: c.genres,
    poster: c.posterUrl ?? undefined,
    explanation: c.explanation?.summary,
    movie: {
      id: c.id,
      title: c.title,
      posterUrl: c.posterUrl,
      rating: c.rating,
      genres: c.genres,
      plot: c.plot,
      cast: c.cast,
      maturity: c.maturity,
      year: c.year,
    },
  };
}

async function postFeed(path: string, body: object): Promise<ClipFeedPage> {
  const [idToken, mediaToken] = await Promise.all([getIdToken(), getAccessToken()]);
  const res = await fetch(`${env.apiBaseUrl}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(idToken ? { Authorization: idToken } : {}) },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`clips feed ${path}: ${res.status}`);
  const data = (await res.json()) as { cards: FeedCard[]; keep: number };
  return { cards: data.cards.map((c) => toClip(c, mediaToken)), keep: data.keep };
}

/** First page of a household member's feed for this agent session. */
export function startClipFeed(profileId: string): Promise<ClipFeedPage> {
  return postFeed('/clips/session', { profile_id: profileId, session_id: getAgentSessionId() });
}

/**
 * Next page: send the card reports since the last call and the cards still
 * queued (not yet seen). The client keeps `keep` of the queued cards, then
 * appends `cards`.
 */
export function refillClipFeed(profileId: string, queued: string[], impressions: CardReport[]): Promise<ClipFeedPage> {
  return postFeed('/clips/refill', { profile_id: profileId, session_id: getAgentSessionId(), queued, impressions });
}

/**
 * Rebuild a clip URL with a freshly refreshed token. Used by the player to
 * recover once when a clip 401s mid-session (token expired after load).
 */
export async function refreshClipUrl(currentSrc: string): Promise<string> {
  // Only media-CDN URLs carry our token; a presigned S3 URL cannot be re-tokened.
  if (!MEDIA_BASE || !currentSrc.startsWith(MEDIA_BASE)) return currentSrc;
  const base = currentSrc.split('?')[0];
  const token = await getAccessToken();
  return withToken(base, token);
}
