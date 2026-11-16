import { useEffect, useState } from 'react';
import { getHome, type HomePage } from './api';

export type HomeState = 'loading' | 'ready' | 'error';

/**
 * A household member's landing page (GET /home): hero + rails from the
 * recommendation cache. No-ops (stays empty) without a profile_id.
 */
export function useHome(profileId?: string) {
  const [page, setPage] = useState<HomePage | null>(null);
  const [state, setState] = useState<HomeState>('loading');

  useEffect(() => {
    if (!profileId) {
      setPage(null);
      setState('ready');
      return;
    }
    let active = true;
    setState('loading');
    getHome(profileId)
      .then((p) => {
        if (!active) return;
        setPage(p);
        setState('ready');
      })
      .catch(() => active && setState('error'));
    return () => {
      active = false;
    };
  }, [profileId]);

  return { hero: page?.hero ?? null, rails: page?.rails ?? [], state };
}
