import { useEffect, useRef, useState } from 'react';
import type { Layout, RunEvent, WhatIfResult, WiStep } from './api';

export type Node = 'profile' | 'understand' | 'retrieve' | 'rerank' | 'feed';
export const NODES: { key: Node; label: string; who: string }[] = [
  { key: 'profile', label: 'Profile update', who: 'stream processor' },
  { key: 'understand', label: 'Understand', who: 'LLM · Haiku 4.5' },
  { key: 'retrieve', label: 'Retrieve', who: 'vector search' },
  { key: 'rerank', label: 'Rerank', who: 'LLM · Haiku 4.5' },
  { key: 'feed', label: 'Clips feed', who: 'ranking policy · in parallel' },
];

export interface LlmCall {
  stage: string; model: string; temperature: number | null; max_tokens: number; system_chars: number; system_head: string;
  input: string; text: string; done: boolean; seconds?: number; usage?: Record<string, number>;
}

export interface Live {
  active: Node | null;
  done: boolean;
  error?: string;
  times: Partial<Record<Node, { start: number; end?: number }>>;
  steps: WiStep[];
  stepsTotal: number;
  llm: Record<string, LlmCall>;
  understood?: Record<string, unknown>;
  retrieval: string[];
  retrievalPath?: string;
  rerankInput: string[];
  rerankExcluded: string[];
  preferences: string;
  streamedPicks: string[];
  picks?: string[];
  response?: string;
  clips?: { confidence: number; pages: { id: string; title: string; reason: string; why: string }[][] };
  memory: string[];
  result?: WhatIfResult;
  now: number;
}

const ID_RE = /\b(?:tt|md)\d{5,}\b/g;

/** Fold the what-if side of the event stream into the live picture. */
export function derive(events: RunEvent[]): Live {
  const L: Live = { active: null, done: false, times: {}, steps: [], stepsTotal: 0, llm: {}, retrieval: [],
    rerankInput: [], rerankExcluded: [], preferences: '', streamedPicks: [], memory: [], now: 0 };
  const start = (n: Node, t: number) => { if (!L.times[n]) L.times[n] = { start: t }; L.active = n; };
  const end = (n: Node, t: number) => { const x = L.times[n] ?? { start: t }; if (x.end === undefined) x.end = t; L.times[n] = x; };
  for (const e of events) {
    L.now = Math.max(L.now, e.t);
    if (e.side === 'baseline') continue;
    const d = e.data;
    switch (e.type) {
      case 'run_start':
        if ((d.body?.watched?.length ?? 0) + (d.body?.reactions?.length ?? 0) > 0) start('profile', e.t);
        break;
      case 'profile_step': start('profile', e.t); L.steps.push(d as WiStep); L.stepsTotal = d.total; break;
      case 'memory': L.memory = d.records ?? []; break;
      case 'agent_start': if (L.times.profile) end('profile', e.t); start('understand', e.t); L.preferences = d.taste_text ?? ''; break;
      case 'llm_start':
        L.llm[d.stage] = { stage: d.stage, model: d.model, temperature: d.temperature, max_tokens: d.max_tokens,
          system_chars: d.system_chars, system_head: d.system_head, input: d.input, text: '', done: false };
        if (d.stage === 'rerank') start('rerank', e.t);
        break;
      case 'llm_delta': if (L.llm[d.stage]) L.llm[d.stage].text += d.text; break;
      case 'llm_end':
        if (L.llm[d.stage]) Object.assign(L.llm[d.stage], { text: d.text, done: true, seconds: d.seconds, usage: d.usage });
        break;
      case 'understood': L.understood = d; end('understand', e.t); start('retrieve', e.t); break;
      case 'retrieval': L.retrieval = [...L.retrieval, ...d.ids]; L.retrievalPath = d.path; break;
      case 'rerank_input':
        end('retrieve', e.t); start('rerank', e.t);
        L.rerankInput = d.ids; L.rerankExcluded = d.excluded ?? []; L.preferences = d.preferences || L.preferences;
        break;
      case 'reranked': L.picks = d.title_ids; L.response = d.response; end('rerank', e.t); break;
      case 'agent_done': start('feed', e.t); break;
      case 'clips_done':
        // The feed runs in parallel with the agent; show its own duration.
        L.clips = d as Live['clips'];
        L.times.feed = { start: Math.max(0, e.t - (Number(d.seconds) || 0)), end: e.t };
        break;
      case 'done': L.result = d.result as WhatIfResult; L.done = true; L.active = null; break;
      case 'failed': L.error = d.error; L.done = true; L.active = null; break;
    }
  }
  const rr = L.llm.rerank?.text ?? '';
  const at = rr.indexOf('title_ids');
  if (at >= 0) {
    const allowed = new Set(L.rerankInput);
    L.streamedPicks = [...new Set(rr.slice(at).match(ID_RE) ?? [])].filter((id) => !allowed.size || allowed.has(id));
  }
  return L;
}

function useTicker(active: boolean) {
  const [, set] = useState(0);
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => set((x) => x + 1), 100);
    return () => clearInterval(id);
  }, [active]);
}

function Ribbon({ text, live }: { text: string; live: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => { if (ref.current) ref.current.scrollLeft = ref.current.scrollWidth; }, [text]);
  return (
    <div className={`ribbon${live ? ' live' : ''}`} ref={ref} aria-live="polite">
      <span>{text.replace(/\s+/g, ' ').slice(-600) || '...'}</span>{live && <i className="caret" />}
    </div>
  );
}

/** The live pipeline drawn over the bottom of the map. */
export function AgentFlow({ live, layout, selected, onSelect, startedAt }:
  { live: Live; layout: Layout; selected: Node | null; onSelect: (n: Node) => void; startedAt: number }) {
  useTicker(!live.done);
  const name = (id: string) => { const i = layout.index.get(id); return i === undefined ? id : layout.titles[i].name; };
  const activeIdx = live.active ? NODES.findIndex((n) => n.key === live.active) : live.done ? NODES.length - 1 : -1;
  const elapsed = live.done ? live.now : Math.max(live.now, (Date.now() - startedAt) / 1000);
  const focus: Node | null = selected ?? live.active ?? (live.done ? 'rerank' : null);
  const u = live.understood ?? {};
  const tags = [
    u.intent && `intent: ${u.intent}`, u.path && `path: ${u.path}`, u.category && `category: ${u.category}`,
    ...((u.genres_include as string[]) ?? []).map((g) => `+ ${g}`), ...((u.genres_exclude as string[]) ?? []).map((g) => `− ${g}`),
    u.mood && `mood: ${u.mood}`, u.search_text && `search: “${u.search_text}”`,
    ...((u.active_rules as string[]) ?? []).map((r) => `rule: ${r}`),
  ].filter(Boolean) as string[];
  const podium = live.picks ?? live.streamedPicks;

  return (
    <div className="flow" role="region" aria-label="Live agent pipeline">
      <div className="flow-scene">
        {focus === 'profile' && (
          <div className="scene-profile">
            {live.steps.map((s, i) => (
              <span key={i} className="fly-in" style={{ animationDelay: `${i * 60}ms` }}>
                {name(s.item)} <b>{s.e === null ? '·' : `${s.e > 0 ? '+' : ''}${s.e}`}</b>
                <small>{s.action === 'new_vector' ? 'new taste vector' : s.action === 'joined' ? `nudged vector ${(s.vector ?? 0) + 1}` : s.action}</small>
              </span>
            ))}
            {!live.steps.length && <span className="muted">Folding the scenario into the sandbox profile...</span>}
          </div>
        )}
        {focus === 'understand' && (
          <div className="scene-understand">
            <Ribbon text={live.llm.understand?.text ?? ''} live={!!live.llm.understand && !live.llm.understand.done} />
            <div className="tags">{tags.map((t, i) => <span key={t} className="burst" style={{ animationDelay: `${i * 90}ms` }}>{t}</span>)}</div>
          </div>
        )}
        {focus === 'retrieve' && (
          <div className="scene-retrieve">
            <div className="cloud" aria-hidden="true">
              {live.retrieval.slice(0, 60).map((id, i) => (
                <i key={id} className={live.rerankExcluded.includes(id) ? 'out' : ''}
                  style={{ ['--a' as string]: `${(i * 137.5) % 360}deg`, ['--r' as string]: `${40 + (i % 5) * 14}px`, animationDelay: `${i * 18}ms` }} />
              ))}
            </div>
            <p><b>{live.retrieval.length}</b> candidates {live.retrievalPath ? <>via <code>{live.retrievalPath}</code></> : ''} · lit on the map</p>
          </div>
        )}
        {focus === 'rerank' && (
          <div className="scene-rerank">
            <div className="podium">
              {[0, 1, 2, 3, 4].map((i) => (
                <span key={i} className={podium[i] ? 'filled' : ''}><b>#{i + 1}</b>{podium[i] ? name(podium[i]) : ''}</span>
              ))}
            </div>
            <div className="field">
              {live.rerankInput.map((id) => {
                const r = podium.indexOf(id);
                return <span key={id} className={r >= 0 ? 'won' : live.picks ? 'lost' : ''}>{name(id)}</span>;
              })}
            </div>
            <Ribbon text={live.llm.rerank?.text ?? ''} live={!!live.llm.rerank && !live.llm.rerank.done} />
          </div>
        )}
        {focus === 'feed' && (
          <div className="scene-feed">
            {(live.clips?.pages ?? []).flat().map((c, i) => (
              <span key={c.id} className={`card-dot ${c.reason}`} style={{ animationDelay: `${i * 70}ms` }} title={c.why}>{i + 1}. {c.title}</span>
            ))}
            {!live.clips && <span className="muted">Rebuilding interests and ranking the first two pages...</span>}
          </div>
        )}
      </div>

      <div className="rail">
        <div className="track"><i style={{ width: `${activeIdx < 0 ? 0 : (activeIdx / (NODES.length - 1)) * 100}%` }} /></div>
        {activeIdx >= 0 && !live.done && <div className="pulse" style={{ left: `${(activeIdx / (NODES.length - 1)) * 100}%` }} />}
        {NODES.map((n, i) => {
          const t = live.times[n.key];
          const state = t?.end !== undefined ? 'done' : live.active === n.key ? 'on' : t ? 'on' : '';
          const secs = t ? ((t.end ?? elapsed) - t.start) : null;
          return (
            <button key={n.key} className={`node ${state}${focus === n.key ? ' focus' : ''}`} style={{ left: `${(i / (NODES.length - 1)) * 100}%` }}
              onClick={() => onSelect(n.key)} aria-pressed={focus === n.key} disabled={!t}>
              <span className="orb" />
              <span className="lbl">{n.label}<small>{n.who}</small>{secs !== null && <em>{secs.toFixed(1)} s</em>}</span>
            </button>
          );
        })}
      </div>
      <div className="flow-clock">{live.error ? <span className="error">{live.error}</span> : live.done ? `done in ${elapsed.toFixed(1)} s` : `${elapsed.toFixed(1)} s`}</div>
    </div>
  );
}

/** Right-panel detail for the focused node: what the stage was given and what it produced. */
export function NodeDetail({ node, live, layout }: { node: Node; live: Live; layout: Layout }) {
  const name = (id: string) => { const i = layout.index.get(id); return i === undefined ? id : layout.titles[i].name; };
  const call = node === 'understand' ? live.llm.understand : node === 'rerank' ? live.llm.rerank : undefined;
  return (
    <div className="node-detail">
      <h3>{NODES.find((n) => n.key === node)?.label} <small>{NODES.find((n) => n.key === node)?.who}</small></h3>
      {call && (
        <>
          <p className="meta">{call.model} · {call.temperature == null ? "thinking off" : `temperature ${call.temperature}`} · max {call.max_tokens} tokens · system prompt {call.system_chars.toLocaleString()} chars
            {call.usage ? ` · ${call.usage.inputTokens + (call.usage.cacheReadInputTokens ?? 0)} in / ${call.usage.outputTokens} out tokens` : ''}
            {call.usage?.cacheReadInputTokens ? ` (${call.usage.cacheReadInputTokens} from cache)` : ''}</p>
          <details><summary>System prompt (start)</summary><pre className="prompt small">{call.system_head}</pre></details>
          <h4>What it was given</h4>
          <pre className="prompt small scroll">{call.input}</pre>
          <h4>What it wrote {call.done ? `(${call.seconds} s)` : '(streaming)'}</h4>
          <pre className="prompt small scroll">{call.text || '...'}{!call.done && <i className="caret" />}</pre>
        </>
      )}
      {node === 'profile' && (
        live.steps.length ? <ul className="bullets">{live.steps.map((s, i) => (
          <li key={i}>{name(s.item)}: e = {s.e ?? 'not scored'}; {s.action}; {s.entities.filter((x) => x.before !== x.after).slice(0, 3)
            .map((x) => `${x.label} ${x.before.toFixed(2)} → ${x.after.toFixed(2)}`).join(', ')}</li>
        ))}</ul> : <p className="muted">No activity in this scenario.</p>
      )}
      {node === 'retrieve' && (
        <>
          <p className="hint">Taste-steered vector search over the catalog. These titles go to the reranker; watched and already-shown ones are excluded first.</p>
          <ol className="small-list">{live.retrieval.map((id) => <li key={id} className={live.rerankExcluded.includes(id) ? 'dropped' : ''}>{name(id)}</li>)}</ol>
        </>
      )}
      {node === 'feed' && live.clips && (
        <ol className="small-list">{live.clips.pages.flat().map((c) => <li key={c.id}>{c.title} · {c.reason === 'for-you' ? c.why : 'explore'}</li>)}</ol>
      )}
      {(node === 'understand' || node === 'rerank') && live.memory.length > 0 && (
        <details><summary>Long-term memory read ({live.memory.length})</summary><ul className="bullets">{live.memory.map((m) => <li key={m}>{m}</li>)}</ul></details>
      )}
    </div>
  );
}
