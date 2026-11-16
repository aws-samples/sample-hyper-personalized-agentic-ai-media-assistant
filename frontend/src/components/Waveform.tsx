import './Waveform.css';

interface WaveformProps {
  bars?: number;
  active?: boolean;
  size?: 'sm' | 'md' | 'lg';
}

/** Animated voice waveform. Purple = the AI/voice layer. */
export function Waveform({ bars = 28, active = true, size = 'md' }: WaveformProps) {
  return (
    <div className={`waveform ${size} ${active ? 'active' : 'idle'}`} aria-hidden>
      {Array.from({ length: bars }).map((_, i) => (
        <span
          key={i}
          className="wf-bar"
          style={{
            animationDelay: `${(i % 7) * 0.09}s`,
            ['--h' as string]: `${30 + ((i * 37) % 70)}%`,
          }}
        />
      ))}
    </div>
  );
}
