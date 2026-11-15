import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useDevice } from '../../components/device';
import {
  followedHighlightClips,
  getClipFeed,
  isHighlightCard,
  refillClipFeed,
  startClipFeed,
  usesClipFeedApi,
  type CardReport,
} from '../../lib/clipsClient';
import { byId } from '../../data/titles';
import { useActiveProfile } from '../../lib/household';
import { useFollowed } from '../../lib/followed';
import { NO_PROFILE } from '../../data/profiles';
import type { Clip } from '../../data/types';
import { track } from '../../lib/events';
import { useContentNav } from '../../lib/contentNav';
import { detailFromMovie, detailFromTitle } from '../TitleDetail/detail';
import { ClipCard } from './ClipCard';
import type { CardFeedback } from './FeedbackButtons';
import { PosterCard } from './PosterCard';
import './Clips.css';

/** Fraction of a clip that must be on-screen before it becomes the active one. */
const VISIBLE_THRESHOLD = 0.7;
/** Refill when this many unseen cards (or fewer) are left after the one in view. */
const REFILL_AT = 3;

interface Report {
  dwellMs: number;
  feedback: CardFeedback;
  /** Changed since the last refill request. */
  dirty: boolean;
}

export function Clips() {
  const device = useDevice();
  const navigate = useNavigate();
  const { clipId } = useParams();
  const profile = useActiveProfile() ?? NO_PROFILE;
  // Household members get the personalized feed API; other profiles the static feed.
  const memberId = usesClipFeedApi(profile.backendProfile) ? profile.backendProfile : null;
  // Followed players' (or team's) match highlights lead a member's feed.
  const followed = useFollowed(memberId);

  const scrollerRef = useRef<HTMLDivElement>(null);
  const didInitialScroll = useRef(false);
  const [clips, setClips] = useState<Clip[]>([]);
  const [loadError, setLoadError] = useState(false);
  const [muted, setMuted] = useState(true); // start muted so autoplay is allowed
  // The clip currently in view, tracked by its stable id (not array index) so
  // it survives feed refills.
  const [activeClipId, setActiveClipId] = useState<string | null>(null);
  const { openTitle } = useContentNav();

  // Card reports (dwell + feedback) for the feed API, keyed by card id.
  const reports = useRef(new Map<string, Report>());
  const [feedbackById, setFeedbackById] = useState<Record<string, CardFeedback>>({});
  const activeSince = useRef<{ id: string; at: number } | null>(null);
  const refilling = useRef(false);
  const clipsRef = useRef<Clip[]>([]);
  clipsRef.current = clips;

  // Load the first page (a member's waits for its follows, so it loads once).
  useEffect(() => {
    if (memberId && !followed.ready) return;
    let cancelled = false;
    reports.current.clear();
    setFeedbackById({});
    setClips([]);
    const load = memberId
      ? Promise.all([
          followedHighlightClips(followed).catch(() => []),
          startClipFeed(memberId).then((p) => p.cards),
        ]).then(([highlights, cards]) => [...highlights, ...cards])
      : getClipFeed();
    load.then((c) => !cancelled && setClips(c)).catch(() => !cancelled && setLoadError(true));
    return () => {
      cancelled = true;
    };
  }, [memberId, followed]);

  /** Close the running dwell timer of the card in view into its report. */
  const settleDwell = useCallback(() => {
    const a = activeSince.current;
    if (!a) return;
    const now = Date.now();
    const r = reports.current.get(a.id) ?? { dwellMs: 0, feedback: 'none', dirty: false };
    r.dwellMs += now - a.at;
    r.dirty = true;
    reports.current.set(a.id, r);
    activeSince.current = { id: a.id, at: now };
  }, []);

  /** Ask the feed API for the next cards; keep `keep` of the queued ones. */
  const refill = useCallback(async () => {
    if (!memberId || refilling.current) return;
    const list = clipsRef.current;
    const at = list.findIndex((c) => c.id === activeSince.current?.id);
    const queued = list.slice(at + 1).filter((c) => !isHighlightCard(c)).map((c) => c.id);
    settleDwell();
    // Followed highlights are not feed API cards, so they are never reported.
    const highlightIds = new Set(list.filter(isHighlightCard).map((c) => c.id));
    const impressions: CardReport[] = [];
    reports.current.forEach((r, id) => {
      if (r.dirty && !highlightIds.has(id)) impressions.push({ item_id: id, dwell_ms: Math.round(r.dwellMs), feedback: r.feedback });
    });
    refilling.current = true;
    try {
      const page = await refillClipFeed(memberId, queued, impressions);
      impressions.forEach((i) => {
        const r = reports.current.get(i.item_id);
        if (r) r.dirty = false;
      });
      setClips((cur) => {
        const idx = cur.findIndex((c) => c.id === activeSince.current?.id);
        const head = cur.slice(0, idx + 1);
        const kept = cur.slice(idx + 1, idx + 1 + page.keep);
        const seen = new Set([...head, ...kept].map((c) => c.id));
        return [...head, ...kept, ...page.cards.filter((c) => !seen.has(c.id))];
      });
    } catch {
      /* keep the current queue; the next swipe retries */
    } finally {
      refilling.current = false;
    }
  }, [memberId, settleDwell]);

  // Track the card in view: dwell per card, and refill when the queue runs low.
  useEffect(() => {
    if (!activeClipId) return;
    settleDwell();
    activeSince.current = { id: activeClipId, at: Date.now() };
    const idx = clips.findIndex((c) => c.id === activeClipId);
    if (memberId && idx >= 0 && clips.length - 1 - idx <= REFILL_AT) void refill();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeClipId]);

  const onFeedback = useCallback(
    (clip: Clip, value: CardFeedback) => {
      settleDwell();
      const r = reports.current.get(clip.id) ?? { dwellMs: 0, feedback: 'none', dirty: false };
      r.feedback = value;
      r.dirty = true;
      reports.current.set(clip.id, r);
      setFeedbackById((m) => ({ ...m, [clip.id]: value }));
      if (value !== 'none') {
        const p: Record<string, string> = { item_id: clip.id, value, content_type: clip.kind };
        if (clip.kind === 'title_card') p.parent_item_id = clip.id;
        track('feedback', 'clips_feed', p);
      }
      // "More like this" / "Less like this" replace the rest of the queue now.
      if (value === 'more' || value === 'dislike') void refill();
    },
    [refill, settleDwell],
  );

  // Autoplay the clip in view, pause the rest.
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller || clips.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          const video = entry.target.querySelector('video');
          if (entry.isIntersecting) {
            // Identify the active clip by its stable id, not its position.
            const id = entry.target.getAttribute('data-clip-id');
            if (id) setActiveClipId(id);
            if (video instanceof HTMLVideoElement) video.play().catch(() => {});
          } else if (video instanceof HTMLVideoElement) {
            video.pause();
          }
        });
      },
      { root: scroller, threshold: VISIBLE_THRESHOLD },
    );

    const cards = scroller.querySelectorAll('[data-clip-id]');
    cards.forEach((c) => observer.observe(c));
    return () => observer.disconnect();
  }, [clips]);

  // Deep link / refresh: on first load, jump to the clip named in the URL
  // (/clips/:clipId) when it is in the feed. Runs once so it never fights the
  // user's scrolling.
  useEffect(() => {
    if (didInitialScroll.current || clips.length === 0) return;
    if (clipId) {
      const el = scrollerRef.current?.querySelector(`[data-clip-id="${CSS.escape(clipId)}"]`);
      if (el instanceof HTMLElement) el.scrollIntoView();
    }
    didInitialScroll.current = true;
  }, [clips, clipId]);

  // Sync the URL to the clip in view (replace, so scrolling doesn't spam history).
  useEffect(() => {
    if (!didInitialScroll.current || !activeClipId) return;
    if (activeClipId !== clipId) {
      navigate(`/clips/${activeClipId}`, { replace: true });
    }
  }, [activeClipId, clipId, navigate]);

  // Keyboard / D-pad navigation (works today on desktop, ready for TV later).
  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    const scroller = scrollerRef.current;
    if (!scroller) return;
    const page = scroller.clientHeight;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      scroller.scrollBy({ top: page, behavior: 'smooth' });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      scroller.scrollBy({ top: -page, behavior: 'smooth' });
    }
  }, []);

  const openClipTitle = useCallback(
    (clip: Clip) => {
      if (clip.movie) openTitle(detailFromMovie(clip.movie));
      else if (clip.titleId) openTitle(detailFromTitle(byId(clip.titleId)));
    },
    [openTitle],
  );

  const nav = (
    <nav className="bottom-nav clips-nav">
      {[
        { i: '⌂', l: 'Home', to: '/home' },
        { i: '⚡', l: 'Clips', on: true },
        { i: '🔍', l: 'Search', to: '/search' },
        { i: profile.icon, l: 'Profile', to: '/home' },
      ].map((t) => (
        <button
          key={t.l}
          className={`bn-item ${t.on ? 'on' : ''}`}
          onClick={t.to ? () => navigate(t.to) : undefined}
        >
          <span className="bn-icon">{t.i}</span>
          <span className="bn-label">{t.l}</span>
        </button>
      ))}
    </nav>
  );

  return (
    <div className={`screen clips-feed ${device}`}>
      {device === 'tv' && (
        <button className="clips-back" onClick={() => navigate('/home')} aria-label="Back">
          ← Home
        </button>
      )}

      <div
        className="clips-scroll"
        ref={scrollerRef}
        tabIndex={0}
        onKeyDown={handleKeyDown}
        role="feed"
        aria-label="Clips"
        aria-busy={clips.length === 0 && !loadError}
        data-active-clip={activeClipId ?? undefined}
      >
        {loadError && <div className="clips-empty">Couldn't load clips.</div>}
        {!loadError && clips.length === 0 && <div className="clips-empty">Loading clips…</div>}
        {clips.map((clip, i) =>
          clip.kind === 'title_card' || !clip.video ? (
            <PosterCard
              key={clip.id}
              clip={clip}
              index={i}
              active={clip.id === activeClipId}
              feedback={feedbackById[clip.id] ?? 'none'}
              onFeedback={onFeedback}
              onWatchFull={openClipTitle}
            />
          ) : (
            <ClipCard
              key={clip.id}
              clip={clip}
              index={i}
              muted={muted}
              onToggleMute={() => setMuted((m) => !m)}
              onWatchFull={openClipTitle}
              onOpenTitle={openClipTitle}
              feedback={memberId ? (feedbackById[clip.id] ?? 'none') : undefined}
              onFeedback={memberId ? onFeedback : undefined}
            />
          ),
        )}
      </div>

      {device === 'mobile' && nav}
    </div>
  );
}
