import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { Mic, MicOff, ArrowLeft, Keyboard, Volume2, VolumeX } from 'lucide-react';
import { useDevice } from '../../components/device';
import { MovieRail } from '../../components/MovieRail';
import { Waveform } from '../../components/Waveform';
import { MockVoiceClient } from '../../lib/mockVoiceClient';
import { AgentSearchClient, agentConfigured } from '../../lib/agentSearchClient';
import { NovaSonicVoiceClient, voiceConfigured } from '../../lib/novaSonicVoiceClient';
import type { VoiceClient, VoiceState, VoiceResult } from '../../lib/voiceClient';
import type { Movie } from '../../lib/api';
import { getTappableOptions } from '../../lib/api';
import { useActiveProfile } from '../../lib/household';
import { useContentNav } from '../../lib/contentNav';
import { detailFromMovie } from '../TitleDetail/detail';
import { track } from '../../lib/events';
import './Search.css';

// Starter suggestion chips on the idle search screen ("tappable options").
// They come from the member's taste profile via GET /home?part=options; the
// backend gives a profile with no taste yet three generic ones at random.
// FALLBACK_PROMPTS shows only when that call fails or there is no backend.
const FALLBACK_PROMPTS = [
  'A feel-good movie under two hours',
  'A tense sci-fi thriller',
  'Award-winning crime dramas',
];

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

/** One conversation turn in the search chat. */
interface Turn {
  id: number;
  query: string;
  reply: string;
  results: VoiceResult[];
  followups: string[];
  /** Recommendation (result cards coming) vs a plain chat reply. */
  expect: boolean;
  /** The results event arrived (possibly empty) -- stop showing the skeleton. */
  answered?: boolean;
}

export function Search() {
  const device = useDevice();
  const navigate = useNavigate();

  const [mode, setMode] = useState<VoiceState>('idle');
  const [transcript, setTranscript] = useState(''); // live STT while listening
  const [status, setStatus] = useState('');
  // Conversation history: each query+reply+results is a turn. Kept so the user
  // can scroll up to previous answers; the newest turn snaps to the top.
  const [turns, setTurns] = useState<Turn[]>([]);
  const [hasSearched, setHasSearched] = useState(false);
  const [showKeyboard, setShowKeyboard] = useState(false);
  // Assistant voice (audio output) mute. Persisted like a streaming service's
  // trailer-mute so it stays set across reloads/sessions.
  const [voiceMuted, setVoiceMuted] = useState(
    () => localStorage.getItem('streamos.voiceMuted') === '1',
  );
  // Whether the mic is currently open. Once you've spoken, the mic stays live
  // (continuous), so a follow-up doesn't need another tap — you can just talk.
  // If you've only typed so far, the mic was never opened, so you must tap.
  const [micLive, setMicLive] = useState(false);
  // Whether YOU (the person asking) are muted. This gates the mic input without
  // closing the session, so the mute button stays put and you can unmute any
  // time — including while the agent is still talking.
  const [micMuted, setMicMuted] = useState(false);
  const { openTitle } = useContentNav();
  const [text, setText] = useState('');
  const idRef = useRef(0);
  // Maps a client turn token -> the turn.id it created, so async events
  // (results/reply) route back to the turn that asked instead of always the
  // newest one. Prevents an interrupted/slow turn's results from overwriting
  // the turn currently on screen.
  const turnIdByToken = useRef<Map<number, number>>(new Map());
  const bodyRef = useRef<HTMLDivElement>(null);
  const currentTurnRef = useRef<HTMLDivElement>(null);

  const activeProfile = useActiveProfile();
  // Agent calls carry the member's profile_id; starter chips are still per persona.
  const profileSlug = activeProfile?.backendProfile ?? '';
  const slugRef = useRef(profileSlug);
  slugRef.current = profileSlug;
  const [prompts, setPrompts] = useState<string[]>([]);
  useEffect(() => {
    let active = true;
    setPrompts([]);
    if (!profileSlug) {
      setPrompts(FALLBACK_PROMPTS);
      return;
    }
    getTappableOptions(profileSlug)
      .then((opts) => active && setPrompts(opts.length ? opts : FALLBACK_PROMPTS))
      .catch(() => active && setPrompts(FALLBACK_PROMPTS));
    return () => {
      active = false;
    };
  }, [profileSlug]);

  // Start a new turn. Voice clients re-emit the submitted text via
  // onFinalTranscript right after a tap/type already added it, so ignore that
  // echo (same query, still pending) to avoid a duplicate turn.
  const startTurn = (q: string, token?: number) => {
    setTurns((ts) => {
      const last = ts[ts.length - 1];
      if (last && last.query === q && !last.reply && last.results.length === 0) {
        // Echo of a turn the UI already created (tap/type then onFinalTranscript);
        // bind the client's token to that existing turn instead of duplicating.
        if (token != null) turnIdByToken.current.set(token, last.id);
        return ts;
      }
      const id = ++idRef.current;
      if (token != null) turnIdByToken.current.set(token, id);
      return [
        ...ts,
        { id, query: q, reply: '', results: [], followups: [], expect: false },
      ];
    });
    setHasSearched(true);
  };

  const clientRef = useRef<VoiceClient | null>(null);
  const client = useMemo(() => {
    // Resolve which turn an event belongs to from its client token. Falls back
    // to the newest turn when there's no token (older clients) or the mapped
    // turn is gone.
    const targetIndex = (ts: Turn[], token?: number): number => {
      if (token != null) {
        const id = turnIdByToken.current.get(token);
        if (id != null) {
          const idx = ts.findIndex((x) => x.id === id);
          if (idx !== -1) return idx;
        }
      }
      return ts.length - 1;
    };
    const events = {
      onState: setMode,
      onStatus: setStatus,
      onPartialTranscript: setTranscript,
      onFinalTranscript: (t: string, token?: number) => {
        setTranscript(t);
        startTurn(t, token);
      },
      onIntent: (kind: 'recommendation' | 'chat', token?: number) =>
        setTurns((ts) => {
          const i = targetIndex(ts, token);
          return ts.map((x, j) => (j === i ? { ...x, expect: kind === 'recommendation' } : x));
        }),
      onResponse: (t: string, token?: number) =>
        setTurns((ts) => {
          const i = targetIndex(ts, token);
          return ts.map((x, j) => (j === i ? { ...x, reply: t } : x));
        }),
      onResults: (r: VoiceResult[], token?: number) =>
        setTurns((ts) => {
          const i = targetIndex(ts, token);
          return ts.map((x, j) => {
            if (j !== i) return x;
            // Write-once: a turn's cards render once and never get replaced, so
            // a duplicate/late result set can't swap the titles out from under
            // the user. (Empty sets don't lock — only real results do.)
            if (x.results.length > 0) return x;
            return { ...x, results: r, answered: true };
          });
        }),
      onFollowups: (f: string[], token?: number) =>
        setTurns((ts) => {
          const i = targetIndex(ts, token);
          return ts.map((x, j) => (j === i ? { ...x, followups: f } : x));
        }),
      onError: (m: string) => setStatus(m),
    };
    // One client, one interface. When voice is configured, BOTH the mic and
    // typed/tappable input go through Nova Sonic — voice as bidi_audio_input,
    // text as bidi_text_input (Nova 2 Sonic is cross-modal) — so it speaks
    // either way. Otherwise fall back to the text/STT agent, then the mock.
    const c: VoiceClient = voiceConfigured()
      ? new NovaSonicVoiceClient(events, () => slugRef.current)
      : agentConfigured()
        ? new AgentSearchClient(events, () => slugRef.current)
        : new MockVoiceClient(events);
    clientRef.current = c;
    return c;
  }, []);

  useEffect(() => () => clientRef.current?.cancel(), []);

  // Whether the active client can mute its spoken audio (Nova Sonic only).
  const canMuteVoice = Boolean(client.setOutputMuted);

  // Apply the persisted voice-mute to the client once it exists.
  useEffect(() => {
    client.setOutputMuted?.(voiceMuted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client]);

  const startListening = () => {
    setTranscript('');
    client.setMuted?.(false);
    client.start();
    setMicLive(true);
    setMicMuted(false);
  };

  const toggleVoice = () => {
    const next = !voiceMuted;
    setVoiceMuted(next);
    client.setOutputMuted?.(next);
    try {
      localStorage.setItem('streamos.voiceMuted', next ? '1' : '0');
    } catch {
      /* ignore storage failures */
    }
  };

  const reset = () => {
    client.cancel();
    setTranscript('');
    setStatus('');
    setTurns([]);
    setHasSearched(false);
    setShowKeyboard(false);
    setText('');
    setMicLive(false);
    setMicMuted(false);
    setMode('idle');
  };

  const runQuery = (q: string) => {
    const trimmed = q.trim();
    if (!trimmed) return;
    startTurn(trimmed);
    setText('');
    // Typed queries and tappable chips go through the same client — into Nova
    // Sonic as text input when voice is on, so the assistant speaks the reply.
    client.submitText(trimmed);
  };

  const submitText = (e: FormEvent) => {
    e.preventDefault();
    runQuery(text);
  };

  // Tap to start/stop on every device (a TV remote's select button fires
  // onClick on the focused element too, so this works with a remote).
  const tapToggle = () => {
    // The mic doubles as its own control. If it's live, tapping mutes/unmutes
    // YOU (the person asking) — a gate that keeps the session open, so it works
    // any time, including while the agent is thinking or talking. If it's off,
    // tapping starts listening (but not mid-thinking — no turn to start).
    if (micLive) {
      const next = !micMuted;
      setMicMuted(next);
      client.setMuted?.(next);
      return;
    }
    if (mode === 'thinking') return;
    startListening();
  };
  const micGesture = { onClick: tapToggle };
  const micLabel = !micLive
    ? 'Tap to talk'
    : micMuted
      ? 'Unmute microphone'
      : 'Mute microphone';

  const isListening = mode === 'listening';
  const isThinking = mode === 'thinking';
  const current = turns[turns.length - 1];
  const followups = current?.followups ?? [];

  // When a new turn is added, snap it to the top of the scroll area so the
  // newest prompt+response leads; older turns scroll up out of view.
  useEffect(() => {
    if (turns.length === 0) return;
    currentTurnRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }, [turns.length]);

  const back = () => (hasSearched ? reset() : navigate('/home'));

  // Assistant voice (sound) mute — the "trailer mute" control, pinned top-right.
  const voiceToggle = () =>
    canMuteVoice ? (
      <button
        type="button"
        className={`voice-toggle ${voiceMuted ? 'off' : ''}`}
        onClick={toggleVoice}
        aria-pressed={voiceMuted}
        aria-label={voiceMuted ? 'Unmute assistant voice' : 'Mute assistant voice'}
        title={voiceMuted ? 'Assistant voice off' : 'Assistant voice on'}
      >
        {voiceMuted ? <VolumeX size={18} /> : <Volume2 size={18} />}
      </button>
    ) : null;

  const composer = (placeholder: string) => (
    <form className="vs-composer" onSubmit={submitText}>
      <input
        className="vs-composer-input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={placeholder}
        aria-label={placeholder}
        autoFocus
      />
      <button type="submit" className="vs-composer-send" disabled={!text.trim()} aria-label="Search">
        ↑
      </button>
    </form>
  );

  // Follow-up input — TV leads with a big voice pill; mobile is a text+mic bar.
  const followUp = () => {
    // The single mic reflects three states: off (tap to talk), live+listening
    // (pulsing, tap to mute), and live+muted (tap to unmute).
    const micHot = micLive && !micMuted;
    const MicIcon = micMuted ? MicOff : Mic;
    const pillTitle = !micLive ? 'Ask a follow-up' : micMuted ? 'Muted' : 'Listening';
    const pillSub = !micLive ? 'Tap and speak' : micMuted ? 'Tap to unmute' : 'Tap to mute';

    // While the agent is thinking, the only relevant control is (un)muting your
    // own mic when it's live. No composing a new query mid-answer.
    if (isThinking) {
      if (!micLive) return null;
      if (device === 'tv') {
        return (
          <div className="followup-tv">
            <button className={`followup-pill ${micHot ? 'hot' : ''}`} {...micGesture} aria-label={micLabel}>
              <span className="fp-mic">
                <MicIcon size={22} />
              </span>
              <span className="fp-copy">
                <span className="fp-title">{pillTitle}</span>
                <span className="fp-sub">{pillSub}</span>
              </span>
            </button>
          </div>
        );
      }
      return (
        <div className="followup-mobile solo">
          <button className={`vs-followup-mic ${micHot ? 'hot' : ''}`} {...micGesture} aria-label={micLabel} title={micLabel}>
            <MicIcon size={18} />
          </button>
        </div>
      );
    }

    if (device === 'tv') {
      return (
        <div className="followup-tv">
          {showKeyboard ? (
            <>
              {composer('Type your follow-up…')}
              <button className="kbd-link" onClick={() => setShowKeyboard(false)}>
                <Mic size={15} /> Use voice instead
              </button>
            </>
          ) : (
            <>
              <button className={`followup-pill ${micHot ? 'hot' : ''}`} {...micGesture} aria-label={micLabel}>
                <span className="fp-mic">
                  <MicIcon size={22} />
                </span>
                <span className="fp-copy">
                  <span className="fp-title">{pillTitle}</span>
                  <span className="fp-sub">{pillSub}</span>
                </span>
              </button>
              <button className="kbd-link" onClick={() => setShowKeyboard(true)}>
                <Keyboard size={15} /> Use keyboard instead
              </button>
            </>
          )}
        </div>
      );
    }
    return (
      <div className="followup-mobile">
        <button className={`vs-followup-mic ${micHot ? 'hot' : ''}`} {...micGesture} aria-label={micLabel} title={micLabel}>
          <MicIcon size={18} />
        </button>
        {composer(micLive ? 'Ask a follow-up, or just speak…' : 'Ask a follow-up, or tap the mic…')}
      </div>
    );
  };



  // ============ CENTERED: idle / listening ============
  if (!hasSearched || isListening) {
    return (
      <div className={`screen search-screen ${device}`}>
        <div className="search-aurora" />
        <button className="search-close" onClick={back} aria-label="Back">
          <ArrowLeft size={20} />
        </button>
        {voiceToggle()}
        <div className="search-stage">
          <button className={`mic-btn ${isListening ? 'live' : ''}`} {...micGesture} aria-label={micLabel}>
            {isListening ? (
              <Waveform bars={device === 'tv' ? 9 : 8} size={device === 'tv' ? 'md' : 'sm'} />
            ) : (
              <Mic className="mic-glyph" size={device === 'tv' ? 46 : 34} />
            )}
            <span className="mic-ring" />
          </button>
          {isListening ? (
            <>
              <div className={`big-transcript ${micMuted ? 'dim' : ''}`}>
                {transcript}
                {!micMuted && <span className="caret" />}
              </div>
              <span className="listening-tag">{micMuted ? 'Muted — tap to resume' : 'Listening…'}</span>
            </>
          ) : (
            <>
              <h1 className="search-h1">Search by voice</h1>
              <p className="search-sub">Tap the mic and speak</p>
              <div className="prompts">
                <span className="prompts-label">Try saying…</span>
                <div className="prompts-list">
                  {prompts.map((p) => (
                    <button key={p} className="prompt-chip" onClick={() => runQuery(p)}>
                      <span className="pq">“</span>
                      {p}
                      <span className="pq">”</span>
                    </button>
                  ))}
                </div>
              </div>
              <div className="vs-or">
                <span>or type</span>
              </div>
              {composer('Search movies, shows, live sports')}
            </>
          )}
        </div>
      </div>
    );
  }

  // ============ SEARCHED: transcript of turns (newest snaps to top) ============
  return (
    <div className={`screen search-screen ${device}`}>
      <div className="search-aurora" />
      {voiceToggle()}
      <header className="results-head">
        <button className="search-close inline" onClick={back} aria-label="Back">
          <ArrowLeft size={18} />
        </button>
        <span className="results-kicker">Voice search</span>
        <span className="results-head-spacer" />
      </header>

      <div className="results-body" ref={bodyRef}>
        {turns.map((t, idx) => {
          const isCurrent = idx === turns.length - 1;
          const thinkingHere = isCurrent && isThinking;
          return (
            <div key={t.id} className="vs-turn" ref={isCurrent ? currentTurnRef : undefined}>
              <div className="results-lead">
                <h1 className="query-heading">
                  <Mic className="qh-mic" size={device === 'tv' ? 22 : 18} />
                  <span>“{t.query}”</span>
                </h1>
                {t.reply && <p className="ai-response">{t.reply}</p>}
                {thinkingHere && !t.reply && !t.expect && (
                  <span className="typing">
                    <span />
                    <span />
                    <span />
                  </span>
                )}
              </div>

              {thinkingHere && t.expect && !t.answered && t.results.length === 0 ? (
                <section className={`rail ${device} vs-skel`}>
                  <div className="rail-head">
                    <h3 className="rail-title">{status || 'Finding recommendations…'}</h3>
                  </div>
                  <div className="vs-skel-track">
                    {Array.from({ length: 5 }).map((_, i) => (
                      <div key={i} className="vs-skel-card" />
                    ))}
                  </div>
                </section>
              ) : (
                t.results.length > 0 && (
                  <MovieRail
                    title="Recommendations"
                    movies={t.results.map(resultToMovie)}
                    resetKey={t.results.map((r) => r.id).join(',')}
                    onSelect={(m) => {
                      // Behaviour event: the viewer opened one of the agent's picks.
                      if (m.id) track('agent_accept', 'agent', { item_id: m.id });
                      openTitle(detailFromMovie(m));
                    }}
                  />
                )
              )}
            </div>
          );
        })}
      </div>

      {(!isThinking || micLive) && (
        <div className="results-footer">
          {!isThinking && followups.length > 0 && (
            <div className="vs-refine">
              <span className="vs-refine-label">Refine</span>
              {followups.map((f) => (
                <button key={f} className="vs-refine-chip" onClick={() => runQuery(f)}>
                  {f}
                </button>
              ))}
            </div>
          )}
          <div className="results-bar">
            {followUp()}
          </div>
        </div>
      )}
    </div>
  );
}
