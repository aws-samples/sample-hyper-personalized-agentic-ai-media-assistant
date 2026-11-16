import { idToken, loadConfig } from './auth';

export type Vec3 = [number, number, number];

export interface Cluster {
  id: number;
  label: string;
  size: number;
  genres: [string, number][];
  examples: string[];
  center: Vec3;
}

export interface Title {
  id: string;
  name: string;
  type: string;
  year: number | null;
  rating: number | null;
  genres: string;
  pos: Vec3;
  cluster: number;
}

export interface Layout {
  builtAt: number;
  count: number;
  clusters: Cluster[];
  titles: Title[];
  index: Map<string, number>;
}

export interface Neighbor {
  id: string;
  name: string;
  cosine: number;
}

export interface Persona {
  profile_id: string;
  name: string;
  household: string;
  has_profile: boolean;
  version: number;
  updated_at: string | null;
}

export interface TasteVector {
  index: number;
  weight: number;
  count: number;
  position: Vec3 | null;
  neighbors: Neighbor[];
}

export interface Entity {
  key: string;
  kind: string;
  label: string;
  s: number;
  s_now: number;
  n: number;
  used: boolean;
}

export interface Watched {
  id: string;
  name: string;
  position: Vec3 | null;
  content_type: string | null;
  completed: boolean;
  completion: number;
  last_watched: string | null;
  poster: string | null;
}

export interface Taste {
  profile_id: string;
  version: number;
  updated_at: string | null;
  taste_vectors: TasteVector[];
  taste_source: 'stored' | 'preview';
  watched: Watched[];
  entities: Entity[];
  contexts: Record<string, { name: string; views: number; short_min: number; long_min: number; top: string[] }>;
  current_bucket: string | null;
  agent_taste_text: string[];
  declared: Record<string, unknown>;
}

export interface TitleDetail {
  id: string;
  name: string;
  type: string;
  plot: string | null;
  genres: string | string[] | null;
  rating: number | null;
  year: number | null;
  cast: string[];
  directors: string[];
  awards: number;
  poster: string | null;
  neighbors: Neighbor[];
}

async function get<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  const cfg = await loadConfig();
  const token = await idToken();
  const qs = new URLSearchParams(params).toString();
  const res = await fetch(`${cfg.apiUrl.replace(/\/$/, '')}/${path}${qs ? `?${qs}` : ''}`, {
    headers: token ? { Authorization: token } : {},
  });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return (await res.json()) as T;
}

/** Map download straight from S3; a failure names the step instead of a bare "Failed to fetch". */
async function download(url: string): Promise<unknown> {
  const res = await fetch(url).catch(() => {
    throw new Error('the catalog map download was blocked (network or CORS)');
  });
  if (!res.ok) throw new Error(`catalog map download failed: ${res.status}`);
  return res.json();
}

export async function fetchLayout(): Promise<Layout> {
  const { url } = await get<{ url: string }>('layout');
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const raw = (await download(url)) as any;
  const titles: Title[] = raw.titles.map((r: unknown[]) => ({
    id: r[0] as string,
    name: r[1] as string,
    type: r[2] as string,
    year: r[3] as number | null,
    rating: r[4] as number | null,
    genres: r[5] as string,
    pos: [r[6], r[7], r[8]] as Vec3,
    cluster: r[9] as number,
  }));
  return {
    builtAt: raw.built_at,
    count: raw.count,
    clusters: raw.clusters,
    titles,
    index: new Map(titles.map((t, i) => [t.id, i])),
  };
}

export const fetchPersonas = () => get<{ personas: Persona[] }>('personas').then((r) => r.personas);
export const fetchTaste = (profileId: string) => get<Taste>('taste', { profile_id: profileId });
export const fetchTitle = (id: string) => get<TitleDetail>('title', { id });

// --- what-if ---------------------------------------------------------------

export interface WhatIfInput {
  profile_id: string;
  watched: string[];
  reactions: { id: string; value: 'like' | 'more' | 'dislike' }[];
  excluded_genres?: string[];
  bucket?: string | null;
  query?: string;
}

export interface WiTaste { index: number; weight: number; count: number; position: Vec3 | null; neighbors: { id: string; cosine: number }[] }

export interface WiStep {
  index: number;
  kind: 'view' | 'feedback';
  item: string;
  value: string | null;
  e: number | null;
  action: 'joined' | 'new_vector' | 'seeded' | 'none';
  vector: number | null;
  entities: { key: string; label: string; before: number; after: number; n: number }[];
  taste_vectors: WiTaste[];
  version: number;
}

export interface WiAgent {
  error?: string;
  statuses: { phase: string; message: string; t: number }[];
  understand?: Record<string, unknown>;
  retrieval?: { path: string; ids: string[] }[];
  reduce?: { candidates: number | null; exclude_ids: string[]; preferences: string; title_ids: string[]; response: string };
  final?: { intent: string; response: string };
  taste_text: string;
  picks: string[];
  seconds: number;
}

export interface WiClips {
  error?: string;
  interests: { kind: string; weight: number; strength: number; prior: boolean; sources: string[]; candidates: string[]; position: Vec3 | null }[];
  negatives: { weight: number; sources: string[] }[];
  microdramas: string[];
  discovery: string[];
  excluded: string[];
  confidence: number;
  personalized_slots: number;
  pages: { id: string; title: string; kind: string; reason: string; why: string; relevance: number; microdrama_boost: boolean }[][];
  seconds: number;
}

export interface WiSide {
  taste_vectors: WiTaste[];
  version: number;
  declared: Record<string, unknown>;
  watched: string[];
  agent: WiAgent;
  clips: WiClips;
}

export interface WhatIfResult {
  profile_id: string;
  query: string;
  bucket: string | null;
  ltm: string[];
  steps: WiStep[];
  baseline: WiSide;
  whatif: WiSide;
}

export async function runWhatIf(input: WhatIfInput): Promise<WhatIfResult> {
  const cfg = await loadConfig();
  const token = await idToken();
  const call = () => fetch(`${cfg.apiUrl.replace(/\/$/, '')}/whatif`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: token } : {}) },
    body: JSON.stringify(input),
  });
  let res = await call();
  // 504: the scenario outlived API Gateway's 29 s (a cold start); the container is warm now.
  if (res.status === 504) res = await call();
  if (!res.ok) {
    const msg = await res.json().then((b) => b.error ?? b.message).catch(() => '');
    throw new Error(msg || `what-if failed: ${res.status}`);
  }
  return (await res.json()) as WhatIfResult;
}

// --- streamed what-if runs ---------------------------------------------------

export interface RunEvent {
  seq: number;
  type: string;
  side: 'baseline' | 'whatif' | null;
  t: number;
  data: Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
}

async function authed(path: string, init: RequestInit = {}) {
  const cfg = await loadConfig();
  const token = await idToken();
  return fetch(`${cfg.apiUrl.replace(/\/$/, '')}/${path}`, {
    ...init, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: token } : {}), ...(init.headers ?? {}) },
  });
}

export async function startWhatIf(input: WhatIfInput): Promise<string> {
  const res = await authed('whatif', { method: 'POST', body: JSON.stringify({ ...input, stream: true }) });
  if (!res.ok) throw new Error(`could not start the scenario: ${res.status}`);
  return (await res.json()).run_id as string;
}

export async function fetchRunEvents(runId: string, after: number): Promise<RunEvent[]> {
  const res = await authed(`whatif/events?${new URLSearchParams({ run_id: runId, after: String(after) })}`);
  if (!res.ok) throw new Error(`events: ${res.status}`);
  return (await res.json()).events as RunEvent[];
}
