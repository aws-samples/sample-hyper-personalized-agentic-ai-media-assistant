import {
  Navigate,
  useLocation,
  useNavigate,
  useParams,
  type Location,
} from 'react-router-dom';
import { byId } from '../data/titles';
import { gameById } from '../data/sports';
import { joinOffsetForGame } from '../lib/sportsClient';
import type { LiveGame } from '../data/types';
import { TitleDetail } from './TitleDetail/TitleDetail';
import { VodPlayer } from './VodPlayer/VodPlayer';
import { SportsEntry } from './SportsEntry/SportsEntry';
import { SportsPlayer } from './SportsPlayer/SportsPlayer';
import { detailFromTitle, type DetailItem } from './TitleDetail/detail';

/**
 * Route wrappers for the content overlays. Each resolves its subject from
 * router `state` (passed by the launcher, so live/API items don't need a
 * refetch) and falls back to resolving by `:id` from the mock catalog / games
 * for deep links and refreshes.
 */

interface NavState {
  item?: DetailItem;
  game?: LiveGame;
  /** Video-seconds offset to enter a live game at (e.g. "Join now" → 125:47). */
  startAt?: number;
  background?: Location;
}

/** Close = go back to the launching screen, or Home on a direct visit. */
function useCloser() {
  const navigate = useNavigate();
  const { state } = useLocation();
  const hasBackground = Boolean((state as NavState | null)?.background);
  return () => (hasBackground ? navigate(-1) : navigate('/home'));
}

export function TitleRoute() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { state } = useLocation() as { state: NavState | null };
  const close = useCloser();
  const item = state?.item ?? (id ? detailFromTitle(byId(id)) : null);
  if (!item) return <Navigate to="/home" replace />;
  const onPlay = () =>
    navigate(`/watch/${item.id ?? id ?? 'x'}`, {
      state: { item, background: state?.background },
    });
  return <TitleDetail item={item} onClose={close} onPlay={onPlay} />;
}

export function WatchRoute() {
  const { id } = useParams();
  const { state } = useLocation() as { state: NavState | null };
  const close = useCloser();
  const item = state?.item ?? (id ? detailFromTitle(byId(id)) : null);
  if (!item) return <Navigate to="/home" replace />;
  return <VodPlayer item={item} onClose={close} />;
}

export function LiveRoute() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { state } = useLocation() as { state: NavState | null };
  const close = useCloser();
  const game = state?.game ?? (id ? gameById(id) : null);
  if (!game) return <Navigate to="/home" replace />;
  const onJoinLive = () =>
    navigate(`/watch/live/${game.id}`, {
      state: { game, startAt: joinOffsetForGame(game), background: state?.background },
    });
  return <SportsEntry game={game} onClose={close} onJoinLive={onJoinLive} />;
}

export function LiveWatchRoute() {
  const { id } = useParams();
  const { state } = useLocation() as { state: NavState | null };
  const close = useCloser();
  const game = state?.game ?? (id ? gameById(id) : null);
  if (!game) return <Navigate to="/home" replace />;
  return <SportsPlayer game={game} onClose={close} startAt={state?.startAt} />;
}
