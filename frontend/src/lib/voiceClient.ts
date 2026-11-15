/**
 * Transport-agnostic voice interface.
 *
 * The Search UI talks only to this interface, so the backend (Nova Sonic on
 * Fargate vs. on AgentCore bidirectional) can be swapped in later without
 * touching the UI. Until the backend path is chosen, `MockVoiceClient` drives
 * the full listen → transcribe → think → results flow so the UX is demoable.
 */

export type VoiceState = 'idle' | 'listening' | 'thinking' | 'results' | 'error';

/** A single voice result — a superset that maps cleanly to a DetailItem. */
export interface VoiceResult {
  id: string;
  title: string;
  /** GenAI "why this matches" line. */
  why: string;
  posterUrl?: string | null;
  hueA?: number;
  hueB?: number;
  genres: string[];
  year?: number | null;
  rating?: number | null;
  maturity?: string | null;
  plot?: string | null;
  cast?: string[];
  kind?: string;
  /** Presigned S3 URL for playback, when the title has a video mapping. */
  contentUrl?: string | null;
  /** Sports live cards, when applicable. */
  live?: boolean;
  liveLabel?: string;
}

export interface VoiceEvents {
  onState?: (s: VoiceState) => void;
  /** Filler / progress updates while the agent works ("Searching for movies…"). */
  onStatus?: (message: string) => void;
  /** Live transcript as the user speaks. */
  onPartialTranscript?: (text: string) => void;
  /** `turn` is a monotonic token the client assigns to each user submission.
   *  The UI uses it to bind later events (reply/results) back to the turn that
   *  actually asked, so a slow/interrupted request can't paint onto a newer
   *  turn. Optional for backward compatibility (clients that omit it fall back
   *  to the latest turn). */
  onFinalTranscript?: (text: string, turn?: number) => void;
  /** Fired once the pipeline decides the request type — before results arrive.
   *  'recommendation' means result cards are coming (show skeletons); 'chat'
   *  means it's a conversational reply with no results. */
  onIntent?: (kind: 'recommendation' | 'chat', turn?: number) => void;
  /** The assistant's spoken reply text (what Sonic would voice). */
  onResponse?: (spokenText: string, turn?: number) => void;
  onResults?: (results: VoiceResult[], turn?: number) => void;
  /** Tappable refinement suggestions ("Shorter runtime", "Less scary", …). */
  onFollowups?: (options: string[], turn?: number) => void;
  onError?: (message: string) => void;
}

export interface VoiceClient {
  /** Begin listening (mic). */
  start(): void;
  /** Stop listening and submit whatever was captured. */
  stop(): void;
  /** Abort the current interaction and reset to idle. */
  cancel(): void;
  /** Submit a typed query instead of speaking. */
  submitText(text: string): void;
  /** Optional mic mute. Stops forwarding captured audio without tearing down
   *  the session — capture keeps running so un-muting resumes instantly. Only
   *  the continuous open-mic transport (Nova Sonic) implements this; text/STT
   *  clients omit it. */
  setMuted?(muted: boolean): void;
  /** Optional voice-OUTPUT mute. Silences the assistant's spoken audio (and
   *  cuts anything currently playing) without affecting the mic, transcript, or
   *  on-screen results. Only the audio transport (Nova Sonic) implements this. */
  setOutputMuted?(muted: boolean): void;
}
