import { useEffect, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { useDevice } from '../../components/device';
import { ClipCard } from './ClipCard';
import {
  highlightClipUrl,
  highlightLabel,
  highlightThumbUrl,
  type HighlightClipGroup,
} from '../../lib/highlightClips';
import { SOCCER_GAME } from '../../data/sports';
import type { Clip } from '../../data/types';
import './Clips.css';

interface HighlightClipsFeedProps {
  /** The highlight clip groups to play (e.g. one player's tagged moments). */
  groups: HighlightClipGroup[];
  /** Which group to open on first render (array index). */
  startIndex?: number;
  /** Small pill shown on each card (e.g. "Because you follow Kofi Asante-Brook"). */
  tag?: string;
  onClose: () => void;
}

const VISIBLE_THRESHOLD = 0.7;

/**
 * A vertical, TikTok-style player for match highlights — the SAME feed
 * chrome as the Clips screen (ClipCard + Clips.css), but driven by
 * HighlightClipGroups instead of the clips feed. Each card plays its moment's
 * window of the VERTICAL match broadcast; the one in view plays.
 */
export function HighlightClipsFeed({ groups, startIndex = 0, tag, onClose }: HighlightClipsFeedProps) {
  const device = useDevice();
  const scrollerRef = useRef<HTMLDivElement>(null);
  const didInitialScroll = useRef(false);
  const [muted, setMuted] = useState(true); // start muted so autoplay is allowed
  const [clips, setClips] = useState<Clip[]>([]);

  // Resolve each group's vertical clip URL → a Clip the ClipCard can render.
  useEffect(() => {
    let active = true;
    Promise.all(
      groups.map(async (g): Promise<Clip> => {
        const src = await highlightClipUrl(g, 'vertical');
        const isGoal = g.lead.type === 'goal' || g.lead.type === 'penalty';
        return {
          id: `hl-${g.n}`,
          title: g.lead.player ?? 'Highlight',
          source: `${g.lead.minute.replace(/\.$/, '')}' · ${SOCCER_GAME.home.city} vs ${SOCCER_GAME.away.city}`,
          kind: 'highlight',
          video: { type: 'file', src: src ?? '' },
          poster: highlightThumbUrl(g, 'vertical') ?? undefined,
          likes: '—',
          duration: '',
          tag: tag ?? highlightLabel(g),
          genres: [g.lead.type.charAt(0).toUpperCase() + g.lead.type.slice(1), SOCCER_GAME.league],
          hueA: isGoal ? 355 : 210,
          hueB: 20,
        };
      }),
    ).then((cs) => {
      if (active) setClips(cs);
    });
    return () => {
      active = false;
    };
  }, [groups, tag]);

  // Autoplay the clip in view, pause the rest (same as the Clips screen).
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller || clips.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          const video = entry.target.querySelector('video');
          if (!(video instanceof HTMLVideoElement)) return;
          if (entry.isIntersecting) video.play().catch(() => {});
          else video.pause();
        });
      },
      { root: scroller, threshold: VISIBLE_THRESHOLD },
    );
    scroller.querySelectorAll('[data-clip-id]').forEach((c) => observer.observe(c));
    return () => observer.disconnect();
  }, [clips]);

  // Open on the requested clip once.
  useEffect(() => {
    if (didInitialScroll.current || clips.length === 0) return;
    const cards = scrollerRef.current?.querySelectorAll('[data-clip-id]');
    const target = cards?.[Math.max(0, Math.min(startIndex, cards.length - 1))];
    if (target instanceof HTMLElement) target.scrollIntoView();
    didInitialScroll.current = true;
  }, [clips, startIndex]);

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div className={`screen clips-feed ${device}`} role="dialog" aria-modal="true">
      <button className="clips-back" onClick={onClose} aria-label="Close">
        <X size={16} /> Close
      </button>
      <div className="clips-scroll" ref={scrollerRef} role="feed" aria-label="Highlights">
        {clips.map((clip, i) => (
          <ClipCard
            key={clip.id}
            clip={clip}
            index={i}
            muted={muted}
            onToggleMute={() => setMuted((m) => !m)}
            onWatchFull={() => {}}
            onOpenTitle={() => {}}
            surface="highlights_feed"
          />
        ))}
      </div>
    </div>
  );
}
