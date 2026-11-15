import { useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent, type MouseEvent } from 'react';
import { ArrowLeft, Play, Pause, Volume2, VolumeX, BarChart3, Sparkles, Mic, X, ListVideo, ChevronDown } from 'lucide-react';
import { useDevice } from '../../components/device';
import { VideoPlaceholder } from '../../components/VideoPlaceholder';
import { Crest } from '../../components/Crest';
import { Waveform } from '../../components/Waveform';
import { getLiveGameVideoUrl } from '../../lib/sportsClient';
import { attachStream } from '../../lib/hls';
import { track } from '../../lib/events';
import { MatchAgentClient } from '../../lib/matchAgentClient';
import {
  matchStateConfigured,
  getMeta,
  getState,
  getHighlights,
  highlightSeekTarget,
  type MatchMeta,
  type MatchStateSnapshot,
  type Highlight,
  type PlayerCard as LivePlayerCard,
  type PbpRow,
} from '../../lib/sportsStateClient';
import {
  groupHighlightsToClips,
  highlightLabel,
  type HighlightClipGroup,
} from '../../lib/highlightClips';
import { Poster } from '../../components/Poster';
import { CatchUpPlayer } from './CatchUpPlayer';
import { useActiveFollowed } from '../../lib/followed';
import type { LiveGame, PlayerStat } from '../../data/types';
import './SportsPlayer.css';

interface SportsPlayerProps {
  game: LiveGame;
  onClose: () => void;
  /** Video-seconds offset to enter at ("Join now"). Default 0. */
  startAt?: number;
}

type StatsTab = 'players' | 'pbp';

interface ChatMsg {
  role: 'user' | 'assistant';
  text: string;
}

/** Contextual starter prompts for the live match. */
const MATCH_SUGGESTIONS = ["What just happened?", "What's the score?", 'Who has had the best chances?'];

/** Minimal browser speech-to-text, used to dictate into the composer (the
 *  same fallback the VOD scene assistant uses; full spoken replies come later). */
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

const teamGrad = (hue: number) => `linear-gradient(145deg, hsl(${hue} 65% 46%), hsl(${hue + 18} 60% 30%))`;

/** mm:ss for the scrubber readout. */
function fmtClock(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

/** Match clock label from the playhead, using /meta's per-half video_s ranges. */
function matchClock(vs: number, meta: MatchMeta): string {
  const h1 = meta.halves.firstHalf;
  const h2 = meta.halves.secondHalf;
  if (!h1 || !h2) return '';
  if (vs < h1.start_video_s) return "0'";
  if (vs <= h1.end_video_s) return `${Math.floor((vs - h1.start_video_s) / 60) + 1}'`;
  if (vs < h2.start_video_s) return 'HT';
  if (vs <= h2.end_video_s) return `${45 + Math.floor((vs - h2.start_video_s) / 60) + 1}'`;
  return 'FT';
}

export function SportsPlayer({ game, onClose, startAt = 0 }: SportsPlayerProps) {
  const device = useDevice();
  const followed = useActiveFollowed();
  const url = getLiveGameVideoUrl(game.id, device);
  const hasVideo = Boolean(url);
  const videoRef = useRef<HTMLVideoElement>(null);

  // Behaviour event: the viewer joined this live game (once per open).
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !hasVideo) return;
    const onPlaying = () => {
      track('live_join', 'sports', { game_id: game.id, video_s: Math.round(v.currentTime) });
      v.removeEventListener('playing', onPlaying);
    };
    v.addEventListener('playing', onPlaying);
    return () => v.removeEventListener('playing', onPlaying);
  }, [game.id, hasVideo]);

  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [statsOpen, setStatsOpen] = useState(false);
  const [tab, setTab] = useState<StatsTab>('players');

  // Match Assistant (text-first; voice comes later, like the VOD scene agent).
  const [chatOpen, setChatOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [pending, setPending] = useState(false);
  const [text, setText] = useState('');
  const [listening, setListening] = useState(false);
  // Playback position in seconds → the agent's `video_s` (spoiler bound).
  // Initialized to `startAt` so "Join now" enters at a fixed point:
  // attachStream seeks the video there and the first getState() is bounded to
  // it, so the scoreboard/clock match the entry point immediately.
  const [elapsed, setElapsed] = useState(startAt);
  const [duration, setDuration] = useState(0);
  const elapsedRef = useRef(startAt);
  elapsedRef.current = elapsed;
  const threadRef = useRef<HTMLDivElement>(null);
  const recRef = useRef<SR | null>(null);
  const finalRef = useRef('');
  const client = useMemo(() => new MatchAgentClient(), []);

  // Live match data (the synthetic match) comes from the deployed match-state
  // API for the soccer game; other games stay on their mock data.
  const isLiveMatch = game.sport === 'soccer' && matchStateConfigured();
  const [meta, setMeta] = useState<MatchMeta | null>(null);
  const [live, setLive] = useState<MatchStateSnapshot | null>(null);
  const [highlights, setHighlights] = useState<Highlight[]>([]);

  // Key Moments: highlights grouped to the promoted clip files; a hover panel
  // lets you watch a moment (plays its clip in an overlay) without losing the
  // live position.
  // Only reveal moments that have already happened at the current playhead, so
  // scrubbing back hides future clips (spoiler-safe). Dropping the future tail
  // preserves clip numbering (clip NN maps to the same file).
  const revealedHighlights = useMemo(
    () => highlights.filter((h) => h.video_s <= elapsed),
    [highlights, elapsed],
  );
  const clipGroups = useMemo(
    () => groupHighlightsToClips(revealedHighlights),
    [revealedHighlights],
  );
  const [momentsOpen, setMomentsOpen] = useState(false);
  // Open the shared clip player over any set of clip groups (all key moments,
  // or one player's tagged highlights) at a given start index.
  const [watch, setWatch] = useState<{ groups: HighlightClipGroup[]; start: number } | null>(null);
  // Which player's highlights are expanded in the Players tab (a followed one first).
  const [expandedPlayer, setExpandedPlayer] = useState<string | null>(null);
  const firstFollowed = followed.players.find((p) => p.gameId === game.id)?.name ?? null;
  const expandedInit = useRef(false);
  useEffect(() => {
    if (!expandedInit.current && firstFollowed) {
      expandedInit.current = true;
      setExpandedPlayer(firstFollowed);
    }
  }, [firstFollowed]);

  // One-time: teams/half ranges + the goal list for the jump-to rail.
  useEffect(() => {
    if (!isLiveMatch) return;
    getMeta().then((m) => m && setMeta(m));
    getHighlights().then(setHighlights);
  }, [isLiveMatch]);

  // Poll the scoreboard/stats bounded to the current playhead.
  useEffect(() => {
    if (!isLiveMatch) return;
    let active = true;
    const pull = () => getState(elapsedRef.current).then((s) => active && s && setLive(s));
    pull();
    const id = setInterval(pull, 3000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, [isLiveMatch]);

  // Mirror <video> state; the open action is a user gesture so unmuted
  // autoplay is allowed — fall back to muted if the browser still blocks it.
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !hasVideo || !url) return;
    // Attach the HLS (or progressive) source; re-seek to the current playhead so
    // switching renditions doesn't reset video_s.
    const detach = attachStream(v, url, elapsedRef.current);
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onTime = () => setElapsed(v.currentTime);
    const onMeta = () => setDuration(v.duration || 0);
    v.addEventListener('play', onPlay);
    v.addEventListener('pause', onPause);
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('loadedmetadata', onMeta);
    v.addEventListener('durationchange', onMeta);
    v.play().catch(() => {
      v.muted = true;
      setMuted(true);
      v.play().catch(() => {});
    });
    return () => {
      v.removeEventListener('play', onPlay);
      v.removeEventListener('pause', onPause);
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('loadedmetadata', onMeta);
      v.removeEventListener('durationchange', onMeta);
      detach();
    };
  }, [hasVideo, url]);

  // Placeholder (no real video yet): advance a simulated playhead so `video_s`
  // still moves. Harmless once the real stream is wired in.
  useEffect(() => {
    if (hasVideo) return;
    const id = setInterval(() => setElapsed((e) => e + 1), 1000);
    return () => clearInterval(id);
  }, [hasVideo]);

  // Keep the chat pinned to the newest message.
  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: 'smooth' });
  }, [messages, pending]);

  // Abort any in-flight agent request / mic on unmount.
  useEffect(
    () => () => {
      client.cancel();
      recRef.current?.stop();
    },
    [client],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (chatOpen) closeChat();
      else onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onClose, chatOpen]);

  const togglePlay = () => {
    const v = videoRef.current;
    if (hasVideo && v) {
      if (v.paused) v.play().catch(() => {});
      else v.pause();
    } else {
      setPlaying((p) => !p);
    }
  };

  const toggleMute = () => {
    setMuted((prev) => {
      const next = !prev;
      if (videoRef.current) videoRef.current.muted = next;
      return next;
    });
  };

  // The clip groups tagged to a given player (by name, from the feed).
  const playerClips = (name: string) =>
    clipGroups.filter((g) => g.members.some((m) => m.player === name));

  // Open the shared CatchUpPlayer over a set of clips; pause the live feed
  // behind it and resume on close.
  const openWatch = (groups: HighlightClipGroup[], start: number) => {
    if (!groups.length) return;
    setMomentsOpen(false);
    videoRef.current?.pause();
    setWatch({ groups, start });
  };
  const watchMoment = (group: (typeof clipGroups)[number]) => openWatch(clipGroups, group.n - 1);
  const watchPlayerClipAt = (name: string, idx: number) => openWatch(playerClips(name), idx);

  const closeWatch = () => {
    setWatch(null);
    videoRef.current?.play().catch(() => {});
  };

  // Chat and stats share the side rail — opening one closes the other.
  const openChat = () => {
    setStatsOpen(false);
    setChatOpen(true);
  };
  const closeChat = () => {
    client.cancel();
    recRef.current?.stop();
    setListening(false);
    setChatOpen(false);
  };

  const ask = async (q: string) => {
    const query = q.trim();
    if (!query || pending) return;
    const history = messages.slice(-6); // prior turns for continuity
    // Add the user's message + an empty assistant bubble the stream fills in.
    setMessages((m) => [...m, { role: 'user', text: query }, { role: 'assistant', text: '' }]);
    setText('');
    setPending(true);
    await client.ask(
      query,
      { videoS: Math.floor(elapsedRef.current), history, followed: followed.resolved },
      {
        onDelta: (chunk) =>
          setMessages((m) => {
            const next = [...m];
            const last = next[next.length - 1];
            if (last?.role === 'assistant') next[next.length - 1] = { ...last, text: last.text + chunk };
            return next;
          }),
        onError: (msg) =>
          setMessages((m) => {
            const next = [...m];
            const last = next[next.length - 1];
            if (last?.role === 'assistant' && !last.text) next[next.length - 1] = { ...last, text: msg };
            else next.push({ role: 'assistant', text: msg });
            return next;
          }),
      },
    );
    setPending(false);
  };

  const submitChat = (e: FormEvent) => {
    e.preventDefault();
    ask(text);
  };

  // Mic dictation via the browser's speech-to-text — fills the composer, then
  // sends on end. Text is always available too (this is the movie assistant's
  // fallback path). Nova Sonic spoken replies are a later layer.
  const toggleMic = () => {
    if (listening) {
      recRef.current?.stop();
      return;
    }
    const w = window as unknown as {
      SpeechRecognition?: new () => SR;
      webkitSpeechRecognition?: new () => SR;
    };
    const Ctor = w.SpeechRecognition || w.webkitSpeechRecognition;
    if (!Ctor) return;
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

  // Shared chat content (empty state + message rows), reused by the TV panel
  // and the mobile sheet so both read identically to the movie assistant.
  const chatBody = (
    <>
      {messages.length === 0 && !pending && (
        <div className="chat-empty">
          <span className="assistant-avatar">✦</span>
          <p>Ask about the match so far — spoiler-safe, I only know what's happened up to now.</p>
          <div className="chat-suggestions">
            {MATCH_SUGGESTIONS.map((s) => (
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
            {m.text ? (
              m.text
            ) : m.role === 'assistant' ? (
              <span className="typing">
                <span />
                <span />
                <span />
              </span>
            ) : null}
          </span>
        </div>
      ))}
    </>
  );

  const composer = (
    <form className="composer" onSubmit={submitChat}>
      <input
        className="composer-input"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={listening ? 'Listening…' : 'Ask about the match…'}
        aria-label="Ask about the match"
      />
      <button
        type="button"
        className={`composer-mic ${listening ? 'live' : ''}`}
        onClick={toggleMic}
        aria-label="Speak"
      >
        {listening ? <Waveform bars={5} size="sm" /> : <Mic size={18} />}
      </button>
    </form>
  );

  const progress = duration > 0 ? Math.min(1, elapsed / duration) : 0;
  const seek = (e: MouseEvent<HTMLDivElement>) => {
    const v = videoRef.current;
    if (!v || !duration) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    v.currentTime = pct * duration;
    setElapsed(pct * duration);
  };

  // Jump the feed to an absolute second (used by the goal rail / markers).
  const seekTo = (sec: number) => {
    const t = duration ? Math.max(0, Math.min(sec, duration - 1)) : Math.max(0, sec);
    const v = videoRef.current;
    if (v) {
      v.currentTime = t;
      v.play().catch(() => {});
    }
    setElapsed(t);
    if (isLiveMatch) getState(t).then((s) => s && setLive(s));
  };

  // Display values: live match data when available, else the mock game.
  const homeTeam = isLiveMatch && meta ? meta.teams.home : null;
  const guestTeam = isLiveMatch && meta ? meta.teams.guest : null;
  const homeCode = homeTeam?.three_letter_code ?? game.home.code;
  const awayCode = guestTeam?.three_letter_code ?? game.away.code;
  const homeScore = live && homeTeam ? live.score[homeTeam.short_name] ?? 0 : game.home.score;
  const awayScore = live && guestTeam ? live.score[guestTeam.short_name] ?? 0 : game.away.score;
  const clockLabel = isLiveMatch && meta ? matchClock(elapsed, meta) : game.clock;
  const periodLabel: string = live
    ? ({
        preMatch: 'Kickoff',
        firstHalf: '1st Half',
        halfTime: 'Half Time',
        secondHalf: '2nd Half',
        fullTime: 'Full Time',
      } as Record<string, string>)[live.period] ?? 'Live'
    : 'Live';
  const goals = revealedHighlights.filter((h) => h.type === 'goal');

  const scoreBug = (
    <div className="scorebug">
      <span className="badge-live">LIVE</span>
      <div className="sb-team">
        <span className="sb-logo" style={{ background: teamGrad(game.home.hue) }}>{homeCode}</span>
        <span className="sb-score">{homeScore}</span>
      </div>
      <div className="sb-team">
        <span className="sb-score">{awayScore}</span>
        <span className="sb-logo" style={{ background: teamGrad(game.away.hue) }}>{awayCode}</span>
      </div>
      <span className="sb-clock">{clockLabel}</span>
    </div>
  );

  // Timeline markers: real goals (clickable → jump to the clip start) for the
  // live match, otherwise the mock game's key plays.
  const timelineMarks =
    isLiveMatch && duration > 0
      ? goals.map((g, i) => (
          <button
            key={i}
            type="button"
            className="stl-mark scored"
            style={{ left: `${Math.min(100, (g.video_s / duration) * 100)}%` }}
            title={`${g.minute} ${g.player ?? ''} (${g.team ?? ''})`}
            onClick={(e) => {
              e.stopPropagation();
              seekTo(highlightSeekTarget(g));
            }}
          />
        ))
      : game.keyPlays.map((k, i) => (
          <span
            key={i}
            className={`stl-mark ${k.kind === 'goal' || k.kind === 'score' ? 'scored' : ''}`}
            style={{ left: `${10 + i * 18}%` }}
            title={`${k.clock} · ${k.label}`}
          />
        ));

  const keyTimeline = (
    <div className="sports-keyplays">
      <div className="skp-row">
        <span className="skp-label">Catch up with key plays</span>
        <span className="skp-time">
          {fmtClock(elapsed)} / {fmtClock(duration)}
        </span>
      </div>
      <div className="sports-timeline" onClick={seek} role="slider" aria-label="Seek" tabIndex={0}>
        <div className="stl-fill" style={{ width: `${progress * 100}%` }} />
        {timelineMarks}
        <span className="stl-knob" style={{ left: `${progress * 100}%` }} />
      </div>
    </div>
  );

  const playerCard = (p: PlayerStat) => (
    <div className="pulse-card" key={p.name}>
      <div className="pulse-head">
        <span
          className="pulse-avatar"
          style={{ background: teamGrad(p.team === 'home' ? game.home.hue : game.away.hue) }}
        >
          {(p.team === 'home' ? game.home : game.away).code}
        </span>
        <div className="pulse-id">
          <span className="pulse-name">
            {p.name} {p.hot && <span className="hot-dot">🔥</span>}
          </span>
          <span className="pulse-team">
            {(p.team === 'home' ? game.home : game.away).city}
            {p.star ? ' · All-Star' : ''}
          </span>
        </div>
        {p.star && <span className="pulse-rating">★★★★☆</span>}
      </div>
      <div className="pulse-line">{p.line}</div>
      {typeof p.form === 'number' && (
        <div className="pulse-form">
          <span>Form</span>
          <div className="form-bar">
            <span style={{ width: `${p.form}%` }} />
          </div>
          <b>{p.form}</b>
        </div>
      )}
    </div>
  );

  // Live player card: real notable player (goals/shots/cards) with a presigned
  // face-circle avatar; falls back to a team-color initial when no face exists.
  const livePlayerCard = (p: LivePlayerCard) => {
    const side = p.team === 'home' ? game.home : game.away;
    const clips = playerClips(p.name);
    const isFav = followed.isPlayer(p.name);
    const hasClips = clips.length > 0;
    // Any player can be expanded to see their stats; clips show when present.
    const expanded = expandedPlayer === p.name;

    const head = (
      <>
        {p.face_url ? (
          <span className="pulse-avatar face" style={{ background: teamGrad(side.hue) }}>
            <img src={p.face_url} alt={p.name} loading="lazy" />
          </span>
        ) : (
          <span className="pulse-avatar" style={{ background: teamGrad(side.hue) }}>
            {side.code}
          </span>
        )}
        <div className="pulse-id">
          <span className="pulse-name">{p.name}</span>
          <span className="pulse-team">{side.city}</span>
        </div>
        <span className="pulse-count">
          {hasClips && (
            <>
              <Play size={11} fill="currentColor" /> {clips.length}
            </>
          )}
          <ChevronDown size={14} className="pulse-chev" />
        </span>
      </>
    );

    return (
      <div
        className={`pulse-card ${isFav ? 'favorite' : ''} ${expanded ? 'expanded' : ''}`}
        key={p.player_id}
        style={{ ['--team' as string]: `hsl(${side.hue} 64% 54%)` } as CSSProperties}
      >
        {isFav && <span className="pulse-fav">★ Because you follow</span>}
        <button
          className="pulse-head"
          onClick={() => setExpandedPlayer(expanded ? null : p.name)}
          aria-expanded={expanded}
        >
          {head}
        </button>
        <div className="pulse-line">{p.line}</div>
        {expanded && hasClips && (
          <div className="pulse-clips">
            {clips.map((g, i) => {
              // Label by THIS player's action in the clip (a clip can bundle
              // two players when their moments overlapped in time).
              const m = g.members.find((x) => x.player === p.name) ?? g.lead;
              const minute = m.minute.replace(/\.$/, '');
              const type = m.type.charAt(0).toUpperCase() + m.type.slice(1);
              return (
                <button key={g.n} className="pulse-clip" onClick={() => watchPlayerClipAt(p.name, i)}>
                  <div className="pulse-clip-art">
                    {g.lead.thumbnail_url ? (
                      <img className="pulse-clip-img" src={g.lead.thumbnail_url} alt={type} loading="lazy" />
                    ) : (
                      <Poster hueA={side.hue} hueB={side.hue + 25} ratio="16/9" rounded={6} showLabel={false} />
                    )}
                    <span className="pulse-clip-min">{minute}'</span>
                    <span className="pulse-clip-play">
                      <Play size={13} fill="currentColor" />
                    </span>
                  </div>
                  <span className="pulse-clip-label">{type}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  // Play-by-play rows: live match events (newest-first, playhead-bounded) when
  // available, otherwise the mock game's key plays. Both share the same shape.
  const pbpRows: PbpRow[] =
    isLiveMatch && live?.play_by_play
      ? live.play_by_play
      : (game.keyPlays as unknown as PbpRow[]);

  // Match a play-by-play row to a revealed highlight clip (by player + minute),
  // so rows with footage get a play button. Only for the live match.
  const normMin = (s: string) => s.replace(/[^\d+]/g, '');
  const pbpClip = (row: PbpRow) =>
    isLiveMatch
      ? clipGroups.find((g) =>
          g.members.some(
            (m) => m.player === row.detail && normMin(m.minute) === normMin(row.clock),
          ),
        )
      : undefined;

  const playByPlay = (
    <div className="pbp">
      {pbpRows.map((k, i) => {
        const clip = pbpClip(k);
        const isFav = followed.isPlayer(k.detail);
        return (
          <div key={i} className={`pbp-row ${k.kind}`}>
            <span className="pbp-clock">{k.clock}</span>
            <span
              className="pbp-dot"
              style={{ background: teamGrad(k.team === 'home' ? game.home.hue : game.away.hue) }}
            />
            <div className="pbp-text">
              <span className="pbp-label">
                {k.label}
                {isFav && <span className="pbp-star" title="Your player">★</span>}
              </span>
              <span className="pbp-detail">{k.detail}</span>
            </div>
            {clip && (
              <button
                className="pbp-play"
                onClick={() => watchMoment(clip)}
                aria-label={`Watch ${k.detail || k.label}`}
              >
                <Play size={15} fill="currentColor" />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );

  return (
    <div className={`screen sports-player ${device} ${statsOpen ? 'with-stats' : ''}`} role="dialog" aria-modal="true">
      <div className="sports-stage">
        <div className="sports-bg">
          {hasVideo ? (
            <video
              ref={videoRef}
              className="sports-video"
              muted={muted}
              playsInline
              onClick={togglePlay}
            />
          ) : (
            <VideoPlaceholder
              hueA={game.home.hue}
              hueB={game.away.hue}
              label={`${game.home.city} vs ${game.away.city}`}
              sublabel="Live match video would play here"
            />
          )}
          <div className="player-scrim" />
        </div>

        <div className="sports-top">
          <button className="player-back" onClick={onClose} aria-label="Back">
            <ArrowLeft size={device === 'tv' ? 28 : 24} />
          </button>
          <span className="sports-league">{game.league}</span>
          {isLiveMatch && clipGroups.length > 0 && (
            <button className="km-btn" onClick={() => setMomentsOpen(true)}>
              <ListVideo size={16} /> Key Moments
            </button>
          )}
        </div>

        {/* The broadcast clip has no burnt-in score bug, so draw our own
            (live, playhead-bounded score and clock for the soccer match). */}
        {scoreBug}

        {momentsOpen && (
          <div className="km-panel-wrap" onClick={() => setMomentsOpen(false)}>
            <div className="km-panel" onClick={(e) => e.stopPropagation()}>
              <div className="km-panel-head">
                <h3>Key Moments</h3>
                <button className="km-panel-close" onClick={() => setMomentsOpen(false)} aria-label="Close">
                  <X size={18} />
                </button>
              </div>
              <div className="km-list">
                {clipGroups.map((g) => {
                  const min = g.lead.minute.replace(/\.$/, '');
                  const isFav = g.members.some((m) => followed.isPlayer(m.player));
                  const type = g.lead.type.charAt(0).toUpperCase() + g.lead.type.slice(1);
                  return (
                    <button
                      key={g.n}
                      className={`km-item ${isFav ? 'fav' : ''}`}
                      onClick={() => watchMoment(g)}
                    >
                      <div className="km-thumb">
                        {g.lead.thumbnail_url ? (
                          <img src={g.lead.thumbnail_url} alt={highlightLabel(g)} loading="lazy" />
                        ) : (
                          <Poster hueA={game.home.hue} hueB={game.away.hue} ratio="16/9" rounded={0} showLabel={false} />
                        )}
                        <span className={`km-min ${g.lead.type}`}>{min}'</span>
                        <span className="km-play">
                          <Play size={15} fill="currentColor" />
                        </span>
                      </div>
                      <div className="km-meta">
                        <span className="km-title">
                          {type}
                          {g.lead.player ? ` — ${g.lead.player}` : ''}
                          {isFav && <span className="km-star" title="Your player">★</span>}
                        </span>
                        <span className="km-sub">{min}' · {g.lead.team ?? game.league}</span>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        )}

        {watch && (
          <CatchUpPlayer
            groups={watch.groups}
            startIndex={watch.start}
            mode="single"
            orientation="horizontal"
            onClose={closeWatch}
          />
        )}

        <div className="sports-controls">
          {keyTimeline}
          <div className="controls-row">
            <div className="controls-left">
              <button className="ctrl-btn primary" onClick={togglePlay} aria-label={playing ? 'Pause' : 'Play'}>
                {playing ? <Pause size={22} /> : <Play size={22} fill="currentColor" />}
              </button>
              <button className="ctrl-btn" onClick={toggleMute} aria-label={muted ? 'Unmute' : 'Mute'}>
                {muted ? <VolumeX size={22} /> : <Volume2 size={22} />}
              </button>
              <span className="live-edge">
                <span className="le-dot" /> Live edge
              </span>
            </div>
            <div className="controls-right">
              <button
                className={`ctrl-btn voice-trigger ${chatOpen ? 'on' : ''}`}
                onClick={() => (chatOpen ? closeChat() : openChat())}
                aria-label="Match Assistant"
                title="Ask about the match"
              >
                <Sparkles size={22} />
              </button>
              <button
                className={`ctrl-btn ${statsOpen ? 'on' : ''}`}
                onClick={() => {
                  setChatOpen(false);
                  setStatsOpen((s) => !s);
                }}
                aria-label="Stats"
                title="Stats"
              >
                <BarChart3 size={22} />
              </button>
            </div>
          </div>
        </div>
      </div>

      {statsOpen && (
        <aside className={`stats-panel ${device}`}>
          {device === 'mobile' && (
            <button
              className="drawer-handle drawer-handle-btn"
              onClick={() => setStatsOpen(false)}
              aria-label="Close stats"
            />
          )}
          <div className="stats-tabs">
            <button className={`stab ${tab === 'players' ? 'on' : ''}`} onClick={() => setTab('players')}>
              Players
            </button>
            <button className={`stab ${tab === 'pbp' ? 'on' : ''}`} onClick={() => setTab('pbp')}>
              Play-by-play
            </button>
            {device === 'mobile' && (
              <button
                className="stats-close"
                onClick={() => setStatsOpen(false)}
                aria-label="Close stats"
              >
                <X size={18} />
              </button>
            )}
          </div>
          <div className="stats-scroll">
            {tab === 'players' ? (
              <>
                {game.sport === 'soccer' && (
                  <div className="mh">
                    <div className="mh-row">
                      <div className="mh-side">
                        <Crest code={game.home.code} className="mh-crest" />
                        <span className="mh-code" style={{ color: `hsl(${game.home.hue} 70% 62%)` }}>
                          {homeCode}
                        </span>
                      </div>
                      <div className="mh-center">
                        <div className="mh-score">
                          <span>{homeScore}</span>
                          <i>–</i>
                          <span>{awayScore}</span>
                        </div>
                        <div className="mh-status">
                          <span className="mh-dot" />
                          {periodLabel} · {clockLabel}
                        </div>
                      </div>
                      <div className="mh-side">
                        <Crest code={game.away.code} className="mh-crest" />
                        <span className="mh-code" style={{ color: `hsl(${game.away.hue} 70% 62%)` }}>
                          {awayCode}
                        </span>
                      </div>
                    </div>
                    <div className="mh-venue">
                      {game.venue ?? game.league}
                    </div>
                  </div>
                )}
                {isLiveMatch && live && homeTeam && guestTeam && (
                  <div className="team-stat-summary">
                    <div className="tss-title">Match stats</div>
                    {(
                      [
                        ['shots', 'Shots'],
                        ['on_target', 'On target'],
                        ['corners', 'Corners'],
                        ['fouls', 'Fouls'],
                      ] as const
                    ).map(([k, label]) => {
                      const h = live.team_stats[homeTeam.short_name]?.[k] ?? 0;
                      const a = live.team_stats[guestTeam.short_name]?.[k] ?? 0;
                      const hPct = h + a > 0 ? (h / (h + a)) * 100 : 50;
                      return (
                        <div className="tss-row" key={k}>
                          <div className="tss-row-top">
                            <b className={h >= a ? 'lead' : ''}>{h}</b>
                            <span>{label}</span>
                            <b className={a >= h ? 'lead' : ''}>{a}</b>
                          </div>
                          <div className="tss-bar">
                            <span
                              className="tss-bar-h"
                              style={{ width: `${hPct}%`, background: teamGrad(game.home.hue) }}
                            />
                            <span
                              className="tss-bar-a"
                              style={{ width: `${100 - hPct}%`, background: teamGrad(game.away.hue) }}
                            />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
                {isLiveMatch && live?.players && (
                  <div className="players-label">Player spotlight</div>
                )}
                {isLiveMatch && live?.players
                  ? [...live.players]
                      .sort(
                        (a, b) =>
                          Number(followed.isPlayer(b.name)) - Number(followed.isPlayer(a.name)),
                      )
                      .map(livePlayerCard)
                  : game.players.map(playerCard)}
              </>
            ) : (
              <>
                <div className="pbp-head">Play-by-play</div>
                {playByPlay}
              </>
            )}
          </div>
        </aside>
      )}

      {chatOpen && device === 'tv' && (
        <div className="voice-panel">
          <div className="voice-panel-head">
            <span className="assistant-avatar sm">✦</span>
            <span className="voice-panel-title">Match Assistant</span>
            <button className="voice-panel-close" onClick={closeChat} aria-label="Close">
              <X size={18} />
            </button>
          </div>
          <div className="voice-panel-body" ref={threadRef}>
            {chatBody}
          </div>
          <div className="voice-panel-composer">{composer}</div>
        </div>
      )}

      {chatOpen && device === 'mobile' && (
        // Bottom sheet OVER the live player — the video stays mounted so the
        // playhead (video_s) is continuous while chatting (spoiler bound).
        <aside className="assistant-sheet mobile">
          <div className="drawer-handle" />
          <div className="chat-head">
            <span className="assistant-avatar sm">✦</span>
            Match Assistant
            <button className="chat-close" onClick={closeChat} aria-label="Close chat">
              <X size={16} />
            </button>
          </div>
          <div className="chat-thread" ref={threadRef}>
            {chatBody}
          </div>
          <div className="chat-composer-wrap">{composer}</div>
        </aside>
      )}
    </div>
  );
}
