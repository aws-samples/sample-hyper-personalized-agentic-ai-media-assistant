import { useEffect, useRef, useState } from 'react';
import { Heart, Plus, Share2, Play, Volume2, VolumeX } from 'lucide-react';
import { Poster } from '../../components/Poster';
import { refreshClipUrl } from '../../lib/clipsClient';
import type { Clip, ClipSource } from '../../data/types';
import { usePlaybackEvents } from '../../lib/usePlaybackEvents';
import type { Surface } from '../../lib/events';
import { FeedbackButtons, type CardFeedback } from './FeedbackButtons';

interface ClipCardProps {
  clip: Clip;
  index: number;
  muted: boolean;
  onToggleMute: () => void;
  onWatchFull: (clip: Clip) => void;
  onOpenTitle: (clip: Clip) => void;
  /** Feed this card is shown in, for behaviour events. */
  surface?: Surface;
  /** Personalized feed: the card's feedback and its handler (like / more / less). */
  feedback?: CardFeedback;
  onFeedback?: (clip: Clip, value: CardFeedback) => void;
}

/**
 * Attach a clip source to a <video>. This is the one place that knows how each
 * source type plays, so adding HLS/live (hls.js or the IVS web player) later is
 * a local change — the feed and overlay never touch playback wiring.
 */
function attachSource(video: HTMLVideoElement, source: ClipSource): void {
  if (source.type === 'file') {
    video.src = source.src;
    return;
  }
  // hls / live: Safari plays HLS natively; other browsers need hls.js / IVS.
  if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = source.src;
  } else {
    // TODO(live): attach via hls.js or the IVS player when a stream is added.
    console.warn(`Clip "${source.src}" needs an HLS/live adapter; not yet wired.`);
  }
}

/** A single full-screen clip: video + Netflix-style overlay. */
export function ClipCard({
  clip,
  index,
  muted,
  onToggleMute,
  onWatchFull,
  onOpenTitle,
  surface = 'clips_feed',
  feedback = 'none',
  onFeedback,
}: ClipCardProps) {
  const videoRef = useRef<HTMLVideoElement>(null);
  usePlaybackEvents(
    videoRef,
    { itemId: clip.id, contentType: clip.kind, surface, parentItemId: clip.titleId },
    { feed: true, rank: index + 1 },
  );
  const retriedRef = useRef(false);
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);
  const [liked, setLiked] = useState(false);
  const [saved, setSaved] = useState(false);
  const [shared, setShared] = useState(false);

  // Copy a deep link to this clip (/clips/:clipId), like the IVS demo's Share.
  const shareClip = () => {
    const url = `${window.location.origin}/clips/${clip.id}`;
    navigator.clipboard
      ?.writeText(url)
      .then(() => {
        setShared(true);
        setTimeout(() => setShared(false), 1500);
      })
      .catch(() => {});
  };

  // Wire the source + mirror playback state into React.
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !clip.video) return;
    retriedRef.current = false;
    attachSource(v, clip.video);

    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onTime = () =>
      setProgress(v.duration > 0 ? Math.min(1, v.currentTime / v.duration) : 0);
    // One-shot recovery: if the clip fails to load (most likely its ?token=
    // access token expired mid-session), refresh the token and reload once.
    const onError = () => {
      if (retriedRef.current) return;
      retriedRef.current = true;
      refreshClipUrl(v.src)
        .then((fresh) => {
          v.src = fresh;
          v.load();
          v.play().catch(() => {});
        })
        .catch(() => {});
    };
    v.addEventListener('play', onPlay);
    v.addEventListener('pause', onPause);
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('error', onError);
    return () => {
      v.removeEventListener('play', onPlay);
      v.removeEventListener('pause', onPause);
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('error', onError);
    };
  }, [clip.video]);

  // Keep the element's muted flag in sync with the feed-wide toggle.
  useEffect(() => {
    if (videoRef.current) videoRef.current.muted = muted;
  }, [muted]);

  const togglePlay = () => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) v.play().catch(() => {});
    else v.pause();
  };

  return (
    <section
      className="clip"
      id={`clip-${clip.id}`}
      data-clip-id={clip.id}
      data-index={index}
      aria-label={`Clip ${index + 1}`}
    >
      {/* Gradient placeholder sits behind the video as a poster/letterbox fill. */}
      <div className="clip-bg">
        <Poster
          hueA={clip.hueA}
          hueB={clip.hueB}
          ratio="2/3"
          rounded={0}
          showLabel={false}
          style={{ height: '100%', width: '100%' }}
        />
      </div>

      <video
        ref={videoRef}
        className="clip-video"
        poster={clip.poster}
        muted={muted}
        playsInline
        loop
        preload="metadata"
        onClick={togglePlay}
      />
      <div className="clip-scrim" />

      {/* Tap-to-play affordance when paused. */}
      {!playing && (
        <button className="clip-playpause" onClick={togglePlay} aria-label="Play">
          <Play size={34} fill="currentColor" />
        </button>
      )}

      {/* Mute toggle (feed-wide). */}
      <button
        className="clip-mute"
        onClick={onToggleMute}
        aria-label={muted ? 'Unmute' : 'Mute'}
      >
        {muted ? <VolumeX size={20} /> : <Volume2 size={20} />}
      </button>

      {/* Right-side action rail. */}
      <div className="clip-rail">
        <button
          className="clip-avatar"
          style={{
            background: `linear-gradient(145deg, hsl(${clip.hueA} 65% 46%), hsl(${clip.hueB} 60% 32%))`,
          }}
          onClick={() => onOpenTitle(clip)}
          aria-label="Go to title"
        />
        {onFeedback ? (
          <FeedbackButtons clip={clip} value={feedback} onFeedback={onFeedback} />
        ) : (
          <button
            className={`clip-action ${liked ? 'liked' : ''}`}
            onClick={() => setLiked((l) => !l)}
          >
            <Heart className="clip-ic" fill={liked ? 'currentColor' : 'none'} />
            <span>{clip.likes}</span>
          </button>
        )}
        <button
          className={`clip-action ${saved ? 'saved' : ''}`}
          onClick={() => setSaved((s) => !s)}
        >
          <span className="clip-plus">
            <Plus className="clip-ic" />
          </span>
          <span>{saved ? 'Added' : 'My List'}</span>
        </button>
        <button className="clip-action" onClick={shareClip}>
          <Share2 className="clip-ic" />
          <span>{shared ? 'Copied' : 'Share'}</span>
        </button>
      </div>

      {/* Bottom-left info block. */}
      <div className="clip-info">
        {clip.kind === 'microdrama' ? (
          <span className="clip-tag">✦ AI-generated</span>
        ) : (
          clip.tag && <span className="clip-tag">✦ {clip.tag}</span>
        )}
        <span className="clip-source">{clip.source}</span>
        <h2 className="clip-title">{clip.title}</h2>
        {clip.genres && clip.genres.length > 0 && (
          <div className="clip-genres">
            {clip.genres.map((g) => (
              <span key={g} className="clip-genre">
                {g}
              </span>
            ))}
          </div>
        )}
        {clip.explanation && <span className="clip-why">{clip.explanation}</span>}
        {(clip.titleId || clip.movie) && (
          <button className="clip-watch" onClick={() => onWatchFull(clip)}>
            <Play className="clip-watch-ic" size={15} fill="currentColor" /> Watch Full
          </button>
        )}
      </div>

      {/* Playback progress. */}
      <div className="clip-progress">
        <span className="clip-progress-fill" style={{ width: `${progress * 100}%` }} />
      </div>
    </section>
  );
}
