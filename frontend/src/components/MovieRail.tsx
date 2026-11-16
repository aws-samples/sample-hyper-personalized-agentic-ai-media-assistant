import { Check, Plus } from 'lucide-react';
import { useDevice } from './device';
import { RailTrack } from './RailTrack';
import { StarRating } from './StarRating';
import type { Movie } from '../lib/api';
import './Rail.css'; // shared rail/card/spacing styles
import './MovieRail.css';

interface MovieRailProps {
  title: string;
  context?: string;
  movies: Movie[];
  resetKey?: string | number;
  onSelect?: (movie: Movie) => void;
  /** When provided, each card shows a ＋/✓ My List toggle. */
  onToggleMyList?: (movie: Movie) => void;
  /** Set of title_ids currently in My List (drives the ✓ state). */
  myListIds?: Set<string>;
}

/** A content rail rendered from live API movies (real poster images). */
export function MovieRail({
  title,
  context,
  movies,
  resetKey,
  onSelect,
  onToggleMyList,
  myListIds,
}: MovieRailProps) {
  const device = useDevice();
  return (
    <section className={`rail ${device}`}>
      <div className="rail-head">
        <h3 className="rail-title">{title}</h3>
        {context && <span className="rail-context">◆ {context}</span>}
      </div>
      <RailTrack resetKey={resetKey}>
        {movies.map((m) => {
          const inList = myListIds?.has(m.id) ?? false;
          return (
            <button key={m.id} className="card portrait" onClick={() => onSelect?.(m)}>
              <div className="card-art">
                {m.posterUrl ? (
                  <img className="poster-img" src={m.posterUrl} alt={m.title} loading="lazy" />
                ) : (
                  <div className="poster-fallback" />
                )}
                {onToggleMyList && (
                  <span
                    className={`card-add ${inList ? 'on' : ''}`}
                    role="button"
                    tabIndex={0}
                    aria-label={inList ? `Remove ${m.title} from My List` : `Add ${m.title} to My List`}
                    aria-pressed={inList}
                    title={inList ? 'Remove from My List' : 'Add to My List'}
                    onClick={(e) => {
                      e.stopPropagation();
                      onToggleMyList(m);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        e.stopPropagation();
                        onToggleMyList(m);
                      }
                    }}
                  >
                    {inList ? <Check size={16} strokeWidth={2.5} /> : <Plus size={16} strokeWidth={2.5} />}
                  </span>
                )}
              </div>
              <div className="card-meta">
                <span className="card-title">{m.title}</span>
                <span className="card-sub">
                  {m.rating ? <StarRating value={m.rating} /> : null}
                  {m.rating && m.genres[0] ? <span className="card-sub-sep">·</span> : null}
                  {m.genres[0] ?? ''}
                </span>
              </div>
            </button>
          );
        })}
      </RailTrack>
    </section>
  );
}
