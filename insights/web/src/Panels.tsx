import type { Entity, Neighbor, Taste, TitleDetail } from './api';

/** A stored vector counts positive signals (a title can repeat); a preview is built from distinct titles. */
export function countLabel(count: number, source: Taste['taste_source']) {
  const unit = source === 'stored' ? 'signal' : 'title';
  return `${count} ${unit}${count === 1 ? '' : 's'}`;
}

function Similar({ items, onSelect }: { items: Neighbor[]; onSelect: (id: string) => void }) {
  // Cosines in this embedding space sit around 0.6-0.9; scale bars to that band.
  const bar = (c: number) => `${Math.max(4, Math.min(100, ((c - 0.55) / 0.4) * 100))}%`;
  return (
    <ol className="similar">
      {items.map((n) => (
        <li key={n.id}>
          <button onClick={() => onSelect(n.id)}>
            <span className="name">{n.name}</span>
            <span className="bar"><i style={{ width: bar(n.cosine) }} /></span>
            <span className="num">{n.cosine.toFixed(3)}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}

export function TitlePanel({ detail, loading, onSelect }: { detail: TitleDetail | null; loading: boolean; onSelect: (id: string) => void }) {
  if (loading && !detail) return <div className="panel-body muted">Loading title...</div>;
  if (!detail) return null;
  const genres = Array.isArray(detail.genres) ? detail.genres.join(', ') : detail.genres;
  return (
    <div className="panel-body">
      <div className="title-head">
        {detail.poster && <img src={detail.poster} alt="" />}
        <div>
          <h2>{detail.name}</h2>
          <p className="meta">
            {[detail.year, detail.type === 'micro-drama' ? 'microdrama' : detail.type, detail.rating ? `★ ${detail.rating}` : null]
              .filter(Boolean).join(' · ')}
          </p>
          {genres && <p className="meta">{genres}</p>}
          {detail.directors.length > 0 && <p className="meta">Directed by {detail.directors.join(', ')}</p>}
        </div>
      </div>
      {detail.plot && <p className="plot">{detail.plot}</p>}
      <h3>Nearest in embedding space</h3>
      <p className="hint">Cosine similarity of the full 1024-dimension Nova vectors, the same space the agent and the clips feed search.</p>
      <Similar items={detail.neighbors} onSelect={onSelect} />
    </div>
  );
}

function EntityBars({ rows }: { rows: Entity[] }) {
  return (
    <ul className="entities">
      {rows.map((e) => (
        <li key={e.key} className={e.used ? 'used' : ''} title={`stored ${e.s.toFixed(3)}, now ${e.s_now.toFixed(3)} after fading, ${e.n} signals`}>
          <span className="kind">{e.kind}</span>
          <span className="name">{e.label}</span>
          <span className={`bar${e.s_now < 0 ? ' neg' : ''}`}><i style={{ width: `${Math.min(100, Math.abs(e.s_now) * 100)}%` }} /></span>
          <span className="num">{e.s_now.toFixed(2)}</span>
          <span className="n">n={e.n}</span>
        </li>
      ))}
    </ul>
  );
}

function ago(iso: string | null) {
  if (!iso) return 'never';
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

export function PersonaPanel({ name, color, taste, changed, onSelect }:
  { name: string; color: string; taste: Taste; changed: boolean; onSelect: (id: string) => void }) {
  const genres = taste.entities.filter((e) => e.kind === 'genre').slice(0, 8);
  const people = taste.entities.filter((e) => e.kind === 'person').slice(0, 8);
  const contexts = Object.entries(taste.contexts).sort((a, b) => b[1].views - a[1].views);
  const declared = taste.declared as Record<string, unknown>;
  const limits = [
    Array.isArray(declared.excluded_genres) && declared.excluded_genres.length ? `Never: ${declared.excluded_genres.join(', ')}` : null,
    Array.isArray(declared.avoid_themes) && declared.avoid_themes.length ? `Avoid: ${declared.avoid_themes.join(', ')}` : null,
    Array.isArray(declared.languages) && declared.languages.length ? `Languages: ${declared.languages.join(', ')}` : null,
  ].filter(Boolean);
  return (
    <div className="panel-body">
      <div className="persona-head">
        <span className="dot" style={{ background: color }} />
        <h2>{name}</h2>
        <span className={`live${changed ? ' pulse' : ''}`}>L2 v{taste.version} · {ago(taste.updated_at)}</span>
      </div>

      <h3>Taste vectors <small>L2</small></h3>
      <p className="hint">
        {taste.taste_source === 'stored'
          ? 'Stored in the profile. Each one is a running average of the embeddings of titles the viewer responded well to, in the same space as the catalog. Every positive signal (a finished view, a liked clip, a "More like this") nudges it, so one title can count more than once.'
          : 'This profile was seeded before taste vectors existed. Previewed here from its viewing with the same grouping rule; nothing is written back.'}
      </p>
      {taste.taste_vectors.length === 0 && <p className="muted">No taste vectors yet.</p>}
      {taste.taste_vectors.map((v) => (
        <div key={v.index} className="vector">
          <div className="vector-head">
            <b style={{ color }}>Vector {v.index + 1}</b> {countLabel(v.count, taste.taste_source)} · weight {Math.round(v.weight * 100)}%
          </div>
          <Similar items={v.neighbors.slice(0, 6)} onSelect={onSelect} />
        </div>
      ))}

      <h3>What the agent reads <small>L2 to prompt</small></h3>
      <p className="hint">The exact taste text the movie agent builds from this profile for reranking. Only entities with at least 3 signals and a score of 0.3 or more make it in.</p>
      <pre className="prompt">{taste.agent_taste_text.join('\n') || '(nothing yet)'}</pre>

      <h3>Entity scores <small>L2</small></h3>
      <p className="hint">Scores fade with a 30-day half-life for genres and 60 days for people. Highlighted rows are the ones the agent uses.</p>
      <EntityBars rows={genres} />
      <EntityBars rows={people} />

      {contexts.length > 0 && (
        <>
          <h3>Time-of-day taste <small>L2 contexts</small></h3>
          <ul className="contexts">
            {contexts.map(([key, c]) => (
              <li key={key} className={key === taste.current_bucket ? 'now' : ''}>
                <b>{c.name}{key === taste.current_bucket ? ' (now)' : ''}</b>
                <span>{c.views} views · {Math.round(c.long_min)} min long-form · {Math.round(c.short_min)} min short-form</span>
                {c.top.length > 0 && <span className="muted">{c.top.join(', ')}</span>}
              </li>
            ))}
          </ul>
        </>
      )}

      <h3>Watch state <small>{taste.watched.length} titles</small></h3>
      <ul className="watched">
        {taste.watched.slice(0, 14).map((w) => (
          <li key={w.id}>
            {w.poster ? (
              <a className="thumb" href={w.poster} target="_blank" rel="noreferrer" aria-label={`Open the poster for ${w.name}`}>
                <img src={w.poster} alt="" loading="lazy" />
              </a>
            ) : <span className="thumb empty" aria-hidden="true" />}
            <button onClick={() => onSelect(w.id)} disabled={!w.position}>
              <span className="name">{w.name}</span>
              <span className="muted">{w.content_type ?? ''}</span>
              <span className="num">{w.completed ? 'finished' : `${Math.round(w.completion * 100)}%`}</span>
            </button>
          </li>
        ))}
      </ul>

      {limits.length > 0 && (
        <>
          <h3>Viewer settings <small>L3</small></h3>
          <ul className="limits">{limits.map((l) => <li key={l as string}>{l}</li>)}</ul>
        </>
      )}
    </div>
  );
}
