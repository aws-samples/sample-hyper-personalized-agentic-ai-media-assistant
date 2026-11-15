/**
 * Central, typed access to environment configuration.
 *
 * Reading env vars in one place means a missing value fails loudly and early
 * (during dev / at startup) instead of surfacing as a confusing runtime error
 * deep inside an API call during the demo.
 */

const raw = import.meta.env;

export const env = {
  awsRegion: raw.VITE_AWS_REGION,
  awsAccountId: raw.VITE_AWS_ACCOUNT_ID,
  cognito: {
    userPoolId: raw.VITE_COGNITO_USER_POOL_ID,
    userPoolClientId: raw.VITE_COGNITO_CLIENT_ID,
    identityPoolId: raw.VITE_COGNITO_IDENTITY_POOL_ID,
  },
  apiBaseUrl: raw.VITE_API_BASE_URL,
  agentCoreAgentId: raw.VITE_AGENTCORE_AGENT_ID,
  agentCoreAgentArn: raw.VITE_AGENTCORE_AGENT_ARN,
  // Sports "match assistant" AgentCore runtime (<prefix>_sports). Separate
  // runtime + wire contract from the movie agents. Optional — when unset, the
  // Match Assistant panel streams a canned answer so the UI is demoable.
  agentCoreMatchArn: raw.VITE_AGENTCORE_MATCH_ARN,
  // Nova Sonic voice runtime. `voiceWsUrl` is a local-dev override
  // (ws://localhost:8080/ws, no auth); `agentCoreVoiceArn` is the deployed
  // WebSocket runtime used on CloudFront (wss:// + bearer subprotocol).
  voiceWsUrl: raw.VITE_VOICE_WS_URL,
  agentCoreVoiceArn: raw.VITE_AGENTCORE_VOICE_ARN,
  // Optional fixed AgentCore session id shared by all clients (environment
  // file `fixedSessionId`). When set, every agent call reuses this session, so
  // scripts/prewarm_session.py can keep its microVM warm and remove the cold
  // start on the first query at an event. Leave unset for normal use, where
  // each client mints a fresh random session id per load. Trade-off: everyone
  // shares that session's conversation and already-shown titles, so change the
  // id between demo runs. Independent of booth mode (lib/booth.ts).
  sessionId: raw.VITE_SESSION_ID,
  // Playable URL (HLS .m3u8 / presigned S3 / CloudFront) for the "live" sports
  // game. `liveSportsUrl` is the 16:9 (TV/desktop) rendition; the vertical one
  // is the 9:16 (mobile) rendition. Optional — when unset, the sports player
  // falls back to a gradient placeholder.
  liveSportsUrl: raw.VITE_LIVE_SPORTS_URL,
  liveSportsUrlVertical: raw.VITE_LIVE_SPORTS_URL_VERTICAL,
  // Optional base URL for the vertical-feed clips (the clips API normally
  // returns presigned URLs). Clips resolve to `${mediaBaseUrl}/<filename>.mp4`.
  // Optional — when unset, clips fall back to a same-origin `/clips/` path.
  mediaBaseUrl: raw.VITE_MEDIA_BASE_URL,
  isolatedDeployment: raw.VITE_ISOLATED_DEPLOYMENT === 'true',
} as const;

/**
 * Returns the list of required env vars that are missing/empty.
 * Used at startup to warn the developer rather than failing silently.
 */
export function missingEnvVars(): string[] {
  const checks: Record<string, string | undefined> = {
    VITE_AWS_REGION: env.awsRegion,
    VITE_AWS_ACCOUNT_ID: env.awsAccountId,
    VITE_COGNITO_USER_POOL_ID: env.cognito.userPoolId,
    VITE_COGNITO_CLIENT_ID: env.cognito.userPoolClientId,
    VITE_API_BASE_URL: env.apiBaseUrl,
    VITE_AGENTCORE_AGENT_ARN: env.agentCoreAgentArn || env.agentCoreAgentId,
  };
  return Object.entries(checks)
    .filter(([, value]) => !value)
    .map(([key]) => key);
}
