import { useEffect, useMemo, useState } from 'react';
import {
  fetchRunEvents, fetchTaste, startWhatIf, type Layout, type Persona, type RunEvent, type Vec3, type WhatIfResult, type WiSide, type WiStep, type WiTaste,
} from './api';
import { AgentFlow, NodeDetail, derive, type Node } from './AgentFlow';
import { MapScene, type Marker } from './MapScene';

type Action = 'watched' | 'like' | 'more' | 'dislike';
interface Item { id: string; action: Action }

const ACTION_LABEL: Record<Action, string> = { watched: 'Watched it', like: 'Like', more: 'More like this', dislike: 'Less like this' };
const BUCKETS = [
  { key: '', label: 'Now' }, { key: 'weekday|daytime', label: 'Weekday day' }, { key: 'weekday|evening', label: 'Weekday evening' },
  { key: 'weekend|daytime', label: 'Weekend day' }, { key: 'weekend|evening', label: 'Weekend evening' },
];
const STAGES = [
  { key: 'profile', title: 'Profile update', who: 'Stream processor · L2' },
  { key: 'understand', title: 'Understand the request', who: 'Movie agent · LLM' },
  { key: 'retrieve', title: 'Retrieve candidates', who: 'Movie agent · vector search' },
  { key: 'rerank', title: 'Rerank with taste', who: 'Movie agent · LLM' },
  { key: 'interests', title: 'Build feed interests', who: 'Clips feed · policy' },
  { key: 'picks', title: 'Pick the cards', who: 'Clips feed · policy' },
] as const;
type StageKey = typeof STAGES[number]['key'];

const C = { base: '#8b95aa', what: '#ff8a3d', cand: '#4cc9f0', pick: '#ffd166', drop: '#ff6b6b', session: '#06d6a0', md: '#f72585', excl: '#ff6b6b' };

function nameOf(layout: Layout, id: string) {
  const i = layout.index.get(id);
  return i === undefined ? id : layout.titles[i].name;
}

function tasteMarkers(vectors: WiTaste[], prefix: string, color: string, ghost: boolean, label: string): Marker[] {
  return vectors.filter((v) => v.position).map((v) => ({
    key: `${prefix}-${v.index}`, pos: v.position as Vec3, color, ghost, size: 1.1 + 2.2 * Math.sqrt(v.weight),
    label: `${label} · vector ${v.index + 1}`, sub: `${v.count} signals · weight ${Math.round(v.weight * 100)}%`,
    lines: ghost ? [] : v.neighbors.slice(0, 8).map((n) => n.id),
  }));
}

function Diff({ before, after, label }: { before: string[]; after: string[]; label: (id: string) => string }) {
  const b = new Set(before);
  const a = new Set(after);
  return (
    <div className="diff">
      <div>
        <h4>Before</h4>
        <ol>{before.map((id) => <li key={id} className={a.has(id) ? '' : 'dropped'}>{label(id)}{!a.has(id) && <em>dropped</em>}</li>)}</ol>
      </div>
      <div>
        <h4>What if</h4>
        <ol>{after.map((id, i) => {
          const was = before.indexOf(id);
          const tag = !b.has(id) ? 'new' : was > i ? 'up' : was < i ? 'down' : '';
          return <li key={id} className={tag === 'new' ? 'new' : ''}>{label(id)}{tag && <em>{tag === 'new' ? 'new' : tag === 'up' ? `up ${was - i}` : `down ${i - was}`}</em>}</li>;
        })}</ol>
      </div>
      {after.length === 0 && <p className="muted">No picks in this scenario.</p>}
    </div>
  );
}

function TextDiff({ before, after }: { before: string; after: string }) {
  const b = new Set(before.split('\n'));
  const a = new Set(after.split('\n'));
  return (
    <pre className="prompt">
      {after.split('\n').map((l, i) => <div key={`a${i}`} className={b.has(l) ? '' : 'added'}>{b.has(l) ? '  ' : '+ '}{l}</div>)}
      {before.split('\n').filter((l) => !a.has(l)).map((l, i) => <div key={`r${i}`} className="removed">- {l}</div>)}
    </pre>
  );
}

function StepCard({ step, layout }: { step: WiStep; layout: Layout }) {
  const what = step.kind === 'view' ? 'Finished watching' : ({ like: 'Liked', more: 'More like this on', dislike: 'Less like this on' } as Record<string, string>)[step.value ?? ''];
  const action = {
    joined: `Moved taste vector ${(step.vector ?? 0) + 1} towards it (cosine with it was at least 0.55)`,
    new_vector: `Too far from every taste vector (cosine below 0.55): started vector ${(step.vector ?? 0) + 1}`,
    seeded: 'Created the first taste vector from viewing history',
    none: step.e !== null && step.e < 0 ? 'Negative signal: entity scores drop, taste vectors only move on positive signals' : 'Not scored',
  }[step.action];
  const rows = step.entities.filter((x) => x.before !== x.after).sort((a, b) => Math.abs(b.after - b.before) - Math.abs(a.after - a.before)).slice(0, 7);
  return (
    <div className="step-card">
      <p className="step-title">{what} <b>{nameOf(layout, step.item)}</b></p>
      <p className="meta">Signal strength e = {step.e === null ? 'not scored' : (step.e > 0 ? '+' : '') + step.e} · L2 v{step.version}</p>
      <p className="step-action">{action}</p>
      <ul className="deltas">
        {rows.map((r) => {
          const crossed = r.n >= 3 && Math.abs(r.after) >= 0.3 && !(Math.abs(r.before) >= 0.3 && r.n - 1 >= 3);
          return (
            <li key={r.key}>
              <span className="name">{r.label}</span>
              <span className="track"><i className="was" style={{ width: `${Math.abs(r.before) * 100}%` }} /><i className={`now${r.after < 0 ? ' neg' : ''}`} style={{ width: `${Math.abs(r.after) * 100}%` }} /></span>
              <span className="num">{r.before.toFixed(2)} → {r.after.toFixed(2)}</span>
              {crossed && <em className="crossed">agent now uses it</em>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function WhatIfView({ layout, personas, colorOf }: { layout: Layout; personas: Persona[]; colorOf: (pid: string) => string }) {
  const usable = personas.filter((p) => p.has_profile);
  const [pid, setPid] = useState(usable[0]?.profile_id ?? '');
  const [items, setItems] = useState<Item[]>([]);
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [excluded, setExcluded] = useState<string[] | null>(null);
  const [bucket, setBucket] = useState('');
  const [running, setRunning] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<WhatIfResult | null>(null);
  const [stage, setStage] = useState<StageKey>('profile');
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [focusNode, setFocusNode] = useState<Node | null>(null);
  const [panel, setPanel] = useState<'live' | 'replay'>('live');
  const [startedAt, setStartedAt] = useState(0);
  // The picked viewer's current (stored) taste vectors, shown on the map before
  // any scenario runs so picking a viewer places them in the catalog space.
  const [current, setCurrent] = useState<{ pid: string; vectors: WiTaste[] } | null>(null);
  const color = colorOf(pid);

  useEffect(() => {
    if (!pid) return;
    let live = true;
    fetchTaste(pid)
      .then((t) => live && setCurrent({ pid, vectors: t.taste_vectors }))
      .catch(() => live && setCurrent(null));
    return () => { live = false; };
  }, [pid]);

  const genres = useMemo(() => {
    const n = new Map<string, number>();
    for (const t of layout.titles) for (const g of t.genres.split(', ').filter(Boolean)) n.set(g, (n.get(g) ?? 0) + 1);
    return [...n.entries()].sort((a, b) => b[1] - a[1]).slice(0, 16).map(([g]) => g);
  }, [layout]);
  const results = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (q.length < 2) return [];
    return layout.titles.filter((t) => t.name.toLowerCase().includes(q) && !items.some((i) => i.id === t.id)).slice(0, 8);
  }, [layout, search, items]);

  const run = async () => {
    setRunning(true);
    setError('');
    setPlaying(false);
    setResult(null);
    setEvents([]);
    setFocusNode(null);
    setPanel('live');
    setStartedAt(Date.now());
    try {
      const runId = await startWhatIf({
        profile_id: pid,
        watched: items.filter((i) => i.action === 'watched').map((i) => i.id),
        reactions: items.filter((i) => i.action !== 'watched').map((i) => ({ id: i.id, value: i.action as 'like' | 'more' | 'dislike' })),
        ...(excluded ? { excluded_genres: excluded } : {}),
        bucket: bucket || null,
        query: query.trim() || undefined,
      });
      // Poll the run's events until it reports done or failed (or two minutes pass).
      let after = 0;
      const all: RunEvent[] = [];
      const deadline = Date.now() + 120_000;
      while (Date.now() < deadline) {
        const batch = await fetchRunEvents(runId, after).catch(() => []);
        if (batch.length) {
          all.push(...batch);
          after = batch[batch.length - 1].seq;
          setEvents([...all]);
          const end = batch.find((e) => e.type === 'done' || e.type === 'failed');
          if (end?.type === 'done') { setResult(end.data.result as WhatIfResult); setStage('profile'); setStep(0); break; }
          if (end?.type === 'failed') throw new Error(end.data.error || 'the scenario could not be run');
        }
        await new Promise((r) => setTimeout(r, 400));
      }
      if (Date.now() >= deadline) throw new Error('the scenario took too long');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunning(false);
    }
  };
  const live = useMemo(() => derive(events), [events]);

  // Autoplay: one profile step every 2 s, then one stage every 4 s.
  useEffect(() => {
    if (!playing || !result) return;
    const inProfile = stage === 'profile' && step < result.steps.length - 1;
    const t = setTimeout(() => {
      if (inProfile) return setStep((s) => s + 1);
      const i = STAGES.findIndex((s) => s.key === stage);
      if (i < STAGES.length - 1) setStage(STAGES[i + 1].key);
      else setPlaying(false);
    }, stage === 'profile' ? 2000 : 4000);
    return () => clearTimeout(t);
  }, [playing, result, stage, step]);

  const view = useMemo(() => {
    const highlights = new Map<string, string>();
    const ranks = new Map<string, string>();
    let markers: Marker[] = [];
    if (panel === 'live' && events.length) {
      const focus = focusNode ?? live.active ?? (live.done ? 'rerank' : null);
      const lastTaste = live.steps.length ? live.steps[live.steps.length - 1].taste_vectors : result?.whatif.taste_vectors ?? [];
      markers = [...(result ? tasteMarkers(result.baseline.taste_vectors, 'base', C.base, true, 'Before') : []),
        ...tasteMarkers(lastTaste, 'what', color, false, 'What if')];
      if (focus === 'profile') live.steps.forEach((s, i) => highlights.set(s.item, i === live.steps.length - 1 ? '#ffffff' : color));
      if (focus === 'retrieve' || focus === 'understand') {
        for (const id of live.retrieval) highlights.set(id, live.rerankExcluded.includes(id) ? C.excl : C.cand);
      }
      if (focus === 'rerank') {
        for (const id of live.rerankInput) highlights.set(id, C.cand);
        (live.picks ?? live.streamedPicks).forEach((id, i) => { highlights.set(id, C.pick); ranks.set(id, `#${i + 1}`); });
      }
      if (focus === 'feed') (live.clips?.pages ?? []).flat().forEach((c, i) => { highlights.set(c.id, c.reason === 'for-you' ? C.pick : C.cand); ranks.set(c.id, `${i + 1}`); });
      return { highlights, ranks, markers };
    }
    if (!result) {
      // No scenario yet: show the viewer's current taste. Same 'what' keys as the
      // run's markers, so they glide from here once a scenario plays.
      if (current?.pid === pid) markers = tasteMarkers(current.vectors, 'what', color, false, 'Current taste');
      return { highlights, ranks, markers };
    }
    const B: WiSide = result.baseline;
    const W: WiSide = result.whatif;
    const ghost = tasteMarkers(B.taste_vectors, 'base', C.base, true, 'Before');
    const cur = stage === 'profile' && result.steps.length ? result.steps[Math.min(step, result.steps.length - 1)].taste_vectors : W.taste_vectors;
    markers = [...ghost, ...tasteMarkers(cur, 'what', color, false, 'What if')];
    if (stage === 'profile') {
      result.steps.slice(0, step + 1).forEach((s, i) => highlights.set(s.item, i === step ? '#ffffff' : color));
    } else if (stage === 'retrieve') {
      for (const r of W.agent.retrieval ?? []) for (const id of r.ids) highlights.set(id, C.cand);
      for (const id of W.agent.reduce?.exclude_ids ?? []) if (highlights.has(id)) highlights.set(id, C.excl);
    } else if (stage === 'rerank') {
      for (const id of B.agent.picks) if (!W.agent.picks.includes(id)) highlights.set(id, C.drop);
      W.agent.picks.forEach((id, i) => { highlights.set(id, C.pick); ranks.set(id, `#${i + 1}`); });
    } else if (stage === 'interests') {
      markers = [...markers, ...W.clips.interests.filter((x) => x.position).map((x, i) => ({
        key: `int-${i}`, pos: x.position as Vec3, color: x.kind === 'session' ? C.session : color, size: 1.4,
        label: x.kind === 'session' ? `Session interest ${i + 1}` : 'Long-term taste', sub: `weight ${x.weight.toFixed(2)}`,
        lines: x.candidates.slice(0, 10),
      }))];
      for (const x of W.clips.interests) for (const id of x.candidates) highlights.set(id, x.kind === 'session' ? C.session : C.cand);
      for (const id of W.clips.microdramas) highlights.set(id, C.md);
    } else if (stage === 'picks') {
      W.clips.pages.flat().forEach((c, i) => { highlights.set(c.id, c.reason === 'for-you' ? C.pick : C.cand); ranks.set(c.id, `${i + 1}`); });
    }
    return { highlights, ranks, markers };
  }, [result, stage, step, color, panel, events.length, live, focusNode, current, pid]);

  const B = result?.baseline;
  const W = result?.whatif;
  const label = (id: string) => nameOf(layout, id);
  const stageIdx = STAGES.findIndex((s) => s.key === stage);

  return (
    <main>
      <aside className="left" aria-label="Scenario">
        <section>
          <h3>Viewer</h3>
          <div className="personas">
            {usable.map((p) => (
              <button key={p.profile_id} className={`persona${pid === p.profile_id ? ' on' : ''}`} aria-pressed={pid === p.profile_id}
                style={{ ['--c' as string]: colorOf(p.profile_id) }} onClick={() => { setPid(p.profile_id); setResult(null); }}>
                <span className="dot" />{p.name}
              </button>
            ))}
          </div>
        </section>
        <section>
          <h3>What if they... <small>{items.length}/10</small></h3>
          <input type="search" placeholder="Add a title" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Add a title to the scenario" />
          {results.length > 0 && (
            <ul className="results">
              {results.map((t) => (
                <li key={t.id}><button disabled={items.length >= 10} onClick={() => { setItems([...items, { id: t.id, action: 'watched' }]); setSearch(''); }}>
                  {t.name}<span>{t.year}</span></button></li>
              ))}
            </ul>
          )}
          <ul className="scenario">
            {items.map((it, i) => (
              <li key={it.id}>
                <span className="name">{label(it.id)}</span>
                <select value={it.action} aria-label={`What happens with ${label(it.id)}`}
                  onChange={(e) => setItems(items.map((x, j) => (j === i ? { ...x, action: e.target.value as Action } : x)))}>
                  {(Object.keys(ACTION_LABEL) as Action[]).map((a) => <option key={a} value={a}>{ACTION_LABEL[a]}</option>)}
                </select>
                <button className="x" aria-label={`Remove ${label(it.id)}`} onClick={() => setItems(items.filter((_, j) => j !== i))}>×</button>
              </li>
            ))}
          </ul>
          {items.length === 0 && <p className="hint">Search for titles to add. Each one is watched to the end by default; switch it to a reaction instead.</p>}
        </section>
        <section>
          <h3>Never show <small>L3</small></h3>
          <div className="chips">
            {genres.map((g) => {
              const on = (excluded ?? []).includes(g);
              return <button key={g} className={on ? 'on' : ''} aria-pressed={on}
                onClick={() => setExcluded(on ? (excluded ?? []).filter((x) => x !== g) : [...(excluded ?? []), g])}>{g}</button>;
            })}
          </div>
        </section>
        <section>
          <h3>Time of day</h3>
          <div className="chips">
            {BUCKETS.map((b) => <button key={b.key} className={bucket === b.key ? 'on' : ''} aria-pressed={bucket === b.key} onClick={() => setBucket(b.key)}>{b.label}</button>)}
          </div>
        </section>
        <section>
          <h3>Ask the agent</h3>
          <input type="search" placeholder="Recommend something for me to watch tonight" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Request to the agent" />
        </section>
        <button className="run" disabled={running || !pid} onClick={run}>{running ? 'Running the real pipeline...' : 'Run scenario'}</button>
        {error && <p className="error" role="alert">{error}</p>}
        <p className="hint small">Runs the demo's own processor, agent and clips-feed code on a sandbox copy of this profile. Nothing is written to the real profile.</p>
      </aside>

      <section className="stage" aria-label="3D map of the decision">
        <MapScene layout={layout} colorBy="cluster" overlays={[]} selected={selected} selectedNeighbors={[]}
          focusCluster={null} showLabels={!result} onSelect={setSelected} target={null} autoRotate={false}
          highlights={view.highlights} ranks={view.ranks} markers={view.markers} />
        {result && panel === 'replay' && (
          <div className="legend-chips">
            <span><i style={{ background: C.base }} />Taste before</span>
            <span><i style={{ background: color }} />Taste after</span>
            {stage === 'retrieve' && <><span><i style={{ background: C.cand }} />Candidates</span><span><i style={{ background: C.excl }} />Excluded (watched)</span></>}
            {stage === 'rerank' && <><span><i style={{ background: C.pick }} />Agent picks</span><span><i style={{ background: C.drop }} />Dropped picks</span></>}
            {stage === 'interests' && <><span><i style={{ background: C.session }} />Session interest</span><span><i style={{ background: C.cand }} />Taste candidates</span><span><i style={{ background: C.md }} />Microdramas</span></>}
            {stage === 'picks' && <><span><i style={{ background: C.pick }} />For you</span><span><i style={{ background: C.cand }} />Explore</span></>}
          </div>
        )}
        {!result && !running && !events.length && <div className="stage-hint">Build a scenario on the left, then press Run scenario.</div>}
        {panel === 'live' && events.length > 0 && (
          <AgentFlow live={live} layout={layout} selected={focusNode} onSelect={setFocusNode} startedAt={startedAt} />
        )}
      </section>

      <aside className="right" aria-label="Decision replay">
        {(events.length > 0 || result) && (
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={panel === 'live'} className={panel === 'live' ? 'on' : ''} style={{ ['--c' as string]: C.cand }}
              onClick={() => setPanel('live')}>Live agent</button>
            <button role="tab" aria-selected={panel === 'replay'} className={panel === 'replay' ? 'on' : ''} disabled={!result}
              style={{ ['--c' as string]: C.cand }} onClick={() => setPanel('replay')}>Before and after</button>
          </div>
        )}
        {panel === 'live' && events.length > 0 ? (
          <div className="panel-body">
            <NodeDetail node={focusNode ?? live.active ?? 'rerank'} live={live} layout={layout} />
            {live.response && <><h3>Agent reply</h3><p className="plot">{live.response}</p></>}
            <p className="hint small">Everything here is streamed from the demo's own agent code running on the sandbox profile, with the same model settings as live. Click a node in the pipeline to inspect it.</p>
          </div>
        ) : result && B && W ? (
          <div className="panel-body">
            <div className="replay-controls">
              <button onClick={() => setPlaying(!playing)}>{playing ? 'Pause' : 'Play'}</button>
              <button onClick={() => { setPlaying(false); if (stage === 'profile' && step > 0) setStep(step - 1); else if (stageIdx > 0) setStage(STAGES[stageIdx - 1].key); }}>Back</button>
              <button onClick={() => { setPlaying(false); if (stage === 'profile' && step < result.steps.length - 1) setStep(step + 1); else if (stageIdx < STAGES.length - 1) setStage(STAGES[stageIdx + 1].key); }}>Next</button>
            </div>
            <ol className="stages">
              {STAGES.map((s, i) => (
                <li key={s.key}><button className={s.key === stage ? 'on' : i < stageIdx ? 'done' : ''} onClick={() => { setPlaying(false); setStage(s.key); }}>
                  <b>{i + 1}</b><span>{s.title}<small>{s.who}</small></span></button></li>
              ))}
            </ol>

            {stage === 'profile' && (
              result.steps.length ? <>
                <p className="hint">Step {step + 1} of {result.steps.length}: the stream processor's own update, applied to the sandbox.</p>
                <StepCard step={result.steps[Math.min(step, result.steps.length - 1)]} layout={layout} />
              </> : <p className="muted">No activity in this scenario: only settings or time of day change.</p>
            )}

            {stage === 'understand' && (
              <>
                <p className="hint">One cached LLM call turns the request into structured intent. The viewer's L3 rules are applied here; taste is not.</p>
                <table className="kv"><tbody>
                  {(['intent', 'path', 'category', 'search_text', 'genres_include', 'genres_exclude', 'active_rules', 'overridden_rules'] as const).map((k) => {
                    const v = W.agent.understand?.[k];
                    const text = Array.isArray(v) ? v.join(', ') : v === null || v === undefined || v === '' ? '-' : String(v);
                    return <tr key={k}><th>{k.replace(/_/g, ' ')}</th><td>{text || '-'}</td></tr>;
                  })}
                </tbody></table>
                <p className="hint">Request: "{result.query}". Path "taste" means an open request: the viewer's taste vectors become the search query.</p>
              </>
            )}

            {stage === 'retrieve' && (
              <>
                <p className="hint">Vector search over the catalog's 1024-d embeddings, steered by the taste vectors. Titles already watched are excluded before reranking.</p>
                {(W.agent.retrieval ?? []).map((r, i) => <p key={i}><b>{r.ids.length}</b> candidates via <code>{r.path}</code></p>)}
                <p className="meta">Before: {(B.agent.retrieval ?? []).reduce((n, r) => n + r.ids.length, 0)} candidates. Overlap with what-if: {(() => {
                  const a = new Set((W.agent.retrieval ?? []).flatMap((r) => r.ids));
                  return (B.agent.retrieval ?? []).flatMap((r) => r.ids).filter((id) => a.has(id)).length;
                })()}.</p>
                <p className="meta">Excluded as watched: {W.agent.reduce?.exclude_ids.length ?? 0} titles ({W.watched.length - B.watched.length} from this scenario).</p>
              </>
            )}

            {stage === 'rerank' && (
              <>
                <h3>What the agent reads <small>taste text</small></h3>
                <TextDiff before={B.agent.taste_text} after={W.agent.taste_text} />
                <h3>Agent picks</h3>
                <Diff before={B.agent.picks} after={W.agent.picks} label={label} />
                {W.agent.reduce?.response && <><h3>Agent reply</h3><p className="plot">{W.agent.reduce.response}</p></>}
                <p className="hint small">The rerank is an LLM call, so wording and order can vary between runs, as they do live.</p>
              </>
            )}

            {stage === 'interests' && (
              <>
                <p className="hint">The feed rebuilds interests from this session's reactions and the long-term taste vectors. Confidence sets how many of 6 cards are personalized.</p>
                <table className="kv"><tbody>
                  <tr><th>confidence</th><td>{B.clips.confidence.toFixed(2)} → <b>{W.clips.confidence.toFixed(2)}</b></td></tr>
                  <tr><th>personalized slots</th><td>{B.clips.personalized_slots} → <b>{W.clips.personalized_slots}</b> of 6</td></tr>
                </tbody></table>
                <ul className="interests">
                  {W.clips.interests.map((x, i) => (
                    <li key={i}><b style={{ color: x.kind === 'session' ? C.session : color }}>{x.kind === 'session' ? 'Session interest' : 'Long-term taste'}</b>
                      <span>weight {x.weight.toFixed(2)} · {x.candidates.length} candidates{x.sources.length ? ` · from ${x.sources.join(', ')}` : ''}</span></li>
                  ))}
                  {W.clips.negatives.map((n, i) => <li key={`n${i}`}><b style={{ color: C.drop }}>Avoid</b><span>near {n.sources.join(', ')} (penalty)</span></li>)}
                </ul>
              </>
            )}

            {stage === 'picks' && (
              <>
                <p className="hint">Microdramas score 1.5x so they lead; a diversity pass and at least one discovery card per page keep the feed varied.</p>
                <Diff before={B.clips.pages.flat().map((c) => c.id)} after={W.clips.pages.flat().map((c) => c.id)} label={(id) => {
                  const c = [...W.clips.pages.flat(), ...B.clips.pages.flat()].find((x) => x.id === id);
                  return c ? `${c.title} · ${c.reason === 'for-you' ? c.why : 'explore'}` : label(id);
                }} />
              </>
            )}
          </div>
        ) : (
          <div className="panel-body">
            <h2>What if...?</h2>
            <p>Change a viewer's activity and settings, then watch the system decide, stage by stage, with the same code the live demo runs.</p>
            <ul className="bullets">
              <li>The stream processor folds each new signal into the profile: entity scores move, and taste vectors glide across the map or split into new ones.</li>
              <li>The movie agent understands the request, retrieves candidates near the new taste and reranks them with the new taste text.</li>
              <li>The clips feed rebuilds its interests and picks a fresh first two pages.</li>
              <li>Every stage shows before and after.</li>
            </ul>
          </div>
        )}
      </aside>
    </main>
  );
}
