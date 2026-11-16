import { useEffect, useState } from 'react';
import { getCarousels, type Carousel } from './api';

export type CarouselsState = 'loading' | 'ready' | 'error';

/**
 * Fetch a profile's carousels once and share them across the Home hero + rows.
 * No-ops (stays empty) when userId/profileSlug are absent (e.g. mock personas).
 */
export function useCarousels(userId?: string | null, profileSlug?: string) {
  const [carousels, setCarousels] = useState<Carousel[]>([]);
  const [state, setState] = useState<CarouselsState>('loading');

  useEffect(() => {
    if (!userId || !profileSlug) {
      setCarousels([]);
      setState('ready');
      return;
    }
    let active = true;
    setState('loading');
    getCarousels(userId, profileSlug)
      .then((c) => {
        if (!active) return;
        setCarousels(c);
        setState('ready');
      })
      .catch(() => active && setState('error'));
    return () => {
      active = false;
    };
  }, [userId, profileSlug]);

  return { carousels, state };
}
