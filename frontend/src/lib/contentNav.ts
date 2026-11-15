import { useCallback } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import type { DetailItem } from '../screens/TitleDetail/detail';
import type { LiveGame } from '../data/types';

/**
 * Openers for the content overlay routes. Each navigation carries the current
 * location as `background` (so the launching screen stays rendered behind the
 * overlay) and the resolved item/game as `state` (so no refetch is needed).
 */
export function useContentNav() {
  const navigate = useNavigate();
  const location = useLocation();

  const openTitle = useCallback(
    (item: DetailItem) =>
      navigate(`/title/${item.id ?? 'x'}`, { state: { item, background: location } }),
    [navigate, location],
  );

  const openWatch = useCallback(
    (item: DetailItem) =>
      navigate(`/watch/${item.id ?? 'x'}`, { state: { item, background: location } }),
    [navigate, location],
  );

  const openGame = useCallback(
    (game: LiveGame) =>
      navigate(`/live/${game.id}`, { state: { game, background: location } }),
    [navigate, location],
  );

  const openGameWatch = useCallback(
    (game: LiveGame, startAt?: number) =>
      navigate(`/watch/live/${game.id}`, { state: { game, startAt, background: location } }),
    [navigate, location],
  );

  return { openTitle, openWatch, openGame, openGameWatch };
}
