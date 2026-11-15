import { env } from './env';
import { ensureMediaAccess, isProtectedMedia } from './mediaSession';
import type { Highlight } from './sportsStateClient';

/**
 * Map the match-state /highlights feed to playable key-moment clips.
 *
 * There are no separate clip files: a key moment plays a window of the match
 * broadcast itself (env.liveSportsUrl / liveSportsUrlVertical, the same
 * timeline in 16:9 and 9:16). Highlights whose clip windows overlap merge into
 * one group (a save and the rebound goal a second later are one moment), and
 * a group plays [inVideoS, outVideoS] of the broadcast. Highlights from before
 * the broadcast started have no clip window and are not playable.
 *
 * Stills for each moment ship with the frontend under
 * /sports/highlights/{horizontal,vertical}/<frame_id>.jpg (cut by
 * backend/live-sports-agent/cut_highlight_thumbs.py).
 */

const THUMB_BASE = '/sports/highlights';

export interface HighlightClipGroup {
  /** 1-based group number (stable for a given feed). */
  n: number;
  /** Highlights covered by this clip, in chronological order. */
  members: Highlight[];
  /** Representative highlight (the most notable member) for labels/positioning. */
  lead: Highlight;
  inVideoS: number;
  outVideoS: number;
}

/** Where a key moment plays: the broadcast URL and the window inside it. */
export interface ClipSourceWindow {
  url: string;
  start: number;
  end: number;
}

const TYPE_RANK: Record<string, number> = { goal: 0, penalty: 1, save: 2, shot: 3 };

/** Pick the most notable member of a group for the label (goal > penalty > save > shot). */
function pickLead(members: Highlight[]): Highlight {
  return [...members].sort(
    (a, b) => (TYPE_RANK[a.type] ?? 9) - (TYPE_RANK[b.type] ?? 9),
  )[0];
}

/**
 * Group highlights into clip-aligned buckets by merging overlapping windows.
 * Only highlights with a `clip` window participate.
 */
export function groupHighlightsToClips(highlights: Highlight[]): HighlightClipGroup[] {
  const withClip = highlights.filter((h) => h.clip).sort(
    (a, b) => a.clip!.in_video_s - b.clip!.in_video_s,
  );

  const groups: { in: number; out: number; members: Highlight[] }[] = [];
  for (const h of withClip) {
    const c = h.clip!;
    const last = groups[groups.length - 1];
    if (last && c.in_video_s <= last.out + 0.01) {
      last.out = Math.max(last.out, c.out_video_s);
      last.members.push(h);
    } else {
      groups.push({ in: c.in_video_s, out: c.out_video_s, members: [h] });
    }
  }

  return groups.map((g, i) => ({
    n: i + 1,
    members: g.members,
    lead: pickLead(g.members),
    inVideoS: Math.max(0, g.in),
    outVideoS: g.out,
  }));
}

function broadcastUrl(orientation: 'horizontal' | 'vertical'): string | null {
  const url = orientation === 'vertical'
    ? env.liveSportsUrlVertical || env.liveSportsUrl
    : env.liveSportsUrl || env.liveSportsUrlVertical;
  return url || null;
}

/**
 * The broadcast URL and window for a clip group. /media/* URLs need the signed
 * cookies, so this makes sure they are set before handing the URL out.
 */
export async function highlightClipSource(
  group: HighlightClipGroup,
  orientation: 'horizontal' | 'vertical' = 'horizontal',
): Promise<ClipSourceWindow | null> {
  const url = broadcastUrl(orientation);
  if (!url) return null;
  if (isProtectedMedia(url)) await ensureMediaAccess();
  return { url, start: group.inVideoS, end: group.outVideoS };
}

/**
 * A single URL for a clip group, for plain <video src> players (feed cards):
 * the broadcast with a media-fragment window (#t=start,end), which browsers
 * use to start at the moment and stop at its end.
 */
export async function highlightClipUrl(
  group: HighlightClipGroup,
  orientation: 'horizontal' | 'vertical' = 'horizontal',
): Promise<string | null> {
  const src = await highlightClipSource(group, orientation);
  return src ? `${src.url}#t=${src.start.toFixed(2)},${src.end.toFixed(2)}` : null;
}

/** Still for a clip group (its lead moment), or null when there is none. */
export function highlightThumbUrl(
  group: HighlightClipGroup,
  orientation: 'horizontal' | 'vertical' = 'vertical',
): string | null {
  const fid = group.lead.frame_id;
  return group.lead.clip && fid != null ? `${THUMB_BASE}/${orientation}/${fid}.jpg` : null;
}

/** A short human label for a clip group, e.g. "78' · Goal — Kofi Asante-Brook". */
export function highlightLabel(group: HighlightClipGroup): string {
  const h = group.lead;
  const minute = h.minute.replace(/\.$/, '') + "'";
  const type = h.type.charAt(0).toUpperCase() + h.type.slice(1);
  return h.player ? `${minute} · ${type} — ${h.player}` : `${minute} · ${type}`;
}
