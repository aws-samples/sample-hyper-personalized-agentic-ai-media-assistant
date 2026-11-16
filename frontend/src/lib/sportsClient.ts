import { env } from './env';
import { gameById } from '../data/sports';
import type { DeviceMode, LiveGame } from '../data/types';

/**
 * "Join now" entry point for a game, in video seconds. The soccer broadcast is
 * a short clip of a late stretch of the match (the 78th minute onwards), so it
 * joins at the start of the clip; the playhead also drives the spoiler-bounded
 * scoreboard/agent, so the game view is consistent on entry.
 */
export const SOCCER_JOIN_OFFSET_S = 0;

export function joinOffsetForGame(game: LiveGame): number {
  if (game.sport === 'soccer') return SOCCER_JOIN_OFFSET_S;
  return 0;
}

/**
 * Live sports data source + AI seams.
 *
 * Today the "live" game is a VOD file presented as live (played from a
 * configured presigned/CloudFront URL). The two functions below are the seams
 * we'll swap later without touching the sports UI:
 *   - getLiveGameVideoUrl(): where the stream comes from (env URL now; a real
 *     backend presign or IVS/HLS endpoint later)
 *   - getGameRecap(): the "catch me up" recap (static now; Bedrock/agent later)
 */

/**
 * Resolve a playable URL for a game, or null when none is configured (the
 * player then shows a gradient placeholder). Only the soccer game has real
 * footage; the game's own `videoUrl` wins, then the env override.
 *
 * The soccer match is delivered as two renditions — 16:9 for TV/desktop and
 * 9:16 for mobile (the same timeline, centre-cropped). `video_s` (the agent's
 * spoiler bound) is the playhead into whichever rendition plays; both are the
 * same match reel, so the second offset is equivalent.
 */
export function getLiveGameVideoUrl(gameId: string, device?: DeviceMode): string | null {
  const game = gameById(gameId);
  if (game.videoUrl) return game.videoUrl;
  if (game.sport === 'soccer') {
    // Always the 16:9 horizontal rendition, including on mobile — the viewer
    // rotates the phone to watch (same as the VOD player). A 9:16 vertical
    // rendition exists (env.liveSportsUrlVertical) but isn't used for the main
    // game view; it's only for the vertical highlight clips.
    void device;
    if (env.liveSportsUrl) return env.liveSportsUrl;
    if (env.liveSportsUrlVertical) return env.liveSportsUrlVertical;
  }
  return null;
}

/**
 * "Catch me up on key plays" recap. Static per-game copy for now; this is the
 * single call a Bedrock/agent summary swaps into later.
 */
export function getGameRecap(gameId: string): string {
  const game = gameById(gameId);
  if (game.sport === 'soccer') {
    return (
      `${game.home.city} lead ${game.away.city} ${game.home.score}–${game.away.score} ` +
      `in the ${game.status.toLowerCase()}. ${game.away.name} went ahead in the first half before ` +
      `${game.home.name} levelled with a header from a corner. A rebound finish has just put ` +
      `the hosts in front — end to end since kickoff.`
    );
  }
  return (
    `${game.home.city} lead ${game.away.city} ${game.home.score}–${game.away.score} ` +
    `in the ${game.status.toLowerCase()}, powered by a hot shooting streak. A half-court ` +
    `buzzer-beater swung the momentum and the run has continued into the second half.`
  );
}
