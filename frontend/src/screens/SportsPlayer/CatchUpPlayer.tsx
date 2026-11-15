import { useEffect, useRef, useState, type MouseEvent } from 'react';
import { ArrowLeft, Play, Pause, Volume2, VolumeX, SkipForward, SkipBack, Radio } from 'lucide-react';
import { attachStream } from '../../lib/hls';
import { usePlaybackEvents } from '../../lib/usePlaybackEvents';
import { Poster } from '../../components/Poster';
import {
  highlightClipSource,
  highlightLabel,
  type ClipSourceWindow,
  type HighlightClipGroup,
} from '../../lib/highlightClips';
import './SportsPlayer.css'; // reuse the game player's chrome (controls, scrubber)
import './CatchUpPlayer.css';

interface CatchUpPlayerProps {
  /** The full set of key moments (for playback + the "up next" list). */
  groups: HighlightClipGroup[];
  startIndex?: number;
  orientation?: 'horizontal' | 'vertical';
  /**
   * 'reel' auto-advances through every moment and then goes live (the
   * "Get Up to Speed" recap). 'single' plays one moment, then shows an
   * end card (up next / watch again / go live).
   */
  mode?: 'reel' | 'single';
  /** When provided, shows a "Go to live" button (and the reel jumps there at the end). */
  onGoLive?: () => void;
  onClose: () => void;
  title?: string;
}

// Thumbnail gradient hue by moment type (no per-team art available here).
const TYPE_HUE: Record<string, number> = { goal: 45, penalty: 2, save: 210, shot: 265 };

function fmt(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

/**
 * Plays highlight clips with the SAME chrome as the live game player (shared
 * .sports-* / .ctrl-btn classes), so watching a key moment or catching up feels
 * identical to the game. Auto-advances through the reel; when it finishes it
 * goes live (if onGoLive is given) or closes.
 */
export function CatchUpPlayer({
  groups,
  startIndex = 0,
  orientation = 'horizontal',
  mode = 'single',
  onGoLive,
  onClose,
  title,
}: CatchUpPlayerProps) {
  const [index, setIndex] = useState(startIndex);
  const [src, setSrc] = useState<ClipSourceWindow | null>(null);
  const [playing, setPlaying] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [duration, setDuration] = useState(0);
  const [ended, setEnded] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [muted, setMuted] = useState(true); // clips start muted, like the game feed

  const group = groups[index];
  const isLast = index >= groups.length - 1;
  const isReel = mode === 'reel';
  usePlaybackEvents(videoRef, group && src
    ? { itemId: `hl-${group.n}`, contentType: 'highlight', surface: 'sports' }
    : null);

  // Resolve where the current moment plays: a window of the match broadcast.
  useEffect(() => {
    let active = true;
    setSrc(null);
    if (group) highlightClipSource(group, orientation).then((u) => active && setSrc(u));
    return () => {
      active = false;
    };
  }, [group, orientation]);

  // Attach + play the current moment's window; advance (or go live) at its end.
  // Times shown on the scrubber are relative to the window.
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !src) return;
    const detach = attachStream(v, src.url, src.start);
    let done = false;
    setDuration(Math.max(0, src.end - src.start));
    const onTime = () => {
      setElapsed(Math.max(0, v.currentTime - src.start));
      if (!done && v.currentTime >= src.end) {
        done = true;
        v.pause();
        onEnded();
      } else if (v.currentTime < src.end - 0.5) {
        done = false;
      }
    };
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onEnded = () => {
      if (isReel) {
        if (!isLast) setIndex((i) => i + 1);
        else if (onGoLive) onGoLive();
        else onClose();
      } else {
        // Single moment: don't jump to live — offer what's next.
        setEnded(true);
      }
    };
    v.muted = muted;
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('play', onPlay);
    v.addEventListener('pause', onPause);
    v.addEventListener('ended', onEnded);
    v.play().catch(() => {
      v.muted = true;
      setMuted(true);
      v.play().catch(() => {});
    });
    return () => {
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('play', onPlay);
      v.removeEventListener('pause', onPause);
      v.removeEventListener('ended', onEnded);
      detach();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src, isLast]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const togglePlay = () => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) v.play().catch(() => {});
    else v.pause();
  };
  const toggleMute = () =>
    setMuted((m) => {
      const n = !m;
      if (videoRef.current) videoRef.current.muted = n;
      return n;
    });
  const prev = () => setIndex((i) => Math.max(0, i - 1));
  const next = () => {
    if (!isLast) setIndex((i) => i + 1);
    else if (onGoLive) onGoLive();
  };
  const seek = (e: MouseEvent<HTMLDivElement>) => {
    const v = videoRef.current;
    if (!v || !duration) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    v.currentTime = (src?.start ?? 0) + pct * duration;
    setElapsed(pct * duration);
  };

  // Pick another moment from the end card (single mode).
  const pickMoment = (i: number) => {
    setEnded(false);
    setIndex(i);
  };
  const replay = () => {
    const v = videoRef.current;
    setEnded(false);
    if (v) {
      v.currentTime = src?.start ?? 0;
      v.play().catch(() => {});
    }
  };

  const progress = duration ? elapsed / duration : 0;
  // Up next: the following moments (wrapping around), capped at 3 cards.
  const upNext = [...groups.slice(index + 1), ...groups.slice(0, index)].slice(0, 3);

  return (
    <div className="screen sports-player catchup" role="dialog" aria-modal="true">
      <div className="sports-stage">
        <div className="sports-bg">
          <video ref={videoRef} className="sports-video" playsInline muted={muted} onClick={togglePlay} />
          <div className="player-scrim" />
        </div>

        <div className="sports-top">
          <button className="player-back" onClick={onClose} aria-label="Back">
            <ArrowLeft size={24} />
          </button>
          <span className="sports-league">{title ?? (isReel ? 'Getting you up to speed' : 'Key moment')}</span>
          {onGoLive && (
            <button className="cu-golive" onClick={onGoLive}>
              <Radio size={15} /> Go to live
            </button>
          )}
        </div>

        <div className="sports-controls">
          <div className="cu-meta">
            {isReel && <span className="cu-count">Key moment {index + 1} of {groups.length}</span>}
            {group && <span className="cu-label">{highlightLabel(group)}</span>}
          </div>
          <div className="sports-timeline" onClick={seek} role="slider" aria-label="Seek" tabIndex={0}>
            <div className="stl-fill" style={{ width: `${progress * 100}%` }} />
            <span className="stl-knob" style={{ left: `${progress * 100}%` }} />
          </div>
          <div className="controls-row">
            <div className="controls-left">
              {isReel && (
                <button className="ctrl-btn" onClick={prev} disabled={index === 0} aria-label="Previous moment">
                  <SkipBack size={20} />
                </button>
              )}
              <button className="ctrl-btn primary" onClick={togglePlay} aria-label={playing ? 'Pause' : 'Play'}>
                {playing ? <Pause size={22} /> : <Play size={22} fill="currentColor" />}
              </button>
              {isReel && (
                <button className="ctrl-btn" onClick={next} aria-label="Next moment">
                  <SkipForward size={20} />
                </button>
              )}
              <button className="ctrl-btn" onClick={toggleMute} aria-label={muted ? 'Unmute' : 'Mute'}>
                {muted ? <VolumeX size={22} /> : <Volume2 size={22} />}
              </button>
              <span className="cu-time">{fmt(elapsed)} / {fmt(duration)}</span>
            </div>
          </div>
        </div>

        {ended && (
          <div className="cu-endcard">
            <div className="cu-endcard-inner">
              <div className="cu-endcard-actions">
                <button className="cu-endcard-btn ghost" onClick={onClose}>Back</button>
                <button className="cu-endcard-btn" onClick={replay}>Watch again</button>
                {onGoLive && (
                  <button className="cu-golive" onClick={onGoLive}>
                    <Radio size={15} /> Go to live
                  </button>
                )}
              </div>
              {upNext.length > 0 && (
                <>
                  <h3 className="cu-endcard-head">Up next</h3>
                  <div className="cu-upnext">
                    {upNext.map((g) => {
                      const i = groups.indexOf(g);
                      const hue = TYPE_HUE[g.lead.type] ?? 220;
                      return (
                        <button key={g.n} className="cu-upnext-card" onClick={() => pickMoment(i)}>
                          <div className="cu-upnext-art">
                            <Poster hueA={hue} hueB={hue + 25} ratio="16/9" rounded={8} showLabel={false} />
                            <span className={`km-badge ${g.lead.type}`}>
                              {g.lead.minute.replace(/\.$/, '')}'
                            </span>
                            <span className="cu-upnext-play">
                              <Play size={18} fill="currentColor" />
                            </span>
                          </div>
                          <span className="cu-upnext-label">{highlightLabel(g)}</span>
                        </button>
                      );
                    })}
                  </div>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
