import { env } from './env';
import { getIdToken } from './cognito';

/**
 * Signed cookies for the app's own /media/* path (the live-sports HLS streams).
 *
 * CloudFront only serves /media/* to requests carrying valid signed cookies.
 * GET /media/session (Cognito) returns them; the media is on this page's own
 * origin, so setting them here makes the browser send them with every playlist
 * and segment request, from hls.js and from native (Safari) HLS alike.
 *
 * Cookies last 4 hours. They are fetched again once less than 3 hours remain,
 * so a stream started now can always play to the end of a full match.
 */

const PATH = '/media/';
const REFRESH_BEFORE_S = 3 * 3600;
let expires = 0;
let pending: Promise<boolean> | null = null;

/** True for a URL that needs the signed cookies. */
export function isProtectedMedia(url: string): boolean {
  return url.startsWith(PATH);
}

async function fetchCookies(): Promise<boolean> {
  const token = await getIdToken();
  if (!env.apiBaseUrl || !token) return false;
  const base = env.apiBaseUrl.replace(/\/+$/, '');
  const res = await fetch(`${base}/media/session`, { headers: { Authorization: token } });
  if (!res.ok) return false;
  const data = (await res.json()) as { cookies: Record<string, string>; expires: number };
  const maxAge = Math.max(0, data.expires - Math.floor(Date.now() / 1000));
  for (const [name, value] of Object.entries(data.cookies)) {
    document.cookie = `${name}=${value}; Path=${PATH}; Max-Age=${maxAge}; Secure; SameSite=Strict`;
  }
  expires = data.expires;
  return true;
}

/** True while the cookies set in this page load are still fresh. */
export function hasMediaAccess(): boolean {
  return expires - Date.now() / 1000 > REFRESH_BEFORE_S;
}

/** Make sure valid media cookies are set. Resolves false when that failed. */
export function ensureMediaAccess(): Promise<boolean> {
  if (hasMediaAccess()) return Promise.resolve(true);
  if (!pending) {
    pending = fetchCookies()
      .catch(() => false)
      .finally(() => {
        pending = null;
      });
  }
  return pending;
}

/** Drop the cookies (sign-out), so the next viewer starts without them. */
export function clearMediaAccess(): void {
  expires = 0;
  for (const name of ['CloudFront-Policy', 'CloudFront-Signature', 'CloudFront-Key-Pair-Id', 'CloudFront-Hash-Algorithm']) {
    document.cookie = `${name}=; Path=${PATH}; Max-Age=0; Secure; SameSite=Strict`;
  }
}
