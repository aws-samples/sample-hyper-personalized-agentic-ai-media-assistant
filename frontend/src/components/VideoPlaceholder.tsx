import { MonitorPlay } from 'lucide-react';
import './VideoPlaceholder.css';

interface VideoPlaceholderProps {
  /** Tint hues so the placeholder picks up the game/title colors. */
  hueA?: number;
  hueB?: number;
  label?: string;
  sublabel?: string;
  /** Show the LIVE tag on the placeholder frame. */
  live?: boolean;
}

/**
 * Explicit "video would play here" placeholder. Used wherever real playback
 * isn't wired yet (e.g. the sports player before a stream URL exists), so the
 * layout reads as a real player frame rather than an empty gradient.
 */
export function VideoPlaceholder({
  hueA = 210,
  hueB = 260,
  label = 'Video would play here',
  sublabel = 'Playback placeholder — no stream connected',
  live = false,
}: VideoPlaceholderProps) {
  const bg = `radial-gradient(120% 120% at 30% 20%, hsl(${hueA} 40% 22%) 0%, hsl(${hueA} 45% 12%) 45%, hsl(${hueB} 50% 8%) 100%)`;
  return (
    <div className="video-ph" style={{ background: bg }}>
      <div className="video-ph-frame">
        {live && <span className="badge-live">LIVE</span>}
        <div className="video-ph-inner">
          <span className="video-ph-icon">
            <MonitorPlay size={34} />
          </span>
          <span className="video-ph-label">{label}</span>
          <span className="video-ph-sub">{sublabel}</span>
        </div>
      </div>
    </div>
  );
}
