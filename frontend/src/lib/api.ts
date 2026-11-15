import { env } from './env';
import { getIdToken } from './cognito';

/**
 * Typed client for the IBC 2026 recommendations REST API.
 * The response `Movie` shape is the stable contract the backend normalizes to
 * (see the Lambda shared layer), independent of the OpenSearch schema.
 */

export interface Movie {
  id: string;
  title: string;
  posterUrl?: string | null;
  rating?: number | null;
  genres: string[];
  plot?: string | null;
  cast: string[];
  maturity?: string | null;
  year?: number | null;
  blurb?: string;
  /** Presigned S3 URL for playback, when available (from the agent). */
  contentUrl?: string | null;
}

async function authHeaders(): Promise<Record<string, string>> {
  const token = await getIdToken();
  return token ? { Authorization: token } : {};
}

/** True when the API base URL is configured (otherwise callers fall back to mock). */
export function apiConfigured(): boolean {
  return Boolean(env.apiBaseUrl);
}

/** A named Home row of movies. */
export interface Carousel {
  key: string;
  displayTitle: string;
  movies: Movie[];
}

/**
 * Resolve a playable presigned S3 URL for a title, or null when none exists.
 *
 * Titles recommended by the agent already carry a `contentUrl`; this is the
 * fallback for titles that reach the player without one (Home / top-picks
 * rows, which come from the REST recs API).
 */
export async function getVideoUrl(titleId: string): Promise<string | null> {
  if (!env.apiBaseUrl || !titleId) return null;
  const params = new URLSearchParams({ title_id: titleId });
  const res = await fetch(`${env.apiBaseUrl}/get_video_url?${params}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) return null;
  const data = await res.json();
  return (data.url as string) ?? null;
}

/** Fetch a profile's top-pick movies. `userId` is the Cognito username. */
export async function getTopPicks(userId: string, profileSlug: string): Promise<Movie[]> {
  if (!env.apiBaseUrl) return [];
  const params = new URLSearchParams({ user_id: userId, profile: profileSlug });
  const res = await fetch(`${env.apiBaseUrl}/get_top_picks?${params}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`get_top_picks failed: ${res.status}`);
  const data = await res.json();
  return (data.movies ?? []) as Movie[];
}

/** Fetch all of a profile's Home carousels (ordered). */
export async function getCarousels(userId: string, profileSlug: string): Promise<Carousel[]> {
  if (!env.apiBaseUrl) return [];
  const params = new URLSearchParams({ user_id: userId, profile: profileSlug });
  const res = await fetch(`${env.apiBaseUrl}/get_carousels?${params}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`get_carousels failed: ${res.status}`);
  const data = await res.json();
  return (data.carousels ?? []) as Carousel[];
}

// ---- Household + Home (identity comes from the Cognito token) -------------

/** One member of the signed-in account's household. */
export interface HouseholdMember {
  profileId: string;
  displayName: string;
  /** Seed member this one was created from (e.g. "action-fan"); null for others. */
  memberKey?: string | null;
  avatar: { hue?: number; icon?: string; kids?: boolean };
  rank: number;
  /** Set when the member was removed (soft delete; its data is kept for a restore). */
  removedAt?: string;
}

export interface Household {
  members: HouseholdMember[];
  /** Removed members, which can be restored with all their settings and history. */
  removed: HouseholdMember[];
}

type MemberResult = { ok: true; member: HouseholdMember } | { ok: false; status: number; error: string };

/** Remove a member (soft) or restore a removed one: POST /household/members/{id}/remove|restore. */
export async function setMemberRemoved(profileId: string, remove: boolean): Promise<MemberResult> {
  const action = remove ? 'remove' : 'restore';
  const res = await fetch(`${env.apiBaseUrl}/household/members/${encodeURIComponent(profileId)}/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(await authHeaders()) },
    body: '{}',
  });
  const body = await res.json().catch(() => ({}));
  if (res.ok) return { ok: true, member: body as HouseholdMember };
  return { ok: false, status: res.status, error: (body as { error?: string }).error ?? `failed: ${res.status}` };
}

/** Add a member to the caller's household (POST /household/members). */
export async function addMember(displayName: string, kidsProfile: boolean): Promise<
  { ok: true; member: HouseholdMember } | { ok: false; status: number; error: string }
> {
  const res = await fetch(`${env.apiBaseUrl}/household/members`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(await authHeaders()) },
    body: JSON.stringify({ display_name: displayName, kids_profile: kidsProfile }),
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 201) return { ok: true, member: body as HouseholdMember };
  return { ok: false, status: res.status, error: (body as { error?: string }).error ?? `failed: ${res.status}` };
}

/** The caller's household: active and removed members. The first call creates the default household. */
export async function getHousehold(): Promise<Household> {
  if (!env.apiBaseUrl) return { members: [], removed: [] };
  const res = await fetch(`${env.apiBaseUrl}/household`, { headers: await authHeaders() });
  if (!res.ok) throw new Error(`household failed: ${res.status}`);
  const data = await res.json();
  return { members: (data.members ?? []) as HouseholdMember[], removed: (data.removed ?? []) as HouseholdMember[] };
}

/** A member's landing page: hero + ordered rails, all from the recommendation cache. */
export interface HomePage {
  profileId: string;
  hero: Movie | null;
  rails: Carousel[];
}

export async function getHome(profileId: string): Promise<HomePage> {
  const params = new URLSearchParams({ profile_id: profileId });
  const res = await fetch(`${env.apiBaseUrl}/home?${params}`, { headers: await authHeaders() });
  if (!res.ok) throw new Error(`home failed: ${res.status}`);
  return (await res.json()) as HomePage;
}

/** Just the member's idle-search starter texts (GET /home?part=options; no rows). */
export async function getTappableOptions(profileId: string): Promise<string[]> {
  if (!env.apiBaseUrl || !profileId) return [];
  const params = new URLSearchParams({ profile_id: profileId, part: 'options' });
  const res = await fetch(`${env.apiBaseUrl}/home?${params}`, { headers: await authHeaders() });
  if (!res.ok) throw new Error(`options failed: ${res.status}`);
  const data = (await res.json()) as { tappableOptions?: string[] };
  return Array.isArray(data.tappableOptions) ? data.tappableOptions : [];
}

// ---- My List (watch_list) -------------------------------------------------

/** Fetch a profile's "My List", hydrated to full movies (stored order). */
export async function getList(userId: string, profileSlug: string): Promise<Movie[]> {
  if (!env.apiBaseUrl) return [];
  const params = new URLSearchParams({ user_id: userId, profile: profileSlug });
  const res = await fetch(`${env.apiBaseUrl}/get_list?${params}`, {
    headers: await authHeaders(),
  });
  if (!res.ok) throw new Error(`get_list failed: ${res.status}`);
  const data = await res.json();
  return (data.movies ?? []) as Movie[];
}

async function mutateList(
  path: 'save_list' | 'remove_from_list',
  userId: string,
  profileSlug: string,
  titleId: string,
): Promise<void> {
  if (!env.apiBaseUrl) return;
  const res = await fetch(`${env.apiBaseUrl}/${path}`, {
    method: 'POST',
    headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, profile: profileSlug, title_id: titleId }),
  });
  // 409 (already in list) on save is a no-op success for our optimistic UI.
  if (!res.ok && res.status !== 409) throw new Error(`${path} failed: ${res.status}`);
}

/** Add a title to a profile's My List. */
export function saveToList(userId: string, profileSlug: string, titleId: string): Promise<void> {
  return mutateList('save_list', userId, profileSlug, titleId);
}

/** Remove a title from a profile's My List. */
export function removeFromList(userId: string, profileSlug: string, titleId: string): Promise<void> {
  return mutateList('remove_from_list', userId, profileSlug, titleId);
}
