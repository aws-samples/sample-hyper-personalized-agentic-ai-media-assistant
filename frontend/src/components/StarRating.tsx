import './StarRating.css';

interface StarRatingProps {
  /** The score as shown, e.g. 7.8. */
  value: number | string;
  /** Out-of value appended as "/10". Off by default to keep cards compact. */
  showScale?: boolean;
  className?: string;
}

/**
 * A title's rating as a neutral star and score: ★ 7.8 (or ★ 7.8/10).
 *
 * Deliberately unbranded: the catalog ratings are sample data, so no ratings
 * provider's logo is shown. Every title rating in the app renders through this
 * component (MovieRail poster cards and the TitleDetail sheet), so a branded
 * badge can be swapped in here if a deployment licenses one.
 *
 * Sized in `em`, so it follows the font-size of the row it sits in.
 */
export function StarRating({ value, showScale = false, className }: StarRatingProps) {
  const label = `Rating ${value}${showScale ? ' out of 10' : ''}`;
  return (
    <span className={`star-rating ${className ?? ''}`.trim()} aria-label={label} title={label}>
      <span className="star-rating-star" aria-hidden="true">
        ★
      </span>
      <span className="star-rating-score">
        {value}
        {showScale && <span className="star-rating-scale">/10</span>}
      </span>
    </span>
  );
}
