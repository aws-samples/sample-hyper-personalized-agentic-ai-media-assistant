import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  fetchLayout, fetchPersonas, fetchTaste, fetchTitle,
  type Layout, type Persona, type Taste, type TitleDetail, type Vec3,
} from './api';
import { currentUser, loadConfig, signIn, signOut, type AppConfig } from './auth';
import { CLUSTER_COLORS, HOME_DISTANCE, MapScene, TYPE_COLORS, type Overlay } from './MapScene';

/** Camera distance when focusing a viewer's taste: close enough to read its
 *  neighbourhood, far enough to keep the surrounding catalog in view. */
const PERSONA_DISTANCE = 75;
import { PersonaPanel, TitlePanel } from './Panels';
import { WhatIfView } from './WhatIf';

const PERSONA_COLORS = ['#ff8a3d', '#ff4fa3', '#7dd3fc', '#b9f227'];
const POLL_MS = 10_000;

function Login({ onDone }: { onDone: (user: string) => void }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await signIn(username.trim(), password);
      onDone(username.trim());
    } catch (err) {
      setError((err as Error).message || 'Sign-in failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="login">
      <form onSubmit={submit} aria-labelledby="login-title">
        <p className="eyebrow">Behind the curtain</p>
        <h1 id="login-title">The system behind every personalized screen</h1>
        <p className="muted">Sign in with your demo account.</p>
        <label>Username<input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" disabled={busy}>{busy ? 'Signing in...' : 'Sign in'}</button>
      </form>
    </div>
  );
}

function Intro({ layout }: { layout: Layout }) {
  const built = new Date(layout.builtAt * 1000);
  return (
    <div className="panel-body">
      <h2>The catalog's semantic layer</h2>
      <p>
        Every point is one of the <b>{layout.count.toLocaleString()}</b> titles in the catalog, placed by meaning.
        Each title already has a 1024-dimension Amazon Nova multimodal embedding of its plot, genres, cast and themes;
        here those vectors are projected into 3D, so titles that feel alike sit together.
      </p>
      <p>
        The same vectors power everything the viewer sees: the agent's search, the clips feed, and the taste vectors in
        each profile. Pick a viewer on the left to see where their taste sits in this space.
      </p>
      <h3>How to read it</h3>
      <ul className="bullets">
        <li>Neighbourhoods are meaningful; exact distances in 3D are approximate.</li>
        <li>Similarity scores in the panels always come from the full 1024-d vectors.</li>
        <li>Large pink points are the microdramas.</li>
        <li>Drag to orbit, scroll to zoom, click a point for its details.</li>
      </ul>
      <p className="muted small">Map built {built.toLocaleString()} from the live catalog; rebuilt automatically when the catalog changes.</p>
    </div>
  );
}

export default function App() {
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [user, setUser] = useState<string | null | undefined>(undefined);
  const [layout, setLayout] = useState<Layout | null>(null);
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [active, setActive] = useState<string[]>([]);
  const [tastes, setTastes] = useState<Record<string, Taste>>({});
  const [changed, setChanged] = useState<Record<string, boolean>>({});
  const [panelPersona, setPanelPersona] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<TitleDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [colorBy, setColorBy] = useState<'cluster' | 'type'>('cluster');
  const [focusCluster, setFocusCluster] = useState<number | null>(null);
  const [showLabels, setShowLabels] = useState(true);
  const [query, setQuery] = useState('');
  const [target, setTarget] = useState<Vec3 | null>(null);
  const [distance, setDistance] = useState<number | null>(null);
  const [error, setError] = useState('');
  const [mode, setMode] = useState<'map' | 'whatif'>('map');
  const versions = useRef<Record<string, number>>({});

  useEffect(() => {
    loadConfig().then(setCfg).then(currentUser).then(setUser).catch((e) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!user) return;
    Promise.all([fetchLayout(), fetchPersonas()])
      .then(([l, p]) => {
        setLayout(l);
        setPersonas(p);
      })
      .catch((e) => setError(`Could not load data: ${e.message}`));
  }, [user]);

  const colorOf = useCallback(
    (pid: string) => PERSONA_COLORS[Math.max(0, personas.findIndex((p) => p.profile_id === pid)) % PERSONA_COLORS.length],
    [personas],
  );

  // Live: re-read the active profiles every few seconds; flag a new L2 version.
  useEffect(() => {
    if (!active.length) return;
    let live = true;
    const load = () =>
      Promise.all(active.map((pid) => fetchTaste(pid).then((t) => [pid, t] as const)))
        .then((rows) => {
          if (!live) return;
          const flags: Record<string, boolean> = {};
          for (const [pid, t] of rows) {
            const prev = versions.current[pid];
            flags[pid] = prev !== undefined && prev !== t.version;
            versions.current[pid] = t.version;
          }
          setTastes((old) => ({ ...old, ...Object.fromEntries(rows) }));
          setChanged(flags);
        })
        .catch(() => {});
    load();
    const timer = setInterval(load, POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [active]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let live = true;
    setDetailLoading(true);
    fetchTitle(selected)
      .then((d) => live && setDetail(d))
      .catch(() => live && setDetail(null))
      .finally(() => live && setDetailLoading(false));
    return () => {
      live = false;
    };
  }, [selected]);

  const select = useCallback((id: string | null) => {
    setSelected(id);
    if (id && layout) {
      const i = layout.index.get(id);
      if (i !== undefined) {
        setTarget([...layout.titles[i].pos] as Vec3);
        setDistance(null);
      }
    }
  }, [layout]);

  const togglePersona = (pid: string) => {
    setActive((cur) => {
      const next = cur.includes(pid) ? cur.filter((x) => x !== pid) : [...cur, pid];
      setPanelPersona(next.includes(pid) ? pid : next[0] ?? null);
      return next;
    });
    setSelected(null);
    setFocusCluster(null);
  };

  // Fly in toward the first taste vector of a newly shown viewer -- once per
  // viewer or when that vector actually moves, not on every live re-read, so
  // the user's own orbit/zoom is not pulled back every few seconds. With no
  // viewer and no title selected, glide back to the whole-catalog view.
  const flewTo = useRef<string | null>(null);
  useEffect(() => {
    if (selected) return;
    const t = panelPersona ? tastes[panelPersona] : null;
    const p = t?.taste_vectors.find((v) => v.position)?.position;
    const key = panelPersona && p ? `${panelPersona}:${p.join(',')}` : null;
    if (key === flewTo.current) return;
    if (panelPersona && !p) return;   // its taste is still loading
    flewTo.current = key;
    setTarget(p ?? null);
    setDistance(p ? PERSONA_DISTANCE : HOME_DISTANCE);
  }, [panelPersona, tastes, selected]);

  const overlays: Overlay[] = useMemo(
    () => active.filter((pid) => tastes[pid]).map((pid) => ({
      name: personas.find((p) => p.profile_id === pid)?.name ?? pid, color: colorOf(pid), taste: tastes[pid],
    })),
    [active, tastes, personas, colorOf],
  );

  const results = useMemo(() => {
    if (!layout || query.trim().length < 2) return [];
    const q = query.trim().toLowerCase();
    return layout.titles.filter((t) => t.name.toLowerCase().includes(q)).slice(0, 8);
  }, [layout, query]);

  if (error) return <div className="fatal" role="alert">{error}</div>;
  if (user === undefined || !cfg) return <div className="fatal muted">Loading...</div>;
  if (!user) return <Login onDone={setUser} />;

  const panelTaste = panelPersona ? tastes[panelPersona] : null;
  const panelName = personas.find((p) => p.profile_id === panelPersona)?.name ?? '';

  return (
    <div className="app">
      <header>
        <div className="brand">
          <span className="eyebrow">Behind the curtain</span>
          <h1>How every screen gets personalized</h1>
        </div>
        <nav aria-label="Views">
          {(['map', 'whatif'] as const).map((m) => (
            <button key={m} className={`tab${mode === m ? ' on' : ''}`} aria-current={mode === m ? 'page' : undefined}
              onClick={() => setMode(m)}>{m === 'map' ? 'Semantic map' : 'What if'}</button>
          ))}
        </nav>
        <div className="header-actions">
          {cfg.demoUrl && <a href={cfg.demoUrl} target="_blank" rel="noreferrer">Open the demo</a>}
          <button className="ghost" onClick={() => { signOut(); setUser(null); }}>Sign out</button>
        </div>
      </header>

      {mode === 'whatif' && layout ? <WhatIfView layout={layout} personas={personas} colorOf={colorOf} /> : mode === 'whatif' ? (
        <div className="loading">Loading the catalog map...</div>
      ) : (
      <main>
        <aside className="left" aria-label="Map controls">
          <section>
            <h3>Find a title</h3>
            <input type="search" placeholder="Search the catalog" value={query} onChange={(e) => setQuery(e.target.value)}
              aria-label="Search the catalog" />
            {results.length > 0 && (
              <ul className="results">
                {results.map((t) => (
                  <li key={t.id}><button onClick={() => { select(t.id); setQuery(''); }}>{t.name}<span>{t.year}</span></button></li>
                ))}
              </ul>
            )}
          </section>

          <section>
            <h3>Viewer profiles</h3>
            <p className="hint">Overlay each viewer's taste vectors and watch history. Updates live while they use the demo.</p>
            <div className="personas">
              {personas.filter((p) => p.has_profile).map((p) => (
                <button key={p.profile_id} className={`persona${active.includes(p.profile_id) ? ' on' : ''}`}
                  style={{ ['--c' as string]: colorOf(p.profile_id) }} aria-pressed={active.includes(p.profile_id)}
                  onClick={() => togglePersona(p.profile_id)}>
                  <span className="dot" />{p.name}
                </button>
              ))}
            </div>
          </section>

          <section>
            <h3>Colour by</h3>
            <div className="seg" role="radiogroup" aria-label="Colour by">
              {(['cluster', 'type'] as const).map((k) => (
                <button key={k} role="radio" aria-checked={colorBy === k} className={colorBy === k ? 'on' : ''} onClick={() => setColorBy(k)}>
                  {k === 'cluster' ? 'Semantic cluster' : 'Content type'}
                </button>
              ))}
            </div>
            <label className="check"><input type="checkbox" checked={showLabels} onChange={(e) => setShowLabels(e.target.checked)} /> Cluster labels</label>
          </section>

          {layout && colorBy === 'cluster' && (
            <section className="legend">
              <h3>Clusters <small>{layout.clusters.length}</small></h3>
              <ul>
                {[...layout.clusters].sort((a, b) => b.size - a.size).map((c) => (
                  <li key={c.id}>
                    <button className={focusCluster === c.id ? 'on' : ''} title={c.examples.join(', ')}
                      onClick={() => {
                        const on = focusCluster === c.id ? null : c.id;
                        setFocusCluster(on);
                        setSelected(null);
                        setTarget(on === null ? [0, 0, 0] : c.center);
                      }}>
                      <span className="dot" style={{ background: CLUSTER_COLORS[c.id % CLUSTER_COLORS.length] }} />
                      <span className="name">{c.label}</span><span className="num">{c.size}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {layout && colorBy === 'type' && (
            <section className="legend">
              <h3>Content types</h3>
              <ul>
                {Object.entries(TYPE_COLORS).map(([k, c]) => (
                  <li key={k}><span className="row"><span className="dot" style={{ background: c }} /><span className="name">{k === 'micro-drama' ? 'microdrama' : k}</span>
                    <span className="num">{layout.titles.filter((t) => t.type === k).length}</span></span></li>
                ))}
              </ul>
            </section>
          )}
        </aside>

        <section className="stage" aria-label="3D map of the catalog">
          {layout ? (
            <MapScene layout={layout} colorBy={colorBy} overlays={overlays} selected={selected}
              selectedNeighbors={detail?.id === selected ? detail?.neighbors ?? [] : []}
              focusCluster={focusCluster} showLabels={showLabels} onSelect={select} target={target} distance={distance}
              autoRotate={!selected && focusCluster === null &&
                !window.matchMedia('(prefers-reduced-motion: reduce)').matches} />
          ) : <div className="loading">Loading the catalog map...</div>}
          {layout && (
            <div className="caption">
              {layout.count.toLocaleString()} titles · Amazon Nova multimodal embeddings (1024-d) · UMAP projection to 3D
            </div>
          )}
        </section>

        <aside className="right" aria-label="Details">
          {active.length > 1 && !selected && (
            <div className="tabs" role="tablist">
              {active.map((pid) => (
                <button key={pid} role="tab" aria-selected={panelPersona === pid} className={panelPersona === pid ? 'on' : ''}
                  style={{ ['--c' as string]: colorOf(pid) }} onClick={() => setPanelPersona(pid)}>
                  {personas.find((p) => p.profile_id === pid)?.name}
                </button>
              ))}
            </div>
          )}
          {selected ? (
            <>
              <button className="back" onClick={() => select(null)}>{panelTaste ? `Back to ${panelName}` : 'Back to overview'}</button>
              <TitlePanel detail={detail} loading={detailLoading} onSelect={select} />
            </>
          ) : panelTaste ? (
            <PersonaPanel name={panelName} color={colorOf(panelPersona!)} taste={panelTaste}
              changed={!!changed[panelPersona!]} onSelect={select} />
          ) : panelPersona ? (
            <div className="panel-body muted">Loading profile...</div>
          ) : layout ? <Intro layout={layout} /> : null}
        </aside>
      </main>
      )}
    </div>
  );
}
