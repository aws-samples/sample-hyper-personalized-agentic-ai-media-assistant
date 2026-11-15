import { useDevice } from '../../components/device';
import { MovieRail } from '../../components/MovieRail';
import type { Carousel, Movie } from '../../lib/api';
import type { CarouselsState } from '../../lib/useCarousels';

interface LiveRowsProps {
  carousels: Carousel[];
  state: CarouselsState;
  profileSlug: string;
  onSelect?: (movie: Movie) => void;
  onToggleMyList?: (movie: Movie) => void;
  myListIds?: Set<string>;
}

/** Home's rows for a persona with live backend data (one MovieRail per carousel). */
export function LiveRows({
  carousels,
  state,
  profileSlug,
  onSelect,
  onToggleMyList,
  myListIds,
}: LiveRowsProps) {
  const device = useDevice();

  if (state === 'loading') {
    return (
      <>
        {['a', 'b', 'c'].map((k) => (
          <section key={k} className={`rail ${device}`}>
            <div className="rail-head">
              <h3 className="rail-title">&nbsp;</h3>
            </div>
            <div className="rail-track">
              {Array.from({ length: 6 }).map((_, i) => (
                <div key={i} className="card portrait rail-skeleton" />
              ))}
            </div>
          </section>
        ))}
      </>
    );
  }

  if (state === 'error') {
    return <p className="rail-error">Couldn't load recommendations.</p>;
  }

  return (
    <>
      {carousels.map((c) => (
        <MovieRail
          key={c.key}
          title={c.displayTitle}
          movies={c.movies}
          resetKey={`${profileSlug}:${c.key}`}
          onSelect={onSelect}
          onToggleMyList={onToggleMyList}
          myListIds={myListIds}
        />
      ))}
    </>
  );
}
