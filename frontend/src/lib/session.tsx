import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';
import * as cognito from './cognito';
import { resetAgentSession } from './agentSession';
import { setEventProfile } from './events';
import { clearMediaAccess, ensureMediaAccess } from './mediaSession';

/**
 * Session state: real Cognito auth + which demo profile is active.
 *
 * `signIn` authenticates against the Cognito user pool; the library persists
 * tokens to localStorage, so auth survives reloads (restored on mount).
 * The active profile is a client-side selection persisted separately.
 */

const PROFILE_KEY = 'hpm-profile';

interface SessionValue {
  /** True once the Cognito session is confirmed valid. */
  authed: boolean;
  /** Still restoring session from storage on first load. */
  loading: boolean;
  /** Signed-in Cognito username (used as user_id for backend calls). */
  username: string | null;
  profileId: string | null;
  signIn: (username: string, password: string) => Promise<void>;
  signOut: () => void;
  selectProfile: (id: string) => void;
}

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [username, setUsername] = useState<string | null>(null);
  const [profileId, setProfileId] = useState<string | null>(
    () => localStorage.getItem(PROFILE_KEY),
  );

  // Behaviour events are attributed to the active household member.
  useEffect(() => {
    setEventProfile(authed ? profileId : null);
  }, [authed, profileId]);

  // Signed cookies for the /media/* streams: fetched at sign-in so players
  // attach at once, and topped up while the app stays open.
  useEffect(() => {
    if (!authed) return;
    void ensureMediaAccess();
    const timer = window.setInterval(() => void ensureMediaAccess(), 30 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [authed]);

  // Restore any persisted Cognito session on first load.
  useEffect(() => {
    let active = true;
    cognito.hasValidSession().then((valid) => {
      if (!active) return;
      setAuthed(valid);
      setUsername(valid ? cognito.getCurrentUsername() : null);
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, []);

  const signIn = useCallback(async (user: string, password: string) => {
    await cognito.signIn(user, password);
    setAuthed(true);
    setUsername(cognito.getCurrentUsername() ?? user);
  }, []);

  const signOut = useCallback(() => {
    cognito.signOut();
    clearMediaAccess();
    localStorage.removeItem(PROFILE_KEY);
    resetAgentSession();
    setAuthed(false);
    setUsername(null);
    setProfileId(null);
  }, []);

  // Picking a profile starts a new agent conversation.
  const selectProfile = useCallback((id: string) => {
    localStorage.setItem(PROFILE_KEY, id);
    resetAgentSession();
    setProfileId(id);
  }, []);

  const value = useMemo<SessionValue>(
    () => ({ authed, loading, username, profileId, signIn, signOut, selectProfile }),
    [authed, loading, username, profileId, signIn, signOut, selectProfile],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error('useSession must be used within a SessionProvider');
  return ctx;
}
