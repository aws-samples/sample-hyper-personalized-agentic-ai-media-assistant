import { useEffect, useRef, useState } from 'react';
import { Play } from 'lucide-react';
import type { Clip } from '../../data/types';
import { track } from '../../lib/events';
import { useClockPlaybackEvents, type PlaybackItem } from '../../lib/usePlaybackEvents';
import { FeedbackButtons, type CardFeedback } from './FeedbackButtons';

/** A poster card counts as fully watched after this long on screen (the feed's card cap). */
export const TITLE_CARD_MS = 120_000;
const TICK_MS = 250;

interface PosterCardProps {
  clip: Clip;
  index: number;
  /** The card in view: its clock runs and it reports a view. */
  active: boolean;
  feedback: CardFeedback;
  onFeedback: (clip: Clip, value: CardFeedback) => void;
  onWatchFull: (clip: Clip) => void;
}

/**
 * A catalog title in the clips feed, shown as a static poster -- the same
 * approach as the movie player's poster + running clock. While the card is in
 * view a clock runs up to TITLE_CARD_MS and reports `title_card` views (the
 * title as `parent_item_id`), so dwell on the card shapes taste like a clip,
 * without counting as watching the film.
 */
export function PosterCard({ clip, index, active, feedback, onFeedback, onWatchFull }: PosterCardProps) {
  const [positionMs, setPositionMs] = useState(0);
  const shown = useRef(false);

  useEffect(() => {
    if (!active) return;
    if (!shown.current) {
      shown.current = true;
      track('impression', 'clips_feed', { item_id: clip.id, content_type: 'title_card', rank: index + 1 });
    }
    const t = window.setInterval(() => setPositionMs((p) => Math.min(TITLE_CARD_MS, p + TICK_MS)), TICK_MS);
    return () => window.clearInterval(t);
  }, [active, clip.id, index]);

  const item: PlaybackItem | null = active
    ? { itemId: clip.id, contentType: 'title_card', surface: 'clips_feed', parentItemId: clip.id }
    : null;
  useClockPlaybackEvents(item, { playing: active && positionMs < TITLE_CARD_MS, positionMs, durationMs: TITLE_CARD_MS });

  return (
    <section className="clip clip-title-card" data-clip-id={clip.id} data-index={index} aria-label={`${clip.title}, card ${index + 1}`}>
      <div className="clip-bg">
        {clip.poster ? (
          <img className="clip-poster-blur" src={clip.poster} alt="" aria-hidden="true" />
        ) : (
          <div
            className="clip-poster-blur"
            style={{ background: `linear-gradient(145deg, hsl(${clip.hueA} 65% 46%), hsl(${clip.hueB} 60% 32%))` }}
          />
        )}
      </div>
      {clip.poster && <img className="clip-poster" src={clip.poster} alt={`${clip.title} poster`} />}
      <div className="clip-scrim" />

      <div className="clip-rail">
        <FeedbackButtons clip={clip} value={feedback} onFeedback={onFeedback} />
      </div>

      <div className="clip-info">
        {clip.source && <span className="clip-source">{clip.source}</span>}
        <h2 className="clip-title">{clip.title}</h2>
        {clip.genres && clip.genres.length > 0 && (
          <div className="clip-genres">
            {clip.genres.map((g) => (
              <span key={g} className="clip-genre">
                {g}
              </span>
            ))}
          </div>
        )}
        {clip.movie?.plot && <p className="clip-plot">{clip.movie.plot}</p>}
        {clip.explanation && <span className="clip-why">{clip.explanation}</span>}
        <button className="clip-watch" onClick={() => onWatchFull(clip)}>
          <Play className="clip-watch-ic" size={15} fill="currentColor" /> Watch Full
        </button>
      </div>

      <div className="clip-progress">
        <span className="clip-progress-fill" style={{ width: `${(positionMs / TITLE_CARD_MS) * 100}%` }} />
      </div>
    </section>
  );
}
