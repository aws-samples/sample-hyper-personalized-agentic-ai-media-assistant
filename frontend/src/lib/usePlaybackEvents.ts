import { useEffect, useRef, type RefObject } from 'react';
import { track, type ContentType, type Surface } from './events';

export interface PlaybackItem {
  itemId: string;
  contentType: ContentType;
  surface: Surface;
  /** Catalog title a clip belongs to (e.g. a micro drama's film). */
  parentItemId?: string;
}

interface Options {
  /** Feed card: emits an impression when shown and ends the view when scrolled away. */
  feed?: boolean;
  /** Position of the card in its feed (impression rank). */
  rank?: number;
}

const SHORT_FORM: ContentType[] = ['microdrama', 'highlight', 'trailer', 'scene', 'bts', 'title_card'];
const VISIBLE = 0.6;

/** Simulated playback state, for a player with no video file (poster + running clock). */
export interface PlaybackClock {
  playing: boolean;
  positionMs: number;
  durationMs: number;
}

/**
 * The same `play_start` / `progress` / `play_end` events as
 * `usePlaybackEvents`, driven by a simulated clock instead of a <video>.
 * Only time that passes while playing counts as watched; a jump (seek, skip)
 * does not. Reaching the end sends `ended`; leaving the player sends `closed`.
 */
export function useClockPlaybackEvents(item: PlaybackItem | null, clock: PlaybackClock): void {
  const itemRef = useRef(item);
  itemRef.current = item;
  const clockRef = useRef(clock);
  clockRef.current = clock;
  const key = item ? `${item.itemId}|${item.contentType}|${item.surface}` : '';
  // Per-view state shared by the two effects below; reset on each new item.
  const view = useRef({ open: false, last: 0, watchedMs: 0, lastBeat: 0 });
  // Called on every clock change; set by the per-item effect.
  const tick = useRef<(() => void) | null>(null);

  useEffect(() => {
    const it = itemRef.current;
    if (!it) return;
    const beatMs = SHORT_FORM.includes(it.contentType) ? 5000 : 30000;
    const st = view.current;
    st.open = false;
    st.last = clockRef.current.positionMs;
    st.watchedMs = 0;

    const base = () => {
      const c = clockRef.current;
      const p: Record<string, string | number> = {
        item_id: it.itemId,
        content_type: it.contentType,
        position_s: Math.round(c.positionMs / 1000),
      };
      if (c.durationMs > 0) p.duration_s = Math.round(c.durationMs / 1000);
      if (it.parentItemId) p.parent_item_id = it.parentItemId;
      return p;
    };
    const watchedS = () => Math.round(st.watchedMs / 100) / 10;
    const end = (reason: 'ended' | 'closed') => {
      if (!st.open) return;
      st.open = false;
      track('play_end', it.surface, { ...base(), reason, watched_s: watchedS() });
      st.watchedMs = 0;
    };

    tick.current = () => {
      const c = clockRef.current;
      if (c.playing && !st.open) {
        st.open = true;
        st.watchedMs = 0;
        st.lastBeat = Date.now();
        st.last = c.positionMs;
        track('play_start', it.surface, base());
        return;
      }
      const d = c.positionMs - st.last;
      st.last = c.positionMs;
      if (!st.open) return;
      if (c.playing && d > 0 && d <= 2000) st.watchedMs += d;
      if (st.watchedMs > 0 && Date.now() - st.lastBeat >= beatMs) {
        track('progress', it.surface, { ...base(), watched_s: watchedS() });
        st.watchedMs = 0;
        st.lastBeat = Date.now();
      }
      if (c.durationMs > 0 && c.positionMs >= c.durationMs) end('ended');
    };
    tick.current();

    return () => {
      tick.current = null;
      end('closed');
    };
  }, [key]);

  useEffect(() => {
    tick.current?.();
  }, [clock.playing, clock.positionMs, clock.durationMs]);
}

/**
 * Playback events for one <video>: `play_start`, `progress` heartbeats with
 * the seconds actually watched (30 s long-form, 5 s short-form), and
 * `play_end` (ended / closed / swipe). Looping clips report each loop as a
 * finished view, so rewatches are counted. Seeking is not counted as watching.
 */
export function usePlaybackEvents(
  videoRef: RefObject<HTMLVideoElement | null>,
  item: PlaybackItem | null,
  { feed = false, rank }: Options = {},
): void {
  const itemRef = useRef(item);
  itemRef.current = item;
  const key = item ? `${item.itemId}|${item.contentType}|${item.surface}` : '';

  useEffect(() => {
    const v = videoRef.current;
    const it = itemRef.current;
    if (!v || !it) return;
    const beatMs = SHORT_FORM.includes(it.contentType) ? 5000 : 30000;
    let open = false;
    let last = v.currentTime;
    let watched = 0;
    let lastBeat = Date.now();

    const base = () => {
      const p: Record<string, string | number> = {
        item_id: it.itemId,
        content_type: it.contentType,
        position_s: Math.round(v.currentTime),
      };
      if (Number.isFinite(v.duration) && v.duration > 0) p.duration_s = Math.round(v.duration);
      if (it.parentItemId) p.parent_item_id = it.parentItemId;
      return p;
    };
    const start = () => {
      if (open) return;
      open = true;
      watched = 0;
      lastBeat = Date.now();
      track('play_start', it.surface, base());
    };
    const end = (reason: 'ended' | 'closed' | 'swipe', position?: number) => {
      if (!open) return;
      open = false;
      const p = base();
      if (position !== undefined) p.position_s = Math.round(position);
      track('play_end', it.surface, { ...p, reason, watched_s: Math.round(watched * 10) / 10 });
      watched = 0;
    };

    const onPlaying = () => start();
    const onTime = () => {
      const t = v.currentTime;
      const d = t - last;
      // A loop restarts near 0 right after the end: one finished view, then a new one.
      if (open && v.loop && d < -1 && Number.isFinite(v.duration) && last > v.duration - 1.5) {
        end('ended', v.duration);
        start();
      } else if (open && d > 0 && d < 2) {
        watched += d;
      }
      last = t;
      if (open && watched > 0 && Date.now() - lastBeat >= beatMs) {
        const { item_id, position_s, duration_s, content_type, parent_item_id } = base();
        const p: Record<string, string | number> = { item_id, position_s, content_type, watched_s: Math.round(watched * 10) / 10 };
        if (duration_s !== undefined) p.duration_s = duration_s;
        if (parent_item_id !== undefined) p.parent_item_id = parent_item_id;
        track('progress', it.surface, p);
        watched = 0;
        lastBeat = Date.now();
      }
    };
    const onEnded = () => end('ended', v.duration);

    v.addEventListener('playing', onPlaying);
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('ended', onEnded);
    if (!v.paused && v.readyState > 2) start();

    let observer: IntersectionObserver | null = null;
    if (feed) {
      let shown = false;
      const card = (v.closest('[data-clip-id]') as Element | null) ?? v;
      observer = new IntersectionObserver(
        (entries) => {
          for (const e of entries) {
            if (e.isIntersecting && !shown) {
              shown = true;
              const p: Record<string, string | number> = { item_id: it.itemId, content_type: it.contentType };
              if (rank !== undefined) p.rank = rank;
              track('impression', it.surface, p);
            } else if (!e.isIntersecting) {
              end('swipe');
            }
          }
        },
        { threshold: VISIBLE },
      );
      observer.observe(card);
    }

    return () => {
      observer?.disconnect();
      v.removeEventListener('playing', onPlaying);
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('ended', onEnded);
      end('closed');
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, feed, rank, videoRef]);
}
