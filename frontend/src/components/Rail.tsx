import { useDevice } from './device';
import { Poster } from './Poster';
import { RailTrack } from './RailTrack';
import type { Title } from '../data/types';
import './Rail.css';

interface RailProps {
  title: string;
  context?: string;
  items: Title[];
  /** landscape 16/9 cards (continue watching) vs portrait 2/3 (catalog) */
  shape?: 'landscape' | 'portrait';
  onSelect?: (item: Title) => void;
}

export function Rail({ title, context, items, shape = 'landscape', onSelect }: RailProps) {
  const device = useDevice();
  const ratio = shape === 'landscape' ? '16/9' : '2/3';

  return (
    <section className={`rail ${device}`}>
      <div className="rail-head">
        <h3 className="rail-title">{title}</h3>
        {context && <span className="rail-context">◆ {context}</span>}
      </div>
      <RailTrack>
        {items.map((t) => {
          const isLive = t.kind === 'live';
          return (
            <button key={t.id + title} className={`card ${shape}`} onClick={() => onSelect?.(t)}>
              <div className="card-art">
                <Poster
                  hueA={t.hueA}
                  hueB={t.hueB}
                  ratio={ratio as '16/9' | '2/3'}
                  rounded={10}
                  label={t.name}
                  kicker={isLive ? undefined : t.genres[0]}
                  showLabel={shape === 'landscape' && !isLive}
                />
                {isLive && (
                  <div className="card-live">
                    <span className="card-live-top">
                      {t.isLive ? (
                        <span className="badge-live">LIVE</span>
                      ) : (
                        <span className="card-upcoming">UPCOMING</span>
                      )}
                      <span className="card-live-clock">{t.liveClock}</span>
                    </span>
                    <span className="card-live-bottom">
                      <span className="card-live-name">{t.name}</span>
                      <span className="card-live-score">{t.liveScore}</span>
                    </span>
                  </div>
                )}
                {typeof t.progress === 'number' && (
                  <div className="card-progress">
                    <span style={{ width: `${t.progress * 100}%` }} />
                  </div>
                )}
              </div>
              {shape === 'portrait' && !isLive && (
                <div className="card-meta">
                  <span className="card-title">{t.name}</span>
                  <span className="card-sub">
                    {t.match}% match · {t.maturity} · {t.runtime}
                  </span>
                </div>
              )}
            </button>
          );
        })}
      </RailTrack>
    </section>
  );
}
