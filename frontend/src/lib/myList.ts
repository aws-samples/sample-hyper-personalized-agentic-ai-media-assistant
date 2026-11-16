import { useEffect, useSyncExternalStore } from 'react';
import {
  getList,
  saveToList,
  removeFromList,
  type Movie,
} from './api';
import { track } from './events';

/**
 * Shared "My List" store for the active (user, profile).
 *
 * A module-level store (mirroring theme.ts) rather than component state, so the
 * Home rail and the TitleDetail add/remove button — which live on different
 * routes — read and mutate the same list and stay in sync. Add/remove are
 * optimistic with rollback on API failure.
 */

type State = 'loading' | 'ready' | 'error';

interface Snapshot {
  key: string | null; // `${userId}:${profileSlug}` — identifies the loaded list
  userId: string | null;
  profileSlug: string | null;
  movies: Movie[];
  ids: Set<string>;
  state: State;
}

const EMPTY_IDS: Set<string> = new Set();
let snap: Snapshot = {
  key: null,
  userId: null,
  profileSlug: null,
  movies: [],
  ids: EMPTY_IDS,
  state: 'ready',
};

const listeners = new Set<() => void>();
let fetchToken = 0;

function set(next: Partial<Snapshot>) {
  snap = { ...snap, ...next };
  listeners.forEach((l) => l());
}

function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

function getSnapshot() {
  return snap;
}

/** Load (once) the list for a user+profile. No-ops for absent/mock profiles. */
export function loadMyList(userId?: string | null, profileSlug?: string) {
  const key = userId && profileSlug ? `${userId}:${profileSlug}` : null;
  if (key === snap.key) return; // already loaded/loading this list

  if (!key) {
    set({ key: null, userId: null, profileSlug: null, movies: [], ids: EMPTY_IDS, state: 'ready' });
    return;
  }

  set({ key, userId, profileSlug, movies: [], ids: EMPTY_IDS, state: 'loading' });
  const token = ++fetchToken;
  getList(userId!, profileSlug!)
    .then((movies) => {
      if (token !== fetchToken) return; // superseded by a newer load
      set({ movies, ids: new Set(movies.map((m) => m.id)), state: 'ready' });
    })
    .catch(() => {
      if (token === fetchToken) set({ state: 'error' });
    });
}

/** Add a title to the current list (optimistic; rolls back on failure). */
export async function addToMyList(movie: Movie) {
  const { key, userId, profileSlug } = snap;
  if (!key || !userId || !profileSlug || snap.ids.has(movie.id)) return;

  const prevMovies = snap.movies;
  const prevIds = snap.ids;
  const ids = new Set(prevIds);
  ids.add(movie.id);
  set({ movies: [movie, ...prevMovies], ids });

  try {
    await saveToList(userId, profileSlug, movie.id);
    track('list_add', 'title_detail', { item_id: movie.id });
  } catch {
    if (snap.key === key) set({ movies: prevMovies, ids: prevIds });
  }
}

/** Remove a title from the current list (optimistic; rolls back on failure). */
export async function removeFromMyList(titleId: string) {
  const { key, userId, profileSlug } = snap;
  if (!key || !userId || !profileSlug || !snap.ids.has(titleId)) return;

  const prevMovies = snap.movies;
  const prevIds = snap.ids;
  const ids = new Set(prevIds);
  ids.delete(titleId);
  set({ movies: prevMovies.filter((m) => m.id !== titleId), ids });

  try {
    await removeFromList(userId, profileSlug, titleId);
    track('list_remove', 'title_detail', { item_id: titleId });
  } catch {
    if (snap.key === key) set({ movies: prevMovies, ids: prevIds });
  }
}

export interface MyList {
  movies: Movie[];
  /** title_ids currently in the list (drives ✓ state on cards). */
  ids: Set<string>;
  state: State;
  inList: (id: string) => boolean;
  add: (movie: Movie) => void;
  remove: (id: string) => void;
  toggle: (movie: Movie) => void;
}

/** Subscribe to the shared My List for a user+profile. */
export function useMyList(userId?: string | null, profileSlug?: string): MyList {
  useEffect(() => {
    loadMyList(userId, profileSlug);
  }, [userId, profileSlug]);

  const s = useSyncExternalStore(subscribe, getSnapshot);

  return {
    movies: s.movies,
    ids: s.ids,
    state: s.state,
    inList: (id: string) => s.ids.has(id),
    add: addToMyList,
    remove: removeFromMyList,
    toggle: (movie: Movie) =>
      s.ids.has(movie.id) ? removeFromMyList(movie.id) : addToMyList(movie),
  };
}
