/**
 * Attach a video source to a <video> element, handling HLS (.m3u8) across
 * browsers:
 *   - Safari plays HLS natively via `src`.
 *   - Everyone else uses hls.js (Media Source Extensions).
 *   - Progressive files (mp4/webm) just set `src`.
 *
 * `startAt` re-seeks the element after it loads, so the playhead survives a
 * re-attach (e.g. switching the horizontal/vertical rendition). Returns a
 * cleanup that tears down the hls.js instance / listeners.
 */
import Hls from 'hls.js';
import { ensureMediaAccess, hasMediaAccess, isProtectedMedia } from './mediaSession';

/**
 * A /media/* stream needs the signed cookies first. They are normally fetched
 * at sign-in, so this attaches at once; otherwise it waits for them, then
 * attaches and starts playback (the caller's own play() found no source yet).
 */
export function attachStream(video: HTMLVideoElement, url: string, startAt = 0): () => void {
  if (!isProtectedMedia(url) || hasMediaAccess()) return attachNow(video, url, startAt);
  let detach: (() => void) | null = null;
  let cancelled = false;
  void ensureMediaAccess().then(() => {
    if (cancelled) return;
    detach = attachNow(video, url, startAt);
    video.play().catch(() => {});
  });
  return () => {
    cancelled = true;
    detach?.();
  };
}

function attachNow(video: HTMLVideoElement, url: string, startAt: number): () => void {
  const isHls = url.includes('.m3u8');

  const seek = () => {
    if (startAt > 0) {
      try {
        video.currentTime = startAt;
      } catch {
        /* element not ready — the loadedmetadata path covers this */
      }
    }
  };

  // Progressive file, or native HLS (Safari): just set src.
  if (!isHls || video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
    video.addEventListener('loadedmetadata', seek, { once: true });
    return () => video.removeEventListener('loadedmetadata', seek);
  }

  // HLS via hls.js everywhere else.
  if (Hls.isSupported()) {
    const hls = new Hls({ enableWorker: true, lowLatencyMode: false });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, seek);
    return () => {
      hls.off(Hls.Events.MANIFEST_PARSED, seek);
      hls.destroy();
    };
  }

  // Last resort: hand the URL to the element and hope the platform copes.
  video.src = url;
  video.addEventListener('loadedmetadata', seek, { once: true });
  return () => video.removeEventListener('loadedmetadata', seek);
}
