import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import { getHousehold, setMemberRemoved, type HouseholdMember } from './api';
import { useSession } from './session';
import { profileFromMember, type DemoProfile } from '../data/profiles';

/**
 * The signed-in account's profile cards: its household members (GET /household,
 * which creates the default household on first call). Loaded once per sign-in
 * and shared by every screen. Removed members are kept separately so the
 * profile screen can offer to restore them.
 */

type State = 'idle' | 'loading' | 'ready' | 'error';

/** Result of a remove/restore: null on success, else the error to show. */
type ActionResult = Promise<string | null>;

interface HouseholdValue {
  profiles: DemoProfile[];
  /** Number of active household members. */
  members: number;
  /** Removed members, restorable with all their data. */
  removed: DemoProfile[];
  state: State;
  byId: (id: string | null | undefined) => DemoProfile | undefined;
  /** Show a member just created by POST /household/members without refetching. */
  added: (m: HouseholdMember) => void;
  remove: (id: string) => ActionResult;
  restore: (id: string) => ActionResult;
}

const HouseholdContext = createContext<HouseholdValue | null>(null);

const byRank = (a: HouseholdMember, b: HouseholdMember) =>
  a.rank - b.rank || a.displayName.localeCompare(b.displayName);

export function HouseholdProvider({ children }: { children: ReactNode }) {
  const { authed, username } = useSession();
  const [active, setActive] = useState<HouseholdMember[]>([]);
  const [removed, setRemoved] = useState<HouseholdMember[]>([]);
  const [state, setState] = useState<State>('idle');

  useEffect(() => {
    if (!authed) {
      setActive([]);
      setRemoved([]);
      setState('idle');
      return;
    }
    let live = true;
    setState('loading');
    getHousehold()
      .then((h) => {
        if (!live) return;
        setActive(h.members);
        setRemoved(h.removed);
        setState('ready');
      })
      .catch(() => live && setState('error'));
    return () => {
      live = false;
    };
  }, [authed, username]);

  // Move a member between the active and removed lists after the API agreed.
  const move = useCallback(async (id: string, remove: boolean): ActionResult => {
    const r = await setMemberRemoved(id, remove).catch(() => null);
    if (!r || !r.ok) return r?.error ?? 'Something went wrong. Try again.';
    const member = r.member;
    setActive((all) => (remove ? all.filter((m) => m.profileId !== id) : [...all, member].sort(byRank)));
    setRemoved((all) => (remove ? [...all, member].sort(byRank) : all.filter((m) => m.profileId !== id)));
    return null;
  }, []);

  const value = useMemo<HouseholdValue>(() => {
    const profiles = active.map(profileFromMember);
    return {
      profiles,
      members: active.length,
      removed: removed.map(profileFromMember),
      state,
      byId: (id) => profiles.find((p) => p.id === id),
      added: (m) => setActive((all) => (all.some((p) => p.profileId === m.profileId) ? all : [...all, m])),
      remove: (id) => move(id, true),
      restore: (id) => move(id, false),
    };
  }, [active, removed, state, move]);

  return <HouseholdContext.Provider value={value}>{children}</HouseholdContext.Provider>;
}

export function useHousehold(): HouseholdValue {
  const ctx = useContext(HouseholdContext);
  if (!ctx) throw new Error('useHousehold must be used within a HouseholdProvider');
  return ctx;
}

/** The profile chosen in this session, once the household has loaded. */
export function useActiveProfile(): DemoProfile | undefined {
  const { profileId } = useSession();
  return useHousehold().byId(profileId);
}
