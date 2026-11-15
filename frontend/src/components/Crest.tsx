import { LIVE_GAMES } from '../data/sports';

/**
 * Club crests for the demo's fictional teams, drawn inline (no image assets):
 * a shield in the club colour with its three-letter code. Unknown codes
 * render nothing, so callers can layer it over their own colour/text art.
 */
const hueByCode = new Map<string, number>(
  LIVE_GAMES.flatMap((g) => [
    [g.home.code, g.home.hue],
    [g.away.code, g.away.hue],
  ]),
);

export function Crest({ code, className }: { code: string; className?: string }) {
  const hue = hueByCode.get(code.toUpperCase());
  if (hue == null) return null;
  return (
    <svg className={className} viewBox="0 0 64 72" role="img" aria-label={`${code} crest`}>
      <path
        d="M32 2 L60 10 V34 C60 52 47 64 32 70 C17 64 4 52 4 34 V10 Z"
        fill={`hsl(${hue} 62% 42%)`}
        stroke="#fff"
        strokeWidth="3"
      />
      <path d="M8 30 H56" stroke="rgba(255,255,255,0.35)" strokeWidth="2" />
      <text
        x="32"
        y="46"
        textAnchor="middle"
        fontFamily="system-ui, sans-serif"
        fontWeight="800"
        fontSize="17"
        fill="#fff"
      >
        {code.toUpperCase()}
      </text>
    </svg>
  );
}

/** The competition badge (text only: the competition is fictional). */
export function LeagueBadge({ className, label }: { className?: string; label: string }) {
  return <span className={className}>{label}</span>;
}
