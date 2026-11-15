/**
 * Scene-analysis client — "ask about what you're watching".
 *
 * Talks to the SAME deployed AgentCore runtime as search, but sends
 * `request_type: 'analysis'` along with the current playback `timestamp`, the
 * `video_name`, and a `title_id`. The runtime dispatches those to the movie
 * scene-analysis agent (backend/movie-ai-agent-v2/analysis_agent.py), which reads
 * chapter/dialog/celebrity data from OpenSearch to answer questions like
 * "what's happening in this scene?" or "who are the actors here?".
 *
 * Deliberately does NOT send a scene screenshot (`scene_image_s3_key`): the
 * agent falls back to the pre-indexed scene/celebrity data, so no client-side
 * frame capture is required.
 *
 * When the runtime isn't configured (local dev without AWS wiring), a small
 * canned response keeps the player demoable.
 */

import { env } from './env';
import { getAgentSessionId } from './agentSession';
import { getAccessToken } from './cognito';

/**
 * Scene analysis needs the deployer's own analysis data (chapter/segment
 * indexes and the scripts knowledge base); the demo ships none, so there is no
 * default analysed video and the agent answers that scene details aren't
 * available.
 */
export const DEFAULT_SCENE_VIDEO = '';
export const DEFAULT_SCENE_TITLE_ID = '';

/**
 * Titles whose scenes are indexed, keyed by catalog title_id.
 * The scene index matches on an exact `video_name`, so this must be the exact
 * filename the analysis pipeline ingested — NOT the streamed file's name.
 *
 * To add scene analysis for a title: analyze the video into the index,
 * then add a `'<title_id>': '<indexed_video_name>'` entry here.
 */
export const SCENE_VIDEO_BY_TITLE: Record<string, string> = {
  // Tears of Steel, once analysed with the v1 notebooks (README, Scene analysis).
  tt2285752: 'tears-of-steel.mp4',
};

/**
 * Resolve the indexed scene `video_name` for a title ('' when none is indexed).
 */
export function sceneVideoForTitle(titleId?: string | null): string {
  return (titleId && SCENE_VIDEO_BY_TITLE[titleId]) || DEFAULT_SCENE_VIDEO;
}

/** True when the agent runtime is configured (else callers use the mock). */
export function sceneAgentConfigured(): boolean {
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

export interface SceneAskEvents {
  /** Progress updates while the agent works ("Analyzing movie scene…"). */
  onStatus?: (message: string) => void;
  /** The assistant's answer text. */
  onResponse?: (text: string) => void;
  /** Tappable follow-up suggestions, when the agent returns them. */
  onFollowups?: (options: string[]) => void;
  onError?: (message: string) => void;
}

export interface SceneAskOptions {
  /** Current playback position, in milliseconds. */
  timestampMs: number;
  /** Override the analysis video (defaults to the indexed demo movie). */
  videoName?: string;
  /** Override the catalog title id used for cast lookups. */
  titleId?: string;
}

export class SceneAgentClient {
  private getProfile: () => string;
  // Shared across all clients (see agentSession.ts): one conversation per
  // profile across text, voice, scene and match. Read per request.
  private get sessionId(): string {
    return getAgentSessionId();
  }
  private abort: AbortController | null = null;

  constructor(getProfile: () => string) {
    this.getProfile = getProfile;
  }

  /** Abort any in-flight request. */
  cancel() {
    this.abort?.abort();
    this.abort = null;
  }

  /** Ask a question about the current scene. Resolves when the stream ends. */
  async ask(question: string, opts: SceneAskOptions, ev: SceneAskEvents): Promise<void> {
    const text = question.trim();
    if (!text) return;

    // ---- Mock fallback (no runtime configured) ----
    if (!sceneAgentConfigured()) {
      ev.onStatus?.('Analyzing movie scene…');
      await new Promise((r) => setTimeout(r, 700));
      ev.onResponse?.(
        "The agent runtime isn't configured in this environment, so here's a stand-in answer. " +
          'Once VITE_AGENTCORE_AGENT_ID is set, this panel answers from the real scene index.',
      );
      ev.onFollowups?.(['Who are the actors in this scene?', 'What happened before this?']);
      return;
    }

    ev.onStatus?.('Analyzing movie scene…');

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
      session_id: this.sessionId,
      profile: this.getProfile(),
      user_query: text,
      request_type: 'analysis',
      timestamp: Math.max(0, Math.floor(opts.timestampMs)),
      video_name: opts.videoName ?? DEFAULT_SCENE_VIDEO,
      title_id: opts.titleId ?? DEFAULT_SCENE_TITLE_ID,
    });

    try {
      const res = await fetch(invocationsUrl(), {
        method: 'POST',
        headers,
        body,
        signal: this.abort.signal,
      });
      if (!res.ok || !res.body) {
        throw new Error(`Scene agent request failed: ${res.status}`);
      }
      await this.readStream(res.body.getReader(), ev);
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      ev.onError?.('Sorry, something went wrong. Please try again.');
    } finally {
      this.abort = null;
    }
  }

  /** Parse the SSE stream ("data: {...}") and dispatch events. */
  private async readStream(
    reader: ReadableStreamDefaultReader<Uint8Array>,
    ev: SceneAskEvents,
  ) {
    const decoder = new TextDecoder();
    let buffer = '';
    let answered = false;

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
        ev.onStatus?.(String(data.message ?? 'Working…'));
      } else if (type === 'response') {
        answered = true;
        ev.onResponse?.(String(data.response ?? 'No response received.'));
        const followups = Array.isArray(data.tappable_options)
          ? (data.tappable_options as string[])
          : [];
        if (followups.length) ev.onFollowups?.(followups);
      } else if (type === 'error') {
        ev.onError?.(String(data.message ?? 'Something went wrong.'));
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

    if (!answered) ev.onError?.('No response received.');
  }
}
