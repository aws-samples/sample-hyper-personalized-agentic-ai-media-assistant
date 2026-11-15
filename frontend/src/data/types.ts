export type DeviceMode = 'mobile' | 'tv';

export type Sport = 'basketball' | 'soccer';

export interface Title {
  id: string;
  name: string;
  kind: 'movie' | 'series' | 'doc' | 'live';
  year: number;
  genres: string[];
  runtime: string; // "1h 52m" or "S3 · 8 eps"
  maturity: string; // "TV-MA", "PG-13"
  match: number; // % match
  imdb: number;
  cast: string[];
  director?: string;
  synopsis: string;
  whyYoullLove: string; // GenAI reason to watch
  tags: string[]; // trending, mood, etc.
  progress?: number; // 0-1 for continue watching
  hueA: number; // poster gradient (fallback when no poster image)
  hueB: number;
  poster?: string; // real poster image URL (falls back to the gradient)
  // Sports-specific (optional)
  sport?: Sport;
  network?: string; // "Live on streamAI Sports"
  liveScore?: string; // "SEA 78 · 74 MIA" (for kind:"live" cards)
  liveClock?: string; // "Q3 4:12" / "67'" / "Tonight 7:30"
  isLive?: boolean; // true = live now, false = upcoming
}

export interface Profile {
  id: string;
  name: string;
  persona: string;
  hue: number;
  icon: string; // emoji stand-in avatar
  kid?: boolean;
}

export interface ContentRow {
  id: string;
  title: string;
  context?: string; // "Because it's morning", etc.
  titleIds: string[];
  shape?: 'landscape' | 'portrait';
}

export interface Team {
  code: string;
  name: string;
  city: string;
  hue: number;
  score: number;
  record: string;
}

export interface KeyPlay {
  clock: string; // "38'" or "Q3 4:12"
  label: string;
  detail: string;
  team: 'home' | 'away';
  kind: 'goal' | 'score' | 'foul' | 'sub' | 'chance' | 'start';
}

export interface PlayerStat {
  name: string;
  team: 'home' | 'away';
  line: string; // "24 PTS · 8 REB" or "2 Goals · 5 Shots"
  hot?: boolean; // streak
  star?: boolean;
  form?: number; // 0-100 heat
}

export interface CameraAngle {
  id: string;
  label: string;
  detail: string;
  hue: number;
  live?: boolean;
}

export interface LiveGame {
  id: string;
  sport: Sport;
  league: string;
  home: Team;
  away: Team;
  clock: string; // "38'" / "Q3 · 4:12"
  status: string; // "1st Half" / "3rd Quarter"
  viewers: string;
  keyPlays: KeyPlay[];
  players: PlayerStat[];
  angles: CameraAngle[];
  /**
   * Optional event headline. When absent, the UI falls back to the
   * "{home.city} vs {away.city}" matchup.
   */
  title?: string;
  /** Optional venue name shown in the match header. */
  venue?: string;
  /** Optional playable video (presigned S3 / CloudFront). Absent = placeholder. */
  videoUrl?: string;
}

/**
 * How a clip's video is delivered. The feed UI is source-agnostic — only the
 * ClipCard playback adapter knows how to attach each of these to a <video>.
 *   - file: a progressive MP4/WebM (what we use today; local or S3/CloudFront)
 *   - hls:  an .m3u8 stream (VOD or live) — attach via native HLS or hls.js
 *   - live: a live stream; `provider` lets us route to IVS later if needed
 */
export type ClipSource =
  | { type: 'file'; src: string }
  | { type: 'hls'; src: string }
  | { type: 'live'; src: string; provider?: 'ivs' | 'hls' };

export interface Clip {
  id: string;
  title: string;
  source: string; // parent title / match
  /** `title_card`: a catalog title shown as a poster card (no video). */
  kind: 'trailer' | 'highlight' | 'scene' | 'bts' | 'microdrama' | 'title_card';
  video?: ClipSource; // where/how the clip plays; absent for a title card
  likes: string;
  duration: string;
  hueA: number; // poster/placeholder gradient
  hueB: number;
  tag?: string; // optional pill: "Trending", "Because you like…"
  genres?: string[]; // chips shown under the title
  poster?: string; // optional still frame shown before playback
  titleId?: string; // links to a catalog Title for the "Watch Full" CTA
  /** Catalog record for feed cards from the clips feed API ("Watch Full" opens it). */
  movie?: import('../lib/api').Movie;
  /** Why the feed picked this card (shown as a small line on the card). */
  explanation?: string;
}
