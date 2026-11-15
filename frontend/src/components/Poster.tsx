import type { CSSProperties } from 'react';
import './Poster.css';

interface PosterProps {
  hueA: number;
  hueB: number;
  label?: string;
  kicker?: string;
  ratio?: '2/3' | '16/9' | '1/1' | '3/4';
  rounded?: number;
  style?: CSSProperties;
  showLabel?: boolean;
}

/** Deterministic gradient artwork stand-in.
 *  Swap for real posters later by dropping <img> in place. */
export function Poster({
  hueA,
  hueB,
  label,
  kicker,
  ratio = '2/3',
  rounded = 12,
  style,
  showLabel = true,
}: PosterProps) {
  const bg = `radial-gradient(120% 120% at 20% 10%, hsl(${hueA} 70% 42%) 0%, hsl(${hueA} 65% 22%) 42%, hsl(${hueB} 60% 14%) 100%)`;
  return (
    <div
      className="poster"
      style={{
        aspectRatio: ratio,
        background: bg,
        borderRadius: rounded,
        ...style,
      }}
    >
      <div className="poster-sheen" />
      {showLabel && label && (
        <div className="poster-meta">
          {kicker && <span className="poster-kicker">{kicker}</span>}
          <span className="poster-title">{label}</span>
        </div>
      )}
    </div>
  );
}
