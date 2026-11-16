import { env } from './env';

/**
 * One agent session id shared by every client (typed search, voice, scene,
 * match), so all surfaces land on the same AgentCore runtime session and the
 * agent keeps one conversation: what was said and shown by voice is known to
 * text, and the reverse.
 *
 * An optional fixed id (`fixedSessionId` -> VITE_SESSION_ID) pins every client
 * to one session so scripts/prewarm_session.py can keep it warm; otherwise a
 * fresh random id per page load, replaced when the profile changes or the user
 * signs out. A reload starts a new conversation.
 */
let current: string = env.sessionId || crypto.randomUUID();

export function getAgentSessionId(): string {
  return current;
}

/** Start a new conversation. No-op with a fixed session id. */
export function resetAgentSession(): void {
  if (env.sessionId) return;
  current = crypto.randomUUID();
}
