import { env } from './env';
import { getAccessToken, getIdToken } from './cognito';

/**
 * VOD highlight reels (the detail-hero preview), chosen per profile.
 *
 * A show can have several clips, `highlights/vod/<orientation>/<base>_*.mp4`,
 * each annotated in S3 with a one-sentence genre description. get_video_url
 * picks the clip whose description is closest to the profile's taste and
 * returns a presigned URL plus the clip's file name. With a media CDN the
 * player streams that file from the CDN instead.
 */

const MEDIA_BASE = env.mediaBaseUrl?.replace(/\/+$/, '') ?? '';

/** Catalog title_ids that have highlight clips (the API keeps the file names).
 *  Keep in step with the get_video_url VOD_HIGHLIGHTS setting (stacks.py).
 *  Tears of Steel's clips are cut by notebooks/tears-of-steel/; without them
 *  the API answers 404 and the title shows its poster. */
const VOD_HIGHLIGHT_TITLES = new Set<string>(['tt2285752']);

function withToken(url: string, token: string | null): string {
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

/** True when this title has a highlight preview the API can choose for a profile. */
export function hasVodHighlight(titleId: string, profileId?: string | null): boolean {
  return Boolean(env.apiBaseUrl && VOD_HIGHLIGHT_TITLES.has(titleId) && profileId);
}

/** The profile's preview clip URL, or null when none applies. */
export async function vodHighlightUrl(
  titleId: string,
  profileId: string | null | undefined,
  orientation: 'horizontal' | 'vertical' = 'horizontal',
): Promise<string | null> {
  if (!profileId || !hasVodHighlight(titleId, profileId)) return null;
  // REST API's Cognito authorizer takes the raw ID token (same as lib/api.ts).
  const idToken = await getIdToken();
  const params = new URLSearchParams({ title_id: titleId, highlight: '1', profile_id: profileId, orientation });
  try {
    const res = await fetch(`${env.apiBaseUrl}/get_video_url?${params}`, {
      headers: idToken ? { Authorization: idToken } : {},
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { url?: string; clip?: string };
    if (MEDIA_BASE && body.clip) {
      return withToken(`${MEDIA_BASE}/highlights/vod/${orientation}/${body.clip}`, await getAccessToken());
    }
    return body.url ?? null;
  } catch {
    return null;
  }
}
