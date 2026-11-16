/**
 * Real-time voice VoiceClient backed by the Nova Sonic bidirectional agent
 * (`backend/movie-voice-agent`) running on an AgentCore WebSocket runtime.
 *
 * Transport: a single WebSocket. Mic audio streams up as base64 PCM16
 * (`bidi_audio_input`); Sonic streams back audio (`bidi_audio_stream`),
 * transcripts (`bidi_transcript_stream` / `bidi_text_response`), and
 * interruptions (`bidi_interruption`). Our voice agent additionally relays the
 * movie agent's structured results as a custom `recommendations` event and a
 * `tool_start` signal — those drive the poster rail and the thinking state.
 *
 * Auth: reuses AgentCore Identity (the same Cognito access token as the text
 * search path). Because the browser WebSocket API can't set an Authorization
 * header, the token rides in the `Sec-WebSocket-Protocol` subprotocol, as the
 * AgentCore docs prescribe. In local dev, `VITE_VOICE_WS_URL` (ws://localhost)
 * is used directly with no auth.
 */

import { env } from './env';
import { getAgentSessionId } from './agentSession';
import { getAccessToken } from './cognito';
import type { VoiceClient, VoiceEvents, VoiceResult } from './voiceClient';
import { SonicRecorder, SonicPlayer } from './audio/sonicAudio';

const DEBUG = import.meta.env.VITE_VOICE_DEBUG === 'true';
const log = (...a: unknown[]) => DEBUG && console.log('[voice]', ...a);

/** True when a voice endpoint (local URL or deployed ARN) is configured. */
export function voiceConfigured(): boolean {
  return Boolean(env.voiceWsUrl || env.agentCoreVoiceArn);
}

/** Active Nova Sonic sessions (those holding a mic / open socket). */
const activeVoiceClients = new Set<NovaSonicVoiceClient>();

/**
 * Tear down any live voice session (release the mic, close the socket).
 * Call this when navigating into a context that shouldn't keep search voice
 * running underneath it — e.g. opening the VOD player to watch a title.
 */
export function stopActiveVoiceSessions(): void {
  for (const client of [...activeVoiceClients]) client.cancel();
}

/** base64url-encode an ASCII string (JWT) for the WS subprotocol token. */
function base64Url(s: string): string {
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** Map an agent recommendation dict into the UI's VoiceResult shape. */
function toResult(rec: Record<string, unknown>): VoiceResult {
  const premiere = String(rec.premiere_date ?? '');
  const year = premiere.slice(0, 4);
  const genresRaw = rec.genres;
  const genres = Array.isArray(genresRaw)
    ? (genresRaw as string[])
    : typeof genresRaw === 'string'
      ? genresRaw.split(',').map((g) => g.trim()).filter(Boolean)
      : [];
  const cast = Array.isArray(rec.principalCastMembers) ? (rec.principalCastMembers as string[]) : [];
  return {
    id: String(rec.title_id ?? ''),
    title: String(rec.originalTitle ?? rec.title ?? rec.title_id ?? 'Untitled'),
    why: typeof rec.blurb === 'string' ? rec.blurb : '',
    posterUrl: typeof rec.image === 'string' ? rec.image : null,
    genres,
    year: /^\d{4}$/.test(year) ? Number(year) : null,
    rating: typeof rec.imdb_rating === 'number' ? rec.imdb_rating : null,
    plot: typeof rec.plot === 'string' ? rec.plot : null,
    cast,
  };
}

export class NovaSonicVoiceClient implements VoiceClient {
  private ev: VoiceEvents;
  private getProfile: () => string;
  private ws: WebSocket | null = null;
  private recorder = new SonicRecorder();
  private player = new SonicPlayer();
  // Shared across all clients (see agentSession.ts): one conversation per
  // profile across text, voice, scene and match. Read per request.
  private get sessionId(): string {
    return getAgentSessionId();
  }
  // Bumped by cancel(); start() captures it and aborts if it changes across an
  // await, so leaving mid-connection never proceeds to acquire the mic.
  private startGen = 0;

  // Connection mode. 'search' (default) = the search screen, unchanged.
  // 'analysis' = the VOD player: connects with mode=analysis and streams live
  // playback context so the backend's ask_about_scene tool has the timestamp.
  private mode: 'search' | 'analysis';
  private getSceneContext?: () => {
    timestampMs: number;
    videoName?: string;
    titleId?: string;
    title?: string;
  };
  private ctxTimer: ReturnType<typeof setInterval> | null = null;

  // Continuous open-mic streaming (Nova Sonic's native model + what the AWS
  // samples do); Sonic handles turn-taking and barge-in. `muted` is the only
  // gate — an explicit user mute — so we don't create audio-stream gaps.
  private muted = false;
  // Nova Sonic 2.0 only responds to a TEXT turn when an audio content block is
  // open (the reference app "always starts the session with microphone" for
  // this reason). For typed/tappable input we prime the audio content once with
  // a frame of silence so text turns elicit a spoken response.
  private audioPrimed = false;
  // Whether the mic is actively capturing (continuous audio is flowing).
  private micActive = false;
  // Silence keepalive for the no-mic (text/tappable) path. Empirically, a text
  // turn that interrupts an in-progress spoken reply stalls (Nova emits an
  // interruption + a fresh response_start, then never finishes the turn or
  // calls the tool) UNLESS audio is continuously flowing — which is why a
  // session started by voice never hits this. So when there's no live mic we
  // stream silence frames to mimic the (always-working) open-mic path.
  private keepaliveTimer: ReturnType<typeof setInterval> | null = null;

  // Per-turn state. Sonic streams the reply as interim chunks then a final
  // version; we keep committed (finalized) text separate from the live interim
  // so the two don't stack into duplicated text.
  private assistantCommitted = '';
  private assistantInterim = '';
  private sawTool = false;
  private chatTimer: ReturnType<typeof setTimeout> | null = null;

  // Turn routing. Each new user submission gets a monotonic token; we stamp the
  // events it produces with it so the UI binds results to the RIGHT turn. Recs
  // arrive asynchronously (the ~20s tool call), so an interrupted/earlier turn's
  // results must not land on the turn that's now on screen. `tool_start` and
  // `recommendations` both carry the tool `query`, so we map query -> turn at
  // tool_start and resolve it when the recommendations come back.
  private turnSeq = 0;
  private currentTurn = 0;
  private queryTurns = new Map<string, number>();

  constructor(
    events: VoiceEvents,
    getProfile: () => string,
    opts?: {
      mode?: 'search' | 'analysis';
      getSceneContext?: () => {
        timestampMs: number;
        videoName?: string;
        titleId?: string;
        title?: string;
      };
    },
  ) {
    this.ev = events;
    this.getProfile = getProfile;
    this.mode = opts?.mode ?? 'search';
    this.getSceneContext = opts?.getSceneContext;
  }

  /** Analysis mode: send the current playback context to the runtime so
   *  ask_about_scene answers about the right moment/title. */
  private pushSceneContext(): void {
    if (!this.getSceneContext) return;
    const c = this.getSceneContext();
    const frame = {
      type: 'scene_context',
      timestamp: Math.max(0, Math.floor(c.timestampMs || 0)),
      video_name: c.videoName ?? null,
      title_id: c.titleId ?? null,
      title: c.title ?? null,
    };
    // Debug aid: this is exactly what the backend matches scenes against — an
    // exact term on video_name + a timestamp range. Compare video_name here to
    // what's actually in the scene index when scene lookups fall back to
    // title-wide answers.
    log('scene_context →', frame);
    this.send(frame);
  }

  // ---- lifecycle ----
  async start(): Promise<void> {
    const gen = ++this.startGen;
    try {
      activeVoiceClients.add(this);
      await this.ensureConnected();
      // Bailed out (cancel()/navigation) while the socket was connecting — don't
      // go on to open the mic, or we'd acquire a stream nobody will release.
      if (gen !== this.startGen) return;
      // Open Nova Sonic's audio content block with a frame of silence BEFORE
      // streaming speech — same as the text path does via primeAudio(). Without
      // this, a voice-first turn streams speech into a not-yet-opened content
      // block and Nova drops it (the bug where voice only worked after a text
      // turn had already primed the block).
      this.primeAudio();
      this.resetTurn();
      await this.recorder.start((frame) => {
        if (this.muted) return;
        this.send({ type: 'bidi_audio_input', audio: frame, format: 'pcm', sample_rate: 16000, channels: 1 });
      });
      // Cancelled while the mic was being acquired — release it and stop.
      if (gen !== this.startGen) {
        this.recorder.stop();
        return;
      }
      // Live mic now provides continuous audio — no silence keepalive needed.
      this.micActive = true;
      this.stopSilenceKeepalive();
      this.ev.onState?.('listening');
    } catch (err) {
      log('start failed', err);
      this.ev.onError?.('Could not start voice. Check your mic and try again.');
      this.ev.onState?.('error');
    }
  }

  stop(): void {
    // Release the mic; Nova Sonic finalizes the turn on end-of-speech. Keep the
    // connection open for follow-ups.
    this.recorder.stop();
    this.micActive = false;
  }

  /** Cut any in-progress spoken audio without tearing down the session — e.g.
   *  closing the scene chat, so Sonic doesn't keep talking over the resumed
   *  video. The mic/socket stay intact for when the chat reopens. */
  stopSpeaking(): void {
    this.player.clear();
  }

  cancel(): void {
    this.startGen++; // invalidate any in-flight start() so it won't open the mic
    activeVoiceClients.delete(this);
    this.clearChatTimer();
    if (this.ctxTimer) {
      clearInterval(this.ctxTimer);
      this.ctxTimer = null;
    }
    this.stopSilenceKeepalive();
    this.micActive = false;
    this.recorder.stop();
    this.player.close();
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) this.ws.close();
    this.ws = null;
    this.audioPrimed = false;
    this.ev.onState?.('idle');
  }

  /** Explicit user mute: stop forwarding mic audio (capture keeps running so
   *  un-muting is instant). For an optional mute toggle in the UI. */
  setMuted(muted: boolean): void {
    this.muted = muted;
    // A muted mic sends no frames, so the audio input goes silent — same dead
    // state that stalls a barge-in text turn. Keep it alive with silence while
    // muted; on unmute, real mic audio resumes and the keepalive isn't needed.
    if (muted) this.startSilenceKeepalive();
    else this.stopSilenceKeepalive();
  }

  /** Mute the assistant's spoken audio (output). Cuts current + future speech;
   *  the mic, transcript, and on-screen results are untouched. */
  setOutputMuted(muted: boolean): void {
    this.player.setMuted(muted);
  }

  async submitText(text: string): Promise<void> {
    try {
      activeVoiceClients.add(this);
      await this.ensureConnected();
      // Ensure an audio content block is open so Nova Sonic 2.0 treats the text
      // as a live turn and responds (speaks), rather than filing it as history.
      this.primeAudio();
      // No real mic audio flowing (typed/tappable path, or the mic is muted):
      // keep audio alive with silence so a barge-in text turn recovers like the
      // open-mic path instead of stalling.
      if (!this.micActive || this.muted) this.startSilenceKeepalive();
      this.resetTurn();
      this.currentTurn = ++this.turnSeq;
      this.ev.onFinalTranscript?.(text, this.currentTurn);
      this.ev.onState?.('thinking');
      this.send({ type: 'bidi_text_input', text, role: 'user' });
    } catch (err) {
      log('submitText failed', err);
      this.ev.onError?.('Could not send that. Try again.');
    }
  }

  /** Send one ~100ms frame of 16kHz/16-bit PCM silence into the audio input. */
  private sendSilenceFrame(): void {
    const silence = new Uint8Array(3200);
    let bin = '';
    for (let i = 0; i < silence.length; i++) bin += String.fromCharCode(silence[i]);
    this.send({ type: 'bidi_audio_input', audio: btoa(bin), format: 'pcm', sample_rate: 16000, channels: 1 });
  }

  /** Open the audio content block once per connection with a frame of silence,
   *  so text-only turns elicit a spoken response (Nova Sonic 2.0 requirement). */
  private primeAudio(): void {
    if (this.audioPrimed) return;
    this.sendSilenceFrame();
    this.audioPrimed = true;
  }

  /** Keep the audio input alive with silence when there's no live mic, so
   *  text/tappable turns behave like the open-mic path (which never stalls on
   *  barge-in). No-op while the mic is actually capturing. */
  private startSilenceKeepalive(): void {
    if (this.keepaliveTimer) return;
    // Real mic audio is only flowing when the mic is active AND not muted.
    if (this.micActive && !this.muted) return;
    this.keepaliveTimer = setInterval(() => {
      if ((this.micActive && !this.muted) || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      this.sendSilenceFrame();
    }, 100);
  }

  private stopSilenceKeepalive(): void {
    if (this.keepaliveTimer) {
      clearInterval(this.keepaliveTimer);
      this.keepaliveTimer = null;
    }
  }

  // ---- connection ----
  private async ensureConnected(): Promise<void> {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;

    const { url, protocols } = await this.buildUrl();
    log('connecting', url);
    const ws = new WebSocket(url, protocols);
    this.ws = ws;
    this.audioPrimed = false; // fresh connection → audio content not yet open

    await new Promise<void>((resolve, reject) => {
      ws.onopen = () => {
        log('connected');
        resolve();
      };
      ws.onerror = (e) => {
        log('ws error', e);
        reject(new Error('WebSocket connection failed'));
      };
      ws.onclose = (e) => log('ws closed', e.code, e.reason);
      ws.onmessage = (e) => this.onMessage(e);
    });

    // First message: hand the agent our Cognito access token so it can call the
    // movie agent on our behalf (identity propagation — no server-side password).
    const token = await getAccessToken();
    // Forward our session id so the movie agent shares ONE session with the
    // browser (AgentCore session affinity + memory continuity), instead of the
    // voice runtime using its own per-container id.
    this.send({ type: 'auth', token: token ?? '', profile: this.getProfile(), mode: this.mode, session_id: this.sessionId });

    // Analysis mode: prime the scene context immediately, then refresh it on a
    // light interval so a mid-scene question uses the current playback position.
    if (this.mode === 'analysis' && this.getSceneContext) {
      this.pushSceneContext();
      if (!this.ctxTimer) this.ctxTimer = setInterval(() => this.pushSceneContext(), 1500);
    }
  }

  private async buildUrl(): Promise<{ url: string; protocols?: string[] }> {
    const profile = encodeURIComponent(this.getProfile() || '');
    // Local dev: direct ws:// to the agent, no auth.
    if (env.voiceWsUrl) {
      const sep = env.voiceWsUrl.includes('?') ? '&' : '?';
      return { url: `${env.voiceWsUrl}${sep}profile=${profile}` };
    }
    // Deployed: wss:// to AgentCore with the OAuth bearer subprotocol.
    const token = await getAccessToken();
    if (!token) throw new Error('Not signed in');
    const arn = env.agentCoreVoiceArn as string;
    const enc = encodeURIComponent(arn);
    const url =
      `wss://bedrock-agentcore.${env.awsRegion}.amazonaws.com/runtimes/${enc}/ws` +
      `?qualifier=DEFAULT` +
      `&X-Amzn-Bedrock-AgentCore-Runtime-Session-Id=${this.sessionId}` +
      `&X-Amzn-Bedrock-AgentCore-Runtime-Custom-profile=${profile}`;
    const protocols = ['base64UrlBearerAuthorization', `base64UrlBearerAuthorization.${base64Url(token)}`];
    return { url, protocols };
  }

  private send(event: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(event));
    }
  }

  // ---- inbound events ----
  private onMessage(e: MessageEvent): void {
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(e.data as string);
    } catch {
      return;
    }
    const type = data.type;
    log('recv', type);

    switch (type) {
      case 'bidi_audio_stream':
        if (typeof data.audio === 'string') {
          this.player.enqueue(data.audio, Number(data.sample_rate) || 16000);
        }
        break;

      case 'bidi_transcript_stream': {
        const role = data.role === 'user' ? 'user' : 'assistant';
        const isFinal = data.is_final !== false;
        const text = String(data.text ?? '');
        if (role === 'user') this.handleUserTranscript(text, isFinal);
        else this.handleAssistantText(text, isFinal);
        break;
      }

      case 'bidi_text_response':
        if (typeof data.text === 'string') this.handleAssistantText(data.text, true);
        break;

      case 'bidi_interruption':
        this.player.clear();
        break;

      // ---- custom events from our voice agent ----
      case 'tool_start':
        this.sawTool = true;
        this.clearChatTimer();
        // Bind this tool call's query to the turn that's active right now, so
        // its (async) results are routed back to THIS turn even if the user has
        // moved on to another turn by the time they arrive.
        this.queryTurns.set(String(data.query ?? ''), this.currentTurn);
        // Drop the pre-search filler ("let me pull up a few options...") so the
        // reply area shows only the final answer once results come back.
        this.assistantCommitted = '';
        this.assistantInterim = '';
        this.ev.onResponse?.('', this.currentTurn);
        // Don't show the result skeleton yet: the call may be an answer-only
        // turn. The 'executor' status below means a real search has started.
        this.ev.onState?.('thinking');
        break;

      // Real progress update forwarded by the voice agent from the movie
      // agent's pipeline (e.g. "Searching for movies...", "Found 12 movies").
      // Shown on screen only — never spoken.
      case 'status':
        if (typeof data.message === 'string' && data.message) {
          this.ev.onStatus?.(data.message);
        }
        if (data.phase === 'executor') {
          const turn = this.queryTurns.get(String(data.query ?? '')) ?? this.currentTurn;
          this.ev.onIntent?.('recommendation', turn);
        }
        break;

      case 'recommendations': {
        this.sawTool = true;
        this.clearChatTimer();
        const recs = (data['movie-recommendation'] as Record<string, unknown>[]) ?? [];
        const followups = Array.isArray(data.tappable_options) ? (data.tappable_options as string[]) : [];
        // Presigned playback URLs keyed by title_id, streamed separately from the
        // rec objects. Fold them back on so clicking a result can play video.
        const contentUrls = (data.content_urls as Record<string, string>) ?? {};
        // Route to the turn that requested this (bound at tool_start), NOT
        // whatever turn is current now — an earlier/interrupted turn's results
        // must not overwrite the turn on screen.
        const turn = this.queryTurns.get(String(data.query ?? '')) ?? this.currentTurn;
        this.ev.onIntent?.('recommendation', turn);
        // Note: the reply text shown is Sonic's spoken transcript (matches the
        // audio), not the agent's response text — so we don't set onResponse here.
        this.ev.onResults?.(
          recs
            .map(toResult)
            .map((r) => ({ ...r, contentUrl: contentUrls[r.id] ?? null }))
            .filter((r) => r.id),
          turn,
        );
        this.ev.onFollowups?.(followups, turn);
        this.ev.onState?.('results');
        break;
      }

      // The movie agent answered a question about titles already shown (or found
      // nothing): no new cards. Mark the tool call's turn answered so the result
      // skeleton clears; the reply shown is Sonic's spoken transcript.
      case 'answer': {
        this.sawTool = true;
        this.clearChatTimer();
        const turn = this.queryTurns.get(String(data.query ?? '')) ?? this.currentTurn;
        const followups = Array.isArray(data.tappable_options) ? (data.tappable_options as string[]) : [];
        this.ev.onIntent?.('chat', turn);
        this.ev.onResults?.([], turn);
        this.ev.onFollowups?.(followups, turn);
        this.ev.onState?.('results');
        break;
      }

      default:
        break;
    }
  }

  private handleUserTranscript(text: string, isFinal: boolean): void {
    if (!isFinal) {
      this.ev.onPartialTranscript?.(text);
      return;
    }
    if (!text) return;
    // A completed user utterance = a new query/turn.
    this.resetTurn();
    this.currentTurn = ++this.turnSeq;
    this.ev.onFinalTranscript?.(text, this.currentTurn);
    this.ev.onState?.('thinking');
  }

  private handleAssistantText(text: string, isFinal: boolean): void {
    if (!text) return;
    if (isFinal) {
      // The final event carries the authoritative text for this segment —
      // commit it and drop the interim (which was the same content streaming in).
      this.assistantCommitted = this.assistantCommitted
        ? `${this.assistantCommitted} ${text}`.trim()
        : text;
      this.assistantInterim = '';
    } else {
      // Interim chunks arrive as deltas; accumulate for a live "typing" feel.
      this.assistantInterim += text;
    }
    const combined = `${this.assistantCommitted} ${this.assistantInterim}`.trim();
    this.ev.onResponse?.(combined, this.currentTurn);
    // If Sonic answers without calling the movie tool, it's a chat reply.
    // Debounce so a "let me look..." filler in a recommendation turn doesn't
    // briefly flash as a chat answer before tool_start arrives.
    if (isFinal && !this.sawTool) this.scheduleChatFinalize();
  }

  private scheduleChatFinalize(): void {
    this.clearChatTimer();
    const turn = this.currentTurn;
    this.chatTimer = setTimeout(() => {
      if (this.sawTool) return;
      this.ev.onIntent?.('chat', turn);
      this.ev.onState?.('results');
    }, 700);
  }

  private clearChatTimer(): void {
    if (this.chatTimer) {
      clearTimeout(this.chatTimer);
      this.chatTimer = null;
    }
  }

  private resetTurn(): void {
    this.clearChatTimer();
    this.assistantCommitted = '';
    this.assistantInterim = '';
    this.sawTool = false;
  }
}
