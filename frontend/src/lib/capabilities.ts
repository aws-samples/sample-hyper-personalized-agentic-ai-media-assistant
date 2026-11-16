/**
 * Feature capability flags.
 *
 * The IBC 2026 demo has more UI than it currently has backend. Every feature
 * is either wired to a real backend ("live") or rendered from local mock data
 * ("mock"). These flags are the single source of truth for that distinction so
 * that:
 *   - screens can decide whether to call the API or read fixtures,
 *   - the UI can badge/label features that aren't live yet,
 *   - flipping a feature to "live" is a one-line change once its backend ships.
 *
 * Update a flag to `true` only once the corresponding backend is deployed and
 * the client code in src/lib/api is wired up.
 */

export interface Capabilities {
  /** Cognito auth (user pool + identity pool). */
  auth: boolean;
  /** REST: top picks, carousels, because-you-watched. */
  recommendations: boolean;
  /** REST: get/save/remove watchlist. */
  watchlist: boolean;
  /** REST: get_video_url for VOD playback. */
  vodPlayback: boolean;
  /** AgentCore streaming: conversational search + "why you'll love this". */
  agentSearch: boolean;
  /** Live sports feed, stats, key moments (workstream 5.2). */
  liveSports: boolean;
  /** TikTok-style short-form clips pipeline (workstream 5.3). */
  shortForm: boolean;
  /** Amazon Quick audience insights dashboard (workstream 5.5). */
  operatorInsights: boolean;
  /** Nova Sonic 2.0 speech-to-speech voice (workstream 5.7). */
  voiceNovaSonic: boolean;
}

/**
 * Current state of the world.
 *
 * Live = backend confirmed to exist (REST API + AgentCore runtime + Cognito).
 * Mock = design exists in the mockups; backend not built yet for IBC.
 */
export const capabilities: Capabilities = {
  // --- Live (backend exists today) ---
  auth: true,
  recommendations: true,
  watchlist: true,
  vodPlayback: true,
  agentSearch: true,

  // --- Mock only (backend not built yet for IBC) ---
  liveSports: false,
  shortForm: false,
  operatorInsights: false,
  voiceNovaSonic: false,
};

export function isLive(feature: keyof Capabilities): boolean {
  return capabilities[feature];
}
