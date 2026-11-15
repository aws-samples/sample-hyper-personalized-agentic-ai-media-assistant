import { Crest, LeagueBadge } from './Crest';
import type { LiveGame } from '../data/types';
import './SplitMatchArt.css';

const grad = (hue: number) => `linear-gradient(145deg, hsl(${hue} 65% 46%), hsl(${hue + 20} 60% 30%))`;

/**
 * Broadcast-style match card art: two club-colour halves that meet on a single
 * clean diagonal seam, each showing its crest, with a bottom scrim and the
 * competition name. The crests ARE the art — callers should not also show crests in
 * a scorebug. Shared by the Home "Live Now" card and the SportsEntry preview.
 */
export function SplitMatchArt({ game, showLeague = true }: { game: LiveGame; showLeague?: boolean }) {
  return (
    <div className="match-art">
      {/* Full-size halves clipped by complementary polygons that share the exact
          same diagonal edge (56% top → 44% bottom), so the seam is one line. */}
      <div className="match-art-half home" style={{ background: grad(game.home.hue) }} />
      <div className="match-art-half away" style={{ background: grad(game.away.hue) }} />
      <Crest code={game.home.code} className="match-art-crest home" />
      <Crest code={game.away.code} className="match-art-crest away" />
      <div className="match-art-scrim" />
      {showLeague && <LeagueBadge className="match-art-league" label={game.league} />}
    </div>
  );
}
