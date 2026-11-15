import { env } from './env';
import { getIdToken } from './cognito';

/**
 * Client for the Settings API (GET / PUT /profile/declared): a member's
 * declared rules (L3). The API is the only writer of L3; loved genres in a PUT
 * are not stored there -- they give the member's learned taste (L2) a weak,
 * fading head start through the event stream.
 */

export type FollowedKind = 'teams' | 'players' | 'drivers' | 'talent';
export interface FollowedEntry {
  name: string;
  id?: string;
}

export interface Declared {
  excluded_genres: string[];
  avoid_themes: string[];
  kids_profile: boolean;
  languages: string[];
  subtitles: string;
  max_length_minutes: number | null;
  followed: Record<FollowedKind, FollowedEntry[]>;
  consent: Record<string, boolean>;
}

export interface GenreOption {
  id: string;
  label: string;
}

export interface DeclaredView {
  profile_id: string;
  version: number;
  updated_at: string | null;
  updated_by: string | null;
  declared: Declared;
  options?: { genres: GenreOption[] };
}

export interface PutResult {
  status: number;
  body: DeclaredView & { error?: string; details?: { field: string; reason: string }[]; current?: DeclaredView };
}

async function headers(): Promise<Record<string, string>> {
  const token = await getIdToken();
  return { 'Content-Type': 'application/json', ...(token ? { Authorization: token } : {}) };
}

const url = (profileId: string) =>
  `${env.apiBaseUrl}/profile/declared?${new URLSearchParams({ profile_id: profileId })}`;

export async function getDeclared(profileId: string): Promise<DeclaredView> {
  const res = await fetch(url(profileId), { headers: await headers() });
  if (!res.ok) throw new Error(`profile/declared failed: ${res.status}`);
  return (await res.json()) as DeclaredView;
}

export async function putDeclared(
  profileId: string,
  body: { version: number; source: 'onboarding' | 'settings'; declared: Declared; loved_genres: string[] },
): Promise<PutResult> {
  const res = await fetch(url(profileId), {
    method: 'PUT',
    headers: await headers(),
    body: JSON.stringify({ ...body, request_id: crypto.randomUUID() }),
  });
  return { status: res.status, body: await res.json().catch(() => ({})) };
}

/** One type-ahead match from GET /profile/entities. */
export interface EntitySuggestion {
  id: string;
  name: string;
  kind: Exclude<FollowedKind, 'talent'>;
  sport: string;
  team: string | null;
  number: number | null;
}

export async function suggestEntities(
  q: string,
  kind: Exclude<FollowedKind, 'talent'>,
  signal?: AbortSignal,
): Promise<EntitySuggestion[]> {
  const res = await fetch(`${env.apiBaseUrl}/profile/entities?${new URLSearchParams({ q, kind })}`, {
    headers: await headers(),
    signal,
  });
  if (!res.ok) throw new Error(`profile/entities failed: ${res.status}`);
  return ((await res.json()) as { entities: EntitySuggestion[] }).entities;
}
