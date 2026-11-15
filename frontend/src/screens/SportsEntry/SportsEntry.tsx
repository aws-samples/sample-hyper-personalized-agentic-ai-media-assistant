import { useCallback, useEffect, useRef, useState, type WheelEvent } from 'react';
import { Play, RotateCcw, X, Users, ListVideo, ChevronLeft, ChevronRight } from 'lucide-react';
import { Poster } from '../../components/Poster';
import { SplitMatchArt } from '../../components/SplitMatchArt';
import { useDevice } from '../../components/device';
import { getHighlights, matchStateConfigured } from '../../lib/sportsStateClient';
import { useActiveFollowed } from '../../lib/followed';
import {
  groupHighlightsToClips,
  highlightLabel,
  type HighlightClipGroup,
} from '../../lib/highlightClips';
import { joinOffsetForGame } from '../../lib/sportsClient';
import { CatchUpPlayer } from '../SportsPlayer/CatchUpPlayer';
import type { LiveGame, KeyPlay } from '../../data/types';
import './SportsEntry.css';

interface SportsEntryProps {
  game: LiveGame;
  onClose: () => void;
  /** Jump into the live player. */
  onJoinLive: () => void;
  /** Start from the beginning (same player for now). */
  onWatchFromStart?: () => void;
}

/**
 * Compact pre-play modal (Prime Video "live event" / Peacock "Live in Browse"
 * style): a game preview with a score bug, an unmissable Join Live CTA, a
 * one-tap AI catch-up, and a Key Moments strip. Deliberately small — the real
 * catch-up/stats live inside the player.
 */
export function SportsEntry({ game, onClose, onJoinLive, onWatchFromStart }: SportsEntryProps) {
  const followed = useActiveFollowed();
  const [groups, setGroups] = useState<HighlightClipGroup[]>([]);
  // Opens the shared player: single moment (one clip) or the full reel.
  const [player, setPlayer] = useState<{ start: number; single: boolean } | null>(null);
  // Don't flash the mock gradient plays while the real highlights are still
  // loading — show skeletons.
  const [soccerLoaded, setSoccerLoaded] = useState(false);
  const momentsRef = useRef<HTMLDivElement>(null);

  const isSoccerLive = game.sport === 'soccer' && matchStateConfigured();
  const hasMoments = isSoccerLive && groups.length > 0;
  const soccerPending = isSoccerLive && !soccerLoaded;

  // Load the real highlights (grouped to clips) for the soccer match, only up
  // to the "Join Live" point so the entry never shows a moment from after it
  // (spoiler-safe). Moments from before the broadcast have no footage and are
  // not playable, so they don't form groups.
  useEffect(() => {
    if (!isSoccerLive) return;
    let active = true;
    const liveNow = joinOffsetForGame(game);
    getHighlights().then((hl) => {
      if (!active) return;
      setGroups(groupHighlightsToClips(hl.filter((h) => h.video_s <= liveNow)));
      setSoccerLoaded(true);
    });
    return () => {
      active = false;
    };
  }, [isSoccerLive, game]);

  // Let a mouse wheel scroll the horizontal Key Moments rail (desktop/mouse).
  const onMomentsWheel = (e: WheelEvent<HTMLDivElement>) => {
    const el = momentsRef.current;
    if (!el || Math.abs(e.deltaY) < Math.abs(e.deltaX)) return;
    el.scrollLeft += e.deltaY;
  };

  // Netflix-style scroll arrows for the Key Moments rail (matches the home
  // carousels): shown on hover for TV/desktop, only when the rail overflows,
  // and hidden at each end. Mobile keeps native touch swipe (no arrows).
  const device = useDevice();
  const [railScrollable, setRailScrollable] = useState(false);
  const [railAtStart, setRailAtStart] = useState(true);
  const [railAtEnd, setRailAtEnd] = useState(true);
  const updateRail = useCallback(() => {
    const el = momentsRef.current;
    if (!el) return;
    setRailScrollable(el.scrollWidth - el.clientWidth > 2);
    setRailAtStart(el.scrollLeft <= 2);
    setRailAtEnd(el.scrollLeft + el.clientWidth >= el.scrollWidth - 2);
  }, []);
  const pageRail = (dir: 1 | -1) => {
    const el = momentsRef.current;
    if (el) el.scrollBy({ left: dir * el.clientWidth * 0.85, behavior: 'smooth' });
  };
  // Recompute when the rail mounts/resizes or its contents change (skeletons →
  // real moments), so the arrows appear/adjust once the row actually overflows.
  useEffect(() => {
    const el = momentsRef.current;
    if (!el) return;
    updateRail();
    const raf = requestAnimationFrame(updateRail); // after first paint
    const ro = new ResizeObserver(updateRail);
    ro.observe(el);
    window.addEventListener('resize', updateRail);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener('resize', updateRail);
    };
  }, [updateRail, groups, soccerPending]);
  const showRailArrows = device === 'tv' && railScrollable;

  const teamHue = (p: KeyPlay) => (p.team === 'home' ? game.home.hue : game.away.hue);

  return (
    <div className="sports-entry" role="dialog" aria-modal="true">
      <div className="se-backdrop" onClick={onClose} />

      {player && (
        <CatchUpPlayer
          groups={groups}
          startIndex={player.start}
          mode={player.single ? 'single' : 'reel'}
          orientation="horizontal"
          onGoLive={onJoinLive}
          onClose={() => setPlayer(null)}
        />
      )}

      <div className="se-modal">
        <button className="se-close" onClick={onClose} aria-label="Close">
          <X size={18} />
        </button>

        {/* Preview "screen" — reads as the live broadcast you're about to enter. */}
        <div className="se-preview">
          <SplitMatchArt game={game} />
          <span className="badge-live se-live">LIVE</span>
          <div className="se-scorebug">
            <span className="se-sb-score">
              <b>{game.home.score}</b>
              <span className="se-sb-dash">–</span>
              <b>{game.away.score}</b>
            </span>
            <span className="se-sb-clock">{game.clock}</span>
          </div>
          <button className="se-play" onClick={onJoinLive} aria-label="Join live">
            <Play size={28} fill="currentColor" />
          </button>
        </div>

        <div className="se-body">
          <div className="se-meta">
            <span className="se-league">{game.league}</span>
            <span className="se-viewers">
              <Users size={13} /> {game.viewers}
            </span>
          </div>

          <h2 className="se-title">
            {game.title ?? (
              <>
                {game.home.city} <span className="se-vs">vs</span> {game.away.city}
              </>
            )}
          </h2>
          <span className="se-status">
            {game.home.record} · {game.away.record} — {game.status}
          </span>

          {/* Primary action is unmistakable; catch-up + restart are secondary. */}
          <div className="se-actions">
            <button className="se-join" onClick={onJoinLive}>
              <Play size={18} fill="currentColor" /> Join Live
            </button>
            {hasMoments && (
              <button className="se-catchup-live" onClick={() => setPlayer({ start: 0, single: false })}>
                <ListVideo size={17} /> Get Up to Speed
                <span className="se-catchup-sub">Key plays, then straight to live</span>
              </button>
            )}
            <div className="se-actions-row">
              <button className="se-secondary" onClick={onWatchFromStart ?? onJoinLive}>
                <RotateCcw size={16} /> From start
              </button>
            </div>
          </div>

          <div className="se-moments">
            <div className="se-moments-head">
              <h3>Key Moments</h3>
              <span>{hasMoments ? 'Tap to watch' : 'Tap to jump in'}</span>
            </div>
            <div className="se-moments-rail">
              {showRailArrows && !railAtStart && (
                <button className="se-rail-arrow left" aria-label="Scroll left" onClick={() => pageRail(-1)}>
                  <ChevronLeft />
                </button>
              )}
              <div className="se-moments-track" ref={momentsRef} onWheel={onMomentsWheel} onScroll={updateRail}>
              {hasMoments
                ? groups.map((g) => {
                    const hue = g.lead.type === 'goal' ? game.home.hue : game.away.hue;
                    const isFav = g.members.some((m) => followed.isPlayer(m.player));
                    return (
                      <button
                        key={g.n}
                        className="se-moment"
                        onClick={() => setPlayer({ start: g.n - 1, single: true })}
                      >
                        <div className="se-moment-art">
                          {g.lead.thumbnail_url ? (
                            <img
                              className="se-moment-img"
                              src={g.lead.thumbnail_url}
                              alt={highlightLabel(g)}
                              loading="lazy"
                            />
                          ) : (
                            <Poster hueA={hue} hueB={hue + 30} ratio="16/9" rounded={8} showLabel={false} />
                          )}
                          <span className="se-moment-scrim" />
                          <span className={`se-moment-clock ${g.lead.type}`}>
                            {g.lead.minute.replace(/\.$/, '')}'
                          </span>
                          {isFav && <span className="se-moment-star" title="Your player">★</span>}
                          <span className="se-moment-play">
                            <Play size={16} fill="currentColor" />
                          </span>
                        </div>
                        <span className="se-moment-label">{highlightLabel(g)}</span>
                      </button>
                    );
                  })
                : soccerPending
                ? Array.from({ length: 5 }).map((_, i) => (
                    <div key={i} className="se-moment se-moment-skeleton" aria-hidden="true">
                      <div className="se-moment-art">
                        <div className="se-skel-box" />
                      </div>
                      <span className="se-skel-line" />
                    </div>
                  ))
                : game.keyPlays.map((p, i) => (
                    <button key={i} className="se-moment" onClick={onJoinLive}>
                      <div className="se-moment-art">
                        <Poster hueA={teamHue(p)} hueB={teamHue(p) + 30} ratio="16/9" rounded={8} showLabel={false} />
                        <span className={`se-moment-clock ${p.kind}`}>{p.clock}</span>
                        <span className="se-moment-play">
                          <Play size={16} fill="currentColor" />
                        </span>
                      </div>
                      <span className="se-moment-label">{p.label}</span>
                    </button>
                  ))}
              </div>
              {showRailArrows && !railAtEnd && (
                <button className="se-rail-arrow right" aria-label="Scroll right" onClick={() => pageRail(1)}>
                  <ChevronRight />
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
