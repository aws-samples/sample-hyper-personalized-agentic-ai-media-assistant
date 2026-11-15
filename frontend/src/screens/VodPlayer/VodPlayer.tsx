import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type PointerEvent,
} from 'react';
import {
  Play,
  Pause,
  SkipBack,
  SkipForward,
  Volume2,
  VolumeX,
  Subtitles,
  Maximize,
  X,
  Mic,
  MicOff,
  ArrowLeft,
  Sparkles,
  Keyboard,
} from 'lucide-react';
import { useDevice } from '../../components/device';
import { Poster } from '../../components/Poster';
import { Waveform } from '../../components/Waveform';
import { useActiveProfile } from '../../lib/household';
import { getVideoUrl, type Movie } from '../../lib/api';
import {
  NovaSonicVoiceClient,
  stopActiveVoiceSessions,
  voiceConfigured,
} from '../../lib/novaSonicVoiceClient';
import {
  SceneAgentClient,
  DEFAULT_SCENE_TITLE_ID,
  sceneVideoForTitle,
} from '../../lib/sceneAgentClient';
import type { VoiceResult } from '../../lib/voiceClient';
import { MovieRail } from '../../components/MovieRail';
import { useContentNav } from '../../lib/contentNav';
import { detailFromMovie, type DetailItem } from '../TitleDetail/detail';
import { useClockPlaybackEvents, usePlaybackEvents } from '../../lib/usePlaybackEvents';
import './VodPlayer.css';

/** VoiceResult → Movie, so the scene chat renders the same rail as search. */
function resultToMovie(r: VoiceResult): Movie {
  return {
    id: r.id,
    title: r.title,
    posterUrl: r.posterUrl,
    rating: r.rating,
    genres: r.genres,
    plot: r.plot,
    cast: r.cast ?? [],
    maturity: r.maturity,
    year: r.year,
    blurb: r.why || undefined,
    contentUrl: r.contentUrl ?? null,
  };
}

interface VodPlayerProps {
  item: DetailItem;
  onClose: () => void;
}

interface ChatMsg {
  role: 'user' | 'assistant';
  text: string;
}

/** Fallback runtime for the simulated clock when there's no real video. */
const FALLBACK_DURATION_MS = 128 * 60 * 1000; // 2h 08m

function fmt(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) ms = 0;
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

/** Minimal browser speech-to-text, used to fill the composer from the mic. */
type SR = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start: () => void;
  stop: () => void;
  onresult: ((e: unknown) => void) | null;
  onerror: (() => void) | null;
  onend: (() => void) | null;
};

export function VodPlayer({ item, onClose }: VodPlayerProps) {
  const device = useDevice();

  // The agent attaches a presigned URL for titles it recommends; for titles
  // that arrive without one (Home / top-picks), resolve it via get_video_url.
  const [resolvedUrl, setResolvedUrl] = useState<string | null>(item.contentUrl ?? null);
  const contentUrl = item.contentUrl ?? resolvedUrl;
  const hasVideo = Boolean(contentUrl);
  // True once we know whether a video exists (lookup finished or not needed),
  // so the simulated clock doesn't report a view that turns into a real one.
  const [urlChecked, setUrlChecked] = useState(Boolean(item.contentUrl) || !item.id);
  const videoRef = useRef<HTMLVideoElement>(null);
  usePlaybackEvents(videoRef, hasVideo && item.id
    ? { itemId: item.id, contentType: 'movie', surface: 'vod_player' }
    : null);

  const [playing, setPlaying] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [duration, setDuration] = useState(FALLBACK_DURATION_MS);
  // No video file: the poster and running clock still report as a real view.
  useClockPlaybackEvents(
    !hasVideo && urlChecked && item.id
      ? { itemId: item.id, contentType: 'movie', surface: 'vod_player' }
      : null,
    { playing, positionMs: elapsed, durationMs: duration },
  );
  const [muted, setMuted] = useState(false);
  const [chatOpen, setChatOpen] = useState(false);
  const [controlsVisible, setControlsVisible] = useState(true);

  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [pending, setPending] = useState(false);
  const [status, setStatus] = useState('');
  const [followups, setFollowups] = useState<string[]>([]);
  const [text, setText] = useState('');
  const [listening, setListening] = useState(false);
  const [transcript, setTranscript] = useState(''); // live STT while listening
  const [showKeyboard, setShowKeyboard] = useState(false); // mic-first; text optional
  // Assistant voice (audio output) mute — cuts Sonic's spoken reply. Persisted
  // and shared with the search screen (same key), so it's one preference.
  const [voiceMuted, setVoiceMuted] = useState(
    () => localStorage.getItem('streamos.voiceMuted') === '1',
  );
  // Mic (input) controls — mirrors the search chat. micLive = mic session open
  // (stays open across turns); micMuted = you muted yourself (gated, session
  // stays up so you can unmute any time, even while the agent is talking).
  const [micLive, setMicLive] = useState(false);
  const [micMuted, setMicMuted] = useState(false);

  const threadRef = useRef<HTMLDivElement>(null);
  const recRef = useRef<SR | null>(null);
  const finalRef = useRef('');
  const hideTimer = useRef<number | null>(null);

  const profileSlug = useActiveProfile()?.backendProfile ?? '';
  const slugRef = useRef(profileSlug);
  slugRef.current = profileSlug;

  const { openTitle } = useContentNav();
  const [results, setResults] = useState<VoiceResult[]>([]);
  const assistantIdxRef = useRef<number | null>(null);

  // Fallback text path (used only when no voice runtime is configured).
  const client = useMemo(() => new SceneAgentClient(() => slugRef.current), []);

  // Entering the player means we're now watching — tear down any live voice-
  // search session so its mic/Nova Sonic socket doesn't keep running behind
  // the player (and collide with the scene assistant's mic).
  useEffect(() => {
    stopActiveVoiceSessions();
  }, []);

  const hueA = item.hueA ?? 220;
  const hueB = item.hueB ?? 275;

  // The OpenSearch scene index is keyed by an exact `video_name`, so we send
  // the indexed name for this title (NOT the streamed file's name — the index
  // has no entry for that, which was silently falling back to whole-movie
  // cast). Titles that aren't indexed send an empty name and get a "not
  // available" answer. Add titles in SCENE_VIDEO_BY_TITLE as their scenes get
  // analyzed.
  const sceneTitleId = item.id || DEFAULT_SCENE_TITLE_ID;
  const sceneVideoName = sceneVideoForTitle(sceneTitleId);

  // Refs so the voice client's periodic scene_context push reads live values.
  const elapsedRef = useRef(elapsed);
  elapsedRef.current = elapsed;
  const sceneVideoNameRef = useRef(sceneVideoName);
  sceneVideoNameRef.current = sceneVideoName;

  // Nova Sonic voice for the scene chat (analysis mode) when a voice runtime is
  // configured. Its events drive the same chat state as the text path; the
  // spoken reply is Sonic's, and any recommendation crossover renders as a rail.
  const voiceClient = useMemo(() => {
    if (!voiceConfigured()) return null;
    return new NovaSonicVoiceClient(
      {
        onState: (s) => {
          setListening(s === 'listening');
          if (s === 'thinking') setPending(true);
        },
        onStatus: setStatus,
        onPartialTranscript: setTranscript,
        onFinalTranscript: (t) => {
          setTranscript(t);
          setMessages((m) => [...m, { role: 'user', text: t }]);
          setResults([]);
          setFollowups([]);
          assistantIdxRef.current = null;
          setPending(true);
        },
        onResponse: (t) => {
          // An empty payload is the tool_start reset that clears pre-tool
          // filler. Drop any filler bubble and STAY in the thinking state so the
          // dots remain up until the real answer streams in — mirroring search's
          // `isThinking && !reply`. Finalizing here (pending=false + an empty
          // bubble) was what made the dots vanish and the panel go blank during
          // tool execution.
          if (!t) {
            setMessages((m) =>
              m.length > 0 && m[m.length - 1].role === 'assistant' ? m.slice(0, -1) : m,
            );
            assistantIdxRef.current = null;
            setPending(true);
            return;
          }
          setPending(false);
          setMessages((m) => {
            const next = [...m];
            if (assistantIdxRef.current == null) {
              next.push({ role: 'assistant', text: t });
              assistantIdxRef.current = next.length - 1;
            } else {
              next[assistantIdxRef.current] = { role: 'assistant', text: t };
            }
            return next;
          });
        },
        onResults: (r) => setResults(r),
        onFollowups: setFollowups,
        onError: (msg) => {
          setPending(false);
          setMessages((m) => [...m, { role: 'assistant', text: msg }]);
        },
      },
      () => slugRef.current,
      {
        mode: 'analysis',
        getSceneContext: () => ({
          timestampMs: elapsedRef.current,
          videoName: sceneVideoNameRef.current,
          titleId: sceneTitleId,
          title: item.title,
        }),
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Apply the persisted assistant-voice mute to the client once it exists.
  useEffect(() => {
    voiceClient?.setOutputMuted?.(voiceMuted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voiceClient]);

  const toggleVoice = () => {
    const next = !voiceMuted;
    setVoiceMuted(next);
    voiceClient?.setOutputMuted?.(next);
    try {
      localStorage.setItem('streamos.voiceMuted', next ? '1' : '0');
    } catch {
      /* ignore storage failures */
    }
  };

  // Assistant-voice (sound) mute button — only when a voice runtime is wired.
  const canMuteVoice = Boolean(voiceClient?.setOutputMuted);
  const voiceToggleBtn = canMuteVoice ? (
    <button
      type="button"
      className={`voice-mute-toggle ${voiceMuted ? 'off' : ''}`}
      onClick={toggleVoice}
      aria-pressed={voiceMuted}
      aria-label={voiceMuted ? 'Unmute assistant voice' : 'Mute assistant voice'}
      title={voiceMuted ? 'Assistant voice off' : 'Assistant voice on'}
    >
      {voiceMuted ? <VolumeX size={18} /> : <Volume2 size={18} />}
    </button>
  ) : null;

  const suggestions = [
    "What's happening in this scene?",
    'Who are the actors in this scene?',
    `Recommend movies similar to ${item.title}`,
  ];

  // Resolve a playable URL when the title didn't come with one.
  useEffect(() => {
    if (item.contentUrl || !item.id) return;
    let cancelled = false;
    getVideoUrl(item.id)
      .then((url) => {
        if (!cancelled && url) setResolvedUrl(url);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setUrlChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [item.contentUrl, item.id]);

  // Simulated clock (no real video): advance while playing.
  useEffect(() => {
    if (hasVideo || !playing) return;
    const id = setInterval(() => setElapsed((e) => Math.min(e + 1000, duration)), 1000);
    return () => clearInterval(id);
  }, [hasVideo, playing, duration]);

  // Real <video>: mirror its state into React, and try to autoplay on load.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const onTime = () => setElapsed(v.currentTime * 1000);
    const onMeta = () => setDuration((v.duration || 0) * 1000);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('loadedmetadata', onMeta);
    v.addEventListener('play', onPlay);
    v.addEventListener('pause', onPause);
    v.play().then(() => setPlaying(true)).catch(() => setPlaying(false));
    return () => {
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('loadedmetadata', onMeta);
      v.removeEventListener('play', onPlay);
      v.removeEventListener('pause', onPause);
    };
  }, [hasVideo]);

  // Start the simulated clock once (no real video).
  useEffect(() => {
    if (!hasVideo) setPlaying(true);
  }, [hasVideo]);

  // Auto-hide chrome while playing; always show when paused or chatting.
  const bumpControls = useCallback(() => {
    setControlsVisible(true);
    if (hideTimer.current) window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => setControlsVisible(false), 3000);
  }, []);

  useEffect(() => {
    if (hideTimer.current) window.clearTimeout(hideTimer.current);
    if (!playing || chatOpen) {
      setControlsVisible(true);
      return;
    }
    hideTimer.current = window.setTimeout(() => setControlsVisible(false), 3000);
    return () => {
      if (hideTimer.current) window.clearTimeout(hideTimer.current);
    };
  }, [playing, chatOpen]);

  // Keep the chat scrolled to the newest message.
  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, pending]);

  // Close on Escape (chat first, then the player).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (chatOpen) closeChat();
      else onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatOpen]);

  useEffect(
    () => () => {
      client.cancel();
      voiceClient?.cancel();
      recRef.current?.stop();
    },
    [client, voiceClient],
  );

  const progress = duration > 0 ? Math.min(1, elapsed / duration) : 0;

  // Play/pause always reflects the real element (never a stale flag).
  const togglePlay = () => {
    const v = videoRef.current;
    if (hasVideo && v) {
      if (v.paused) v.play().catch(() => {});
      else v.pause();
    } else {
      setPlaying((p) => !p);
    }
  };
  const doPause = () => {
    const v = videoRef.current;
    if (hasVideo && v) v.pause();
    else setPlaying(false);
  };
  const doPlay = () => {
    const v = videoRef.current;
    if (hasVideo && v) v.play().catch(() => {});
    else setPlaying(true);
  };

  const openChat = () => {
    doPause();
    setChatOpen(true);
  };
  const closeChat = () => {
    recRef.current?.stop();
    voiceClient?.stop();
    // Always cut Sonic's in-progress speech on close so it doesn't keep talking
    // over the movie once playback resumes.
    voiceClient?.stopSpeaking();
    setListening(false);
    setMicLive(false);
    setMicMuted(false);
    setShowKeyboard(false);
    setChatOpen(false);
    doPlay();
  };

  const skip = (deltaMs: number) => {
    const next = Math.max(0, Math.min(duration, elapsed + deltaMs));
    if (hasVideo && videoRef.current) videoRef.current.currentTime = next / 1000;
    else setElapsed(next);
  };

  // Seek from a pointer x-position within the progress bar. Pointer events
  // unify mouse + touch, so this works for a tap, a click, or a drag/scrub.
  const scrubbingRef = useRef(false);
  const seekToClientX = (clientX: number, el: HTMLDivElement) => {
    const rect = el.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const next = pct * duration;
    if (hasVideo && videoRef.current) videoRef.current.currentTime = next / 1000;
    else setElapsed(next);
  };
  const onSeekPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    scrubbingRef.current = true;
    // Capture so drag keeps seeking even if the finger leaves the thin bar.
    e.currentTarget.setPointerCapture?.(e.pointerId);
    seekToClientX(e.clientX, e.currentTarget);
  };
  const onSeekPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    if (!scrubbingRef.current) return;
    seekToClientX(e.clientX, e.currentTarget);
  };
  const onSeekPointerUp = (e: PointerEvent<HTMLDivElement>) => {
    scrubbingRef.current = false;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };

  const toggleMute = () => {
    setMuted((prev) => {
      const next = !prev;
      if (videoRef.current) videoRef.current.muted = next;
      return next;
    });
  };

  const ask = async (q: string) => {
    const query = q.trim();
    if (!query) return;
    // Voice path: Nova Sonic's events push the user bubble + spoken reply.
    if (voiceClient) {
      setText('');
      setFollowups([]);
      voiceClient.submitText(query);
      return;
    }
    // Fallback: text-only scene agent.
    if (pending) return;
    setMessages((m) => [...m, { role: 'user', text: query }]);
    setText('');
    setFollowups([]);
    setStatus('');
    setPending(true);
    await client.ask(
      query,
      { timestampMs: elapsed, videoName: sceneVideoName, titleId: sceneTitleId },
      {
        onStatus: setStatus,
        onResponse: (t) => setMessages((m) => [...m, { role: 'assistant', text: t }]),
        onFollowups: setFollowups,
        onError: (msg) => setMessages((m) => [...m, { role: 'assistant', text: msg }]),
      },
    );
    setPending(false);
    setStatus('');
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    ask(text);
  };

  // ---- Mic ----
  // Voice runtime configured → Nova Sonic (speaks the answer). Otherwise fall
  // back to the browser's speech-to-text filling the composer (text reply).
  const toggleMic = () => {
    if (voiceClient) {
      // If the mic is live, tapping mutes/unmutes YOU (a gate) — works any time,
      // including while the agent is thinking/talking. If it's off, tapping
      // starts listening (but not mid-thinking — no turn to start).
      if (micLive) {
        const next = !micMuted;
        setMicMuted(next);
        voiceClient.setMuted?.(next);
        return;
      }
      if (pending) return;
      setTranscript('');
      voiceClient.setMuted?.(false);
      voiceClient.start();
      setMicLive(true);
      setMicMuted(false);
      return;
    }
    if (listening) {
      recRef.current?.stop();
      return;
    }
    const w = window as unknown as {
      SpeechRecognition?: new () => SR;
      webkitSpeechRecognition?: new () => SR;
    };
    const Ctor = w.SpeechRecognition || w.webkitSpeechRecognition;
    if (!Ctor) {
      setStatus('Voice input needs Chrome or Edge — type your question instead.');
      return;
    }
    finalRef.current = '';
    const rec = new Ctor();
    rec.lang = 'en-US';
    rec.interimResults = true;
    rec.continuous = false;
    rec.onresult = (e: unknown) => {
      const ev = e as {
        results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>;
      };
      let interim = '';
      for (let i = 0; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) finalRef.current += r[0].transcript;
        else interim += r[0].transcript;
      }
      setText((finalRef.current + interim).trim());
    };
    rec.onerror = () => setListening(false);
    rec.onend = () => {
      setListening(false);
      const q = finalRef.current.trim();
      if (q) ask(q);
    };
    recRef.current = rec;
    setListening(true);
    rec.start();
  };

  const playerArt = (
    <div className="player-art">
      {hasVideo ? (
        <video
          ref={videoRef}
          className="player-video"
          src={contentUrl ?? undefined}
          muted={muted}
          playsInline
          onClick={togglePlay}
        />
      ) : item.posterUrl ? (
        <img
          className="player-poster"
          src={item.posterUrl}
          alt={item.title}
          onClick={togglePlay}
        />
      ) : (
        <Poster
          hueA={hueA}
          hueB={hueB}
          ratio="16/9"
          rounded={0}
          showLabel={false}
          style={{ height: '100%', width: '100%' }}
        />
      )}
    </div>
  );

  // Shared chat thread (mobile bottom sheet).
  const thread = (
    <div className="chat-thread" ref={threadRef}>
      {messages.length === 0 && !pending && (
        <div className="chat-empty">
          <span className="assistant-avatar">✦</span>
          <p>Ask me about what you're watching.</p>
          <div className="chat-suggestions">
            {suggestions.map((s) => (
              <button key={s} className="chip" onClick={() => ask(s)}>
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {messages.map((m, i) => (
        <div key={i} className={`chat-row ${m.role}`}>
          {m.role === 'assistant' && <span className="assistant-avatar sm">✦</span>}
          <span className={`chat-bubble ${m.role}`}>
            {m.role === 'user' && <Mic className="chat-mic" size={13} />}
            {m.text}
          </span>
        </div>
      ))}

      {results.length > 0 && (
        <div className="chat-recs">
          <MovieRail
            title="More like this"
            movies={results.map(resultToMovie)}
            resetKey={results.map((r) => r.id).join(',')}
            onSelect={(m) => openTitle(detailFromMovie(m))}
          />
        </div>
      )}

      {pending && (
        <div className="chat-row assistant">
          <span className="assistant-avatar sm">✦</span>
          <span className="chat-bubble assistant thinking">
            {/* Status text stands in for the animated dots (matches the search
                screen). Falls back to a generic line before the first status
                arrives or on replies that emit none (e.g. pleasantries). */}
            <span className="thinking-status">{status || 'Thinking…'}</span>
          </span>
        </div>
      )}
    </div>
  );

  const composer = (
    <div className="chat-composer-wrap">
      {followups.length > 0 && !pending && (
        <div className="followup-chips">
          {followups.map((f) => (
            <button key={f} className="chip" onClick={() => ask(f)}>
              {f}
            </button>
          ))}
        </div>
      )}
      <form className="composer" onSubmit={submit}>
        <input
          className="composer-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={listening ? 'Listening…' : 'Ask about this scene…'}
          aria-label="Ask about this scene"
        />
        <button
          type="button"
          className={`composer-mic ${micLive && !micMuted ? 'live' : ''} ${micMuted ? 'muted' : ''}`}
          onClick={toggleMic}
          aria-label={micMuted ? 'Unmute microphone' : micLive ? 'Mute microphone' : 'Speak'}
        >
          {micMuted ? (
            <MicOff size={18} />
          ) : listening ? (
            <Waveform bars={5} size="sm" />
          ) : (
            <Mic size={18} />
          )}
        </button>
      </form>
    </div>
  );

  // ================= MOBILE: chat open = mini-player + thread =================
  if (chatOpen && device === 'mobile') {
    return (
      <div className="screen vod-player mobile voice-chat" role="dialog" aria-modal="true">
        <div className="mini-player">
          {playerArt}
          <div className="mini-scrim" />
          <div className="mini-meta">
            <span className="mini-title">{item.title}</span>
            <span className="mini-time">
              {fmt(elapsed)} / {fmt(duration)}
            </span>
          </div>
          <button className="mini-play" onClick={togglePlay} aria-label={playing ? 'Pause' : 'Play'}>
            {playing ? <Pause size={18} /> : <Play size={18} />}
          </button>
          <div
            className="progress"
            onPointerDown={onSeekPointerDown}
            onPointerMove={onSeekPointerMove}
            onPointerUp={onSeekPointerUp}
          >
            <span className="progress-fill" style={{ width: `${progress * 100}%` }} />
          </div>
        </div>

        <div className="chat-head">
          <span className="assistant-avatar sm">✦</span>
          Scene Assistant
          <span className="chat-head-spacer" />
          {voiceToggleBtn}
          <button className="chat-close" onClick={closeChat} aria-label="Close chat">
            <X size={16} />
          </button>
        </div>

        {thread}
        {composer}
      </div>
    );
  }

  const tvVoiceOpen = chatOpen && device === 'tv';
  const chromeHidden = !controlsVisible && playing && !chatOpen;

  // Latest exchange only — the panel refreshes in place per turn (like search),
  // it is NOT a scrolling chat log.
  const lastUser = [...messages].reverse().find((m) => m.role === 'user');
  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
  const hasAsked = messages.length > 0 || pending;
  // Show the "thinking" dots whenever the current turn is still awaiting its
  // reply (i.e. the newest message is the user's). Gating on `!lastAssistant`
  // wrongly suppressed the indicator on every follow-up once any answer existed.
  const awaitingReply = pending && messages[messages.length - 1]?.role === 'user';

  // Mic handling — same single-mic model as the search chat (see toggleMic):
  // tap to start; once live, tap mutes/unmutes you (gated). Works on every
  // device (a TV remote's select button fires onClick too).
  const micGesture = { onClick: toggleMic };
  const micHot = micLive && !micMuted;

  // ================= STANDARD PLAYER + TV VOICE OVERLAY =================
  return (
    <div
      className={`screen vod-player ${device}`}
      role="dialog"
      aria-modal="true"
      onMouseMove={bumpControls}
    >
      {playerArt}
      <div className="player-scrim" />
      {tvVoiceOpen && <div className="player-dim" />}

      <div className={`player-top ${chromeHidden ? 'hidden' : ''}`}>
        <button className="player-back" onClick={onClose} aria-label="Back">
          <ArrowLeft size={device === 'tv' ? 28 : 24} />
        </button>
        <span className="player-title">{item.title}</span>
      </div>

      {tvVoiceOpen ? (
        <div className="voice-panel">
          <div className="voice-panel-head">
            <span className="assistant-avatar sm">✦</span>
            <span className="voice-panel-title">Scene Assistant</span>
            {voiceToggleBtn}
            <button className="voice-panel-close" onClick={closeChat} aria-label="Close">
              <X size={18} />
            </button>
          </div>

          <div className="voice-panel-body">
            {listening ? (
              <div className="voice-listening">
                <Waveform bars={9} size="md" />
                <p className="big-transcript">
                  {transcript}
                  <span className="caret" />
                </p>
                <span className="listening-tag">Listening…</span>
              </div>
            ) : !hasAsked ? (
              <>
                <p className="voice-panel-empty">Ask me about what you're watching.</p>
                <div className="vs-refine">
                  {suggestions.map((s) => (
                    <button key={s} className="vs-refine-chip" onClick={() => ask(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </>
            ) : (
              <>
                {lastUser && <p className="voice-panel-q">"{lastUser.text}"</p>}
                <p className="ai-response">
                  {awaitingReply ? (status || 'Thinking…') : lastAssistant?.text}
                </p>
                {!pending && results.length > 0 && (
                  <div className="voice-panel-recs">
                    <MovieRail
                      title="More like this"
                      movies={results.map(resultToMovie)}
                      resetKey={results.map((r) => r.id).join(',')}
                      onSelect={(m) => openTitle(detailFromMovie(m))}
                    />
                  </div>
                )}
                {!pending && followups.length > 0 && (
                  <div className="vs-refine">
                    {followups.map((f) => (
                      <button key={f} className="vs-refine-chip" onClick={() => ask(f)}>
                        {f}
                      </button>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          <div className="voice-panel-composer">
            {showKeyboard ? (
              <>
                <form className="vs-composer" onSubmit={submit}>
                  <input
                    className="vs-composer-input"
                    value={text}
                    onChange={(e) => setText(e.target.value)}
                    placeholder="Type your follow-up…"
                    aria-label="Ask about this scene"
                    autoFocus
                  />
                  <button
                    type="submit"
                    className="vs-composer-send"
                    disabled={!text.trim()}
                    aria-label="Send"
                  >
                    ↑
                  </button>
                </form>
                <button className="kbd-link" onClick={() => setShowKeyboard(false)}>
                  <Mic size={15} /> Use voice instead
                </button>
              </>
            ) : (
              <>
                <button
                  className={`followup-pill ${micHot ? 'hot' : ''}`}
                  {...micGesture}
                  aria-label={micMuted ? 'Unmute microphone' : micLive ? 'Mute microphone' : 'Tap to talk'}
                >
                  <span className="fp-mic">
                    {micMuted ? (
                      <MicOff size={22} />
                    ) : listening ? (
                      <Waveform bars={5} size="sm" />
                    ) : (
                      <Mic size={22} />
                    )}
                  </span>
                  <span className="fp-copy">
                    <span className="fp-title">
                      {micMuted ? 'Muted' : micLive ? 'Listening' : hasAsked ? 'Ask a follow-up' : 'Ask about this scene'}
                    </span>
                    <span className="fp-sub">
                      {micMuted ? 'Tap to unmute' : micLive ? 'Tap to mute' : 'Tap and speak'}
                    </span>
                  </span>
                </button>
                <button className="kbd-link" onClick={() => setShowKeyboard(true)}>
                  <Keyboard size={15} /> Use keyboard instead
                </button>
              </>
            )}
          </div>
        </div>
      ) : (
        <>
          {device === 'tv' && !playing && (
            <button className="center-play" onClick={togglePlay} aria-label="Play">
              <Play size={34} />
            </button>
          )}

          <div className={`player-controls ${chromeHidden ? 'hidden' : ''}`}>
            <div className="progress-row">
              <span className="time">{fmt(elapsed)}</span>
              <div
                className="progress"
                onPointerDown={onSeekPointerDown}
                onPointerMove={onSeekPointerMove}
                onPointerUp={onSeekPointerUp}
                role="slider"
                aria-label="Seek"
                tabIndex={0}
              >
                <span className="progress-fill" style={{ width: `${progress * 100}%` }}>
                  <span className="progress-knob" />
                </span>
              </div>
              <span className="time">-{fmt(duration - elapsed)}</span>
            </div>
            <div className="controls-row">
              <div className="controls-left">
                <button className="ctrl-btn" onClick={() => skip(-10000)} aria-label="Back 10 seconds">
                  <SkipBack size={22} />
                </button>
                <button
                  className="ctrl-btn primary"
                  onClick={togglePlay}
                  aria-label={playing ? 'Pause' : 'Play'}
                >
                  {playing ? <Pause size={24} /> : <Play size={24} />}
                </button>
                <button className="ctrl-btn" onClick={() => skip(10000)} aria-label="Forward 10 seconds">
                  <SkipForward size={22} />
                </button>
                <button className="ctrl-btn" onClick={toggleMute} aria-label={muted ? 'Unmute' : 'Mute'}>
                  {muted ? <VolumeX size={22} /> : <Volume2 size={22} />}
                </button>
                {device === 'mobile' && <span className="ctrl-title">{item.title}</span>}
              </div>
              <div className="controls-right">
                <button
                  className="ctrl-btn voice-trigger"
                  onClick={openChat}
                  aria-label="Ask about this scene"
                  title="Ask about this scene"
                >
                  <Sparkles size={22} />
                </button>
                <button className="ctrl-btn" aria-label="Subtitles">
                  <Subtitles size={22} />
                </button>
                <button className="ctrl-btn" aria-label="Fullscreen">
                  <Maximize size={22} />
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
