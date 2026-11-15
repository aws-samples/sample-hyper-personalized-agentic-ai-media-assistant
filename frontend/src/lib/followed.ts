import { useEffect, useMemo, useState } from 'react';
import { getDeclared, type FollowedEntry } from './settingsApi';
import { env } from './env';
import { useSession } from './session';
import { LIVE_GAMES, sportsEntityById, type SportsEntity } from '../data/sports';
import type { LiveGame } from '../data/types';

/**
 * Who a profile follows (L3 `followed`, resolved to canonical ids by the
 * Settings API), turned into what the sports screens need: the followed
 * entities that appear in this app's live content, the live games they are
 * in, and name checks for rosters and highlights.
 *
 * Read once per profile per page load (the same L3 read the agent does once
 * per session) and shared by every screen; a save from the preferences screen
 * refreshes it. Entries without a canonical id never match anything.
 */

const PROFILE_ID_RE = /^p-[0-9a-f]{32}$/;
const cache = new Map<string, Promise<FollowedEntry[]>>();
const listeners = new Set<() => void>();

function load(profileId: string): Promise<FollowedEntry[]> {
  let p = cache.get(profileId);
  if (!p) {
    p = getDeclared(profileId)
      .then((v) => [...v.declared.followed.teams, ...v.declared.followed.players])
      .catch(() => {
        cache.delete(profileId); // retry on the next screen
        return [];
      });
    cache.set(profileId, p);
  }
  return p;
}

/** Drop a profile's cached follows (after its preferences were saved). */
export function invalidateFollowed(profileId: string): void {
  cache.delete(profileId);
  listeners.forEach((l) => l());
}

export interface Followed {
  /** False until the profile's follows have been read. */
  ready: boolean;
  /** Every resolved follow ({id, name}), including ones absent from live content. */
  resolved: { id: string; name: string }[];
  /** Followed entities that appear in live content, in the order followed. */
  entities: SportsEntity[];
  players: SportsEntity[];
  teams: SportsEntity[];
  /** Live games with a followed team or player, in LIVE_GAMES order. */
  liveGames: LiveGame[];
  /** Is this roster / highlight name a followed player? */
  isPlayer: (name: string | null | undefined) => boolean;
  /** Is this team name (any feed spelling) a followed team? */
  isTeam: (name: string | null | undefined) => boolean;
}

const norm = (s: string) => s.normalize('NFC').toLowerCase();

export function followedFrom(entries: FollowedEntry[], ready = true): Followed {
  const resolved = entries.filter((e): e is { id: string; name: string } => Boolean(e.id));
  const entities = resolved.map((e) => sportsEntityById.get(e.id)).filter((e): e is SportsEntity => Boolean(e));
  const players = entities.filter((e) => e.kind === 'player');
  const teams = entities.filter((e) => e.kind === 'team');
  const playerNames = new Set(players.flatMap((p) => p.names.map(norm)));
  const teamNames = new Set(teams.flatMap((t) => t.names.map(norm)));
  const gameIds = new Set(entities.map((e) => e.gameId));
  return {
    ready,
    resolved,
    entities,
    players,
    teams,
    liveGames: LIVE_GAMES.filter((g) => gameIds.has(g.id)),
    isPlayer: (name) => Boolean(name && playerNames.has(norm(name))),
    isTeam: (name) => Boolean(name && teamNames.has(norm(name))),
  };
}

const NONE = followedFrom([], false);

/** The active profile's follows. Profiles outside the household follow no one. */
export function useFollowed(profileId: string | null | undefined): Followed {
  const [entries, setEntries] = useState<{ id: string; list: FollowedEntry[] } | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const l = () => setTick((t) => t + 1);
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  }, []);

  useEffect(() => {
    if (!profileId || !env.apiBaseUrl || !PROFILE_ID_RE.test(profileId)) return;
    let active = true;
    load(profileId).then((list) => active && setEntries({ id: profileId, list }));
    return () => {
      active = false;
    };
  }, [profileId, tick]);

  return useMemo(() => {
    if (!profileId || !PROFILE_ID_RE.test(profileId)) return followedFrom([]);
    return entries?.id === profileId ? followedFrom(entries.list) : NONE;
  }, [profileId, entries]);
}

/** Follows of the profile chosen in this session. */
export function useActiveFollowed(): Followed {
  return useFollowed(useSession().profileId);
}
