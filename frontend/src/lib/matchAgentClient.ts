/**
 * Match-assistant client — "ask about the live match".
 *
 * Talks to the sports AgentCore runtime (backend/live-sports-agent),
 * which is a DIFFERENT runtime and a DIFFERENT wire
 * contract than the movie agents:
 *
 *   - Request:  { question, video_s, history }   (video_s = seconds into the
 *               VOD playhead — the match agent is spoiler-safe and only knows
 *               what has happened up to that moment)
 *   - Response: a STREAM of { delta: "token" } chunks (token-by-token), ended
 *               by the stream closing (and a `data: [DONE]` sentinel when the
 *               local FastAPI service.py is the backend). Errors arrive as
 *               { error: "..." }.
 *
 * This is why it's a sibling of sceneAgentClient rather than a reuse of it: the
 * plumbing (Cognito token, invocations URL, SSE read-loop) is identical, but the
 * body we send and the events we parse differ.
 *
 * When no runtime is configured (VITE_AGENTCORE_MATCH_ARN unset, e.g. local UI
 * work), a canned answer streams back so the panel is demoable with no backend.
 */

import { env } from './env';
import { getAgentSessionId } from './agentSession';
import { getAccessToken } from './cognito';

/** True when the match agent runtime is configured (else callers use the mock). */
export function matchAgentConfigured(): boolean {
  return Boolean(env.agentCoreMatchArn);
}

function invocationsUrl(): string {
  const arn = encodeURIComponent(env.agentCoreMatchArn as string);
  return `https://bedrock-agentcore.${env.awsRegion}.amazonaws.com/runtimes/${arn}/invocations?qualifier=DEFAULT`;
}

/** A prior conversation turn, in the shape the match agent expects. */
export interface MatchTurn {
  role: 'user' | 'assistant';
  text: string;
}

export interface MatchAskOptions {
  /** Current playback position, in seconds (the VOD playhead → spoiler bound). */
  videoS: number;
  /** Prior turns for multi-turn continuity. */
  history?: MatchTurn[];
  /** The viewer's resolved follows ({id, name}), so "my team" needs no name. */
  followed?: { id: string; name: string }[];
}

export interface MatchAskEvents {
  /** A streamed chunk of the answer (append to the current bubble). */
  onDelta?: (chunk: string) => void;
  /** The full answer text, once the stream ends. */
  onDone?: (fullText: string) => void;
  onError?: (message: string) => void;
}

/** Canned, streamed answer for local UI work (no runtime configured). */
const MOCK_REPLY =
  "The match agent isn't wired up in this environment yet, so this is a stand-in " +
  'reply. Once VITE_AGENTCORE_MATCH_ARN is set, this panel answers live from the ' +
  'match feed — bounded to the current moment on screen, so no spoilers.';

export class MatchAgentClient {
  // Shared across all clients (see agentSession.ts): one conversation per
  // profile across text, voice, scene and match. Read per request.
  private get sessionId(): string {
    return getAgentSessionId();
  }
  private abort: AbortController | null = null;

  /** Abort any in-flight request. */
  cancel() {
    this.abort?.abort();
    this.abort = null;
  }

  /** Ask a question about the live match. Resolves when the stream ends. */
  async ask(question: string, opts: MatchAskOptions, ev: MatchAskEvents): Promise<void> {
    const text = question.trim();
    if (!text) return;

    // ---- Mock fallback (no runtime configured) ----
    if (!matchAgentConfigured()) {
      const words = MOCK_REPLY.split(' ');
      let acc = '';
      for (const w of words) {
        // Bail out cleanly if cancelled mid-stream.
        if (this.abort?.signal.aborted) return;
        const chunk = (acc ? ' ' : '') + w;
        acc += chunk;
        ev.onDelta?.(chunk);
        await new Promise((r) => setTimeout(r, 28));
      }
      ev.onDone?.(acc);
      return;
    }

    const token = await getAccessToken();
    if (!token) {
      ev.onError?.('Please sign in again.');
      return;
    }

    this.abort = new AbortController();
    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': this.sessionId,
    };
    const body = JSON.stringify({
      question: text,
      video_s: Math.max(0, opts.videoS),
      history: opts.history ?? [],
      followed: opts.followed ?? [],
    });

    try {
      const res = await fetch(invocationsUrl(), {
        method: 'POST',
        headers,
        body,
        signal: this.abort.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`Match agent request failed: ${res.status}`);
      }
      await this.readStream(res.body.getReader(), ev);
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      ev.onError?.('Sorry, something went wrong. Please try again.');
    } finally {
      this.abort = null;
    }
  }

  /**
   * Parse the SSE stream and dispatch deltas. Handles both the AgentCore
   * runtime framing and the local service.py framing ("data: {delta}" lines
   * plus a "data: [DONE]" sentinel).
   */
  private async readStream(
    reader: ReadableStreamDefaultReader<Uint8Array>,
    ev: MatchAskEvents,
  ) {
    const decoder = new TextDecoder();
    let buffer = '';
    let full = '';

    const handle = (raw: string) => {
      const line = raw.trim();
      if (!line.startsWith('data:')) return;
      const payload = line.slice(5).trim();
      if (!payload || payload === '[DONE]') return;
      let data: Record<string, unknown>;
      try {
        data = JSON.parse(payload);
      } catch {
        return;
      }
      if (typeof data.delta === 'string') {
        full += data.delta;
        ev.onDelta?.(data.delta);
      } else if (typeof data.error === 'string') {
        ev.onError?.(data.error);
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

    ev.onDone?.(full);
  }
}
