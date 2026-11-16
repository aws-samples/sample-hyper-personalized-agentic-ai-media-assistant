import { Heart, ThumbsDown, ThumbsUp } from 'lucide-react';
import type { Clip } from '../../data/types';

/** A card's current explicit feedback ('none' = no feedback / un-liked). */
export type CardFeedback = 'none' | 'like' | 'more' | 'dislike';

interface FeedbackButtonsProps {
  clip: Clip;
  value: CardFeedback;
  onFeedback: (clip: Clip, value: CardFeedback) => void;
}

/**
 * Like / More like this / Less like this. "More" and "Less" reshape the rest of
 * the feed right away (the next cards are replaced); a like is a strong
 * positive that applies on the next refill.
 */
export function FeedbackButtons({ clip, value, onFeedback }: FeedbackButtonsProps) {
  const toggle = (v: Exclude<CardFeedback, 'none'>) => onFeedback(clip, value === v ? 'none' : v);
  return (
    <>
      <button
        className={`clip-action ${value === 'like' ? 'liked' : ''}`}
        onClick={() => toggle('like')}
        aria-pressed={value === 'like'}
        aria-label="Like"
      >
        <Heart className="clip-ic" fill={value === 'like' ? 'currentColor' : 'none'} />
        <span>{clip.likes || 'Like'}</span>
      </button>
      <button
        className={`clip-action ${value === 'more' ? 'chosen' : ''}`}
        onClick={() => toggle('more')}
        aria-pressed={value === 'more'}
        aria-label="More like this"
      >
        <ThumbsUp className="clip-ic" fill={value === 'more' ? 'currentColor' : 'none'} />
        <span>More</span>
      </button>
      <button
        className={`clip-action ${value === 'dislike' ? 'chosen' : ''}`}
        onClick={() => toggle('dislike')}
        aria-pressed={value === 'dislike'}
        aria-label="Less like this"
      >
        <ThumbsDown className="clip-ic" fill={value === 'dislike' ? 'currentColor' : 'none'} />
        <span>Less</span>
      </button>
    </>
  );
}
