/**
 * Real VoiceClient backed by the deployed AgentCore recommendation agent.
 *
 * Text path: POSTs the query to the AgentCore runtime `invocations` endpoint
 * with a Cognito ACCESS token as the bearer (AgentCore Identity inbound auth —
 * the same direct-invoke pattern the previous frontend shipped), and streams
 * SSE events back.
 *
 * Voice path: uses the browser Web Speech API for speech-to-text when available
 * (Chrome/Edge), then submits the transcript to the same agent. Full duplex
 * voice (Nova Sonic) is a separate transport added later behind this interface.
 */

import { env } from './env';
import { getAgentSessionId } from './agentSession';
import { getAccessToken } from './cognito';
import type { VoiceClient, VoiceEvents, VoiceResult } from './voiceClient';

/** True when the agent runtime is configured. */
export function agentConfigured(): boolean {
  return Boolean(env.agentCoreAgentArn || env.agentCoreAgentId);
}

function agentArn(): string {
  if (env.agentCoreAgentArn) return env.agentCoreAgentArn;
  return `arn:aws:bedrock-agentcore:${env.awsRegion}:${env.awsAccountId}:runtime/${env.agentCoreAgentId}`;
}

function invocationsUrl(): string {
  const arn = encodeURIComponent(agentArn());
  return `https://bedrock-agentcore.${env.awsRegion}.amazonaws.com/runtimes/${arn}/invocations?qualifier=DEFAULT`;
}

/** Map an agent recommendation item into the UI's VoiceResult shape. */
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

type SpeechRecognitionInstance = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start: () => void;
  stop: () => void;
  onresult: ((e: unknown) => void) | null;
  onerror: ((e: unknown) => void) | null;
  onend: (() => void) | null;
};

export class AgentSearchClient implements VoiceClient {
  private ev: VoiceEvents;
  private getProfile: () => string;
  // Shared across all clients (see agentSession.ts): one conversation per
  // profile across text, voice, scene and match. Read per request.
  private get sessionId(): string {
    return getAgentSessionId();
  }
  private abort: AbortController | null = null;
  private recognition: SpeechRecognitionInstance | null = null;
  private finalTranscript = '';
  private intentSent = false;
  // Monotonic per-submission token so the UI binds results to the turn that
  // asked. Bumped on each submitText; a superseded request is also aborted.
  private turnSeq = 0;

  constructor(events: VoiceEvents, getProfile: () => string) {
    this.ev = events;
    this.getProfile = getProfile;
  }

  // ---- Voice (browser STT → agent) ----
  start() {
    const w = window as unknown as {
      SpeechRecognition?: new () => SpeechRecognitionInstance;
      webkitSpeechRecognition?: new () => SpeechRecognitionInstance;
    };
    const SR = w.SpeechRecognition || w.webkitSpeechRecognition;
    if (!SR) {
      this.ev.onError?.('Voice input needs Chrome or Edge — type your search instead.');
      return;
    }
    this.finalTranscript = '';
    const rec = new SR();
    rec.lang = 'en-US';
    rec.interimResults = true;
    rec.continuous = false;
    rec.onresult = (e: unknown) => {
      const ev = e as { results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> };
      let interim = '';
      for (let i = 0; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) this.finalTranscript += r[0].transcript;
        else interim += r[0].transcript;
      }
      this.ev.onPartialTranscript?.((this.finalTranscript + interim).trim());
    };
    rec.onerror = () => this.ev.onError?.("Didn't catch that — try again or type your search.");
    rec.onend = () => {
      const q = this.finalTranscript.trim();
      if (q) void this.submitText(q);
      else this.ev.onState?.('idle');
    };
    this.recognition = rec;
    this.ev.onState?.('listening');
    rec.start();
  }

  stop() {
    this.recognition?.stop();
  }

  cancel() {
    this.recognition?.stop();
    this.recognition = null;
    this.abort?.abort();
    this.abort = null;
    this.ev.onState?.('idle');
  }

  // ---- Text (typed → agent) ----
  async submitText(text: string) {
    // Supersede any in-flight request so its late SSE events can't overwrite
    // this new turn's results (the "titles appear then change" bug).
    this.abort?.abort();
    this.intentSent = false;
    const turn = ++this.turnSeq;
    this.ev.onFinalTranscript?.(text, turn);
    this.ev.onState?.('thinking');
    this.ev.onStatus?.('Sending your request…');

    const token = await getAccessToken();
    if (!token) {
      this.ev.onError?.('Please sign in again.');
      this.ev.onState?.('error');
      return;
    }

    this.abort = new AbortController();
    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': this.sessionId,
    };
    const body = JSON.stringify({
      session_id: this.sessionId,
      profile: this.getProfile(),
      user_query: text,
      request_type: 'recommendation',
    });

    try {
      const res = await fetch(invocationsUrl(), {
        method: 'POST',
        headers,
        body,
        signal: this.abort.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`Agent request failed: ${res.status}`);
      }
      await this.readStream(res.body.getReader(), turn);
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      this.ev.onError?.('Search failed. Please try again.');
      this.ev.onState?.('error');
    }
  }

  /** Parse the SSE stream ("data: {...}") and dispatch events. `turn` is the
   *  submission token so the UI routes these events to the right turn. */
  private async readStream(reader: ReadableStreamDefaultReader<Uint8Array>, turn: number) {
    const decoder = new TextDecoder();
    let buffer = '';
    const handle = (raw: string) => {
      const line = raw.trim();
      if (!line.startsWith('data: ')) return;
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(line.slice(6));
      } catch {
        return;
      }
      const type = data.type;
      if (type === 'status') {
        const phase = String(data.phase ?? '');
        const message = String(data.message ?? 'Working…');
        if (!this.intentSent) {
          if (phase === 'router_done') {
            this.intentSent = true;
            this.ev.onIntent?.(message.toLowerCase().includes('recommendation') ? 'recommendation' : 'chat', turn);
          } else if (phase === 'executor') {
            // Only retrieval means cards are coming. 'planner' runs before the
            // agent knows the turn is an answer-only reply.
            this.intentSent = true;
            this.ev.onIntent?.('recommendation', turn);
          }
        }
        this.ev.onStatus?.(message);
      } else if (type === 'response') {
        const recs = (data['movie-recommendation'] as Record<string, unknown>[]) ?? [];
        // The agent streams playable presigned S3 URLs separately, keyed by
        // title_id, to keep the recommendation objects small. Fold them back
        // onto each result so the UI can play real video.
        const contentUrls = (data.content_urls as Record<string, string>) ?? {};
        const followups = Array.isArray(data.tappable_options)
          ? (data.tappable_options as string[])
          : [];
        // An answer to a question about titles already shown: a chat reply, no cards.
        if (data.intent === 'answer') this.ev.onIntent?.('chat', turn);
        this.ev.onResponse?.(String(data.response ?? ''), turn);
        this.ev.onResults?.(
          recs
            .map(toResult)
            .map((r) => ({ ...r, contentUrl: contentUrls[r.id] ?? null }))
            .filter((r) => r.id),
          turn,
        );
        this.ev.onFollowups?.(followups, turn);
        this.ev.onState?.('results');
      } else if (type === 'error') {
        this.ev.onError?.(String(data.message ?? 'Something went wrong.'));
        this.ev.onState?.('error');
      }
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const l of lines) handle(l);
    }
    if (buffer) handle(buffer);
  }
}
