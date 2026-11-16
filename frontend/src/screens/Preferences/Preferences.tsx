import { useEffect, useMemo, useState, type KeyboardEvent } from 'react';
import { Navigate, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useDevice } from '../../components/device';
import { useHousehold } from '../../lib/household';
import { invalidateFollowed } from '../../lib/followed';
import { useSession } from '../../lib/session';
import { useBoothMode } from '../../lib/booth';
import {
  getDeclared,
  putDeclared,
  suggestEntities,
  type Declared,
  type DeclaredView,
  type EntitySuggestion,
  type FollowedEntry,
  type FollowedKind,
  type GenreOption,
} from '../../lib/settingsApi';
import './Preferences.css';

/**
 * One screen, three optional inputs (design: L3 cold-start onboarding).
 *  - Genres: tap once = love (an L2 head start that fades), twice = never (an
 *    L3 rule), a third time clears it.
 *  - Who do you follow: suggestions as you type from the sports registry
 *    (GET /profile/entities); a picked one is stored with its canonical id.
 *    Text with no match is saved as typed and shown as "not matched".
 *  - Anything to avoid: free text themes, matched by meaning by the agent.
 * `?first=1` is the onboarding run right after "Add profile" creates a member;
 * without it this is the same screen reached from "Edit" on the profile picker.
 */

type GenreState = 'love' | 'never';
const FIRST_GENRES = ['action', 'comedy', 'drama', 'sci-fi', 'thriller', 'horror', 'romance', 'documentary',
  'animation', 'crime', 'family', 'sport'];
const AVOID_SUGGESTIONS = ['jump scares', 'excessive gore', 'animal cruelty', 'sexual violence'];
const KIND_LABEL: Record<Exclude<FollowedKind, 'talent'>, string> = { teams: 'Team', players: 'Player', drivers: 'Driver' };
const MAX_TEXT = 80;

const clean = (s: string) => s.replace(/\s+/g, ' ').trim().slice(0, MAX_TEXT);

export function Preferences() {
  const device = useDevice();
  const navigate = useNavigate();
  const { profileId = '' } = useParams();
  const [params] = useSearchParams();
  const first = params.get('first') === '1';
  const { byId, state: householdState } = useHousehold();
  const { selectProfile } = useSession();
  const profile = byId(profileId);
  const booth = useBoothMode();

  const [view, setView] = useState<DeclaredView | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [genres, setGenres] = useState<Record<string, GenreState>>({});
  const [showAll, setShowAll] = useState(false);
  const [followed, setFollowed] = useState<{ kind: Exclude<FollowedKind, 'talent'>; entry: FollowedEntry }[]>([]);
  const [followText, setFollowText] = useState('');
  const [followKind, setFollowKind] = useState<Exclude<FollowedKind, 'talent'>>('teams');
  const [suggestions, setSuggestions] = useState<EntitySuggestion[]>([]);
  const [highlight, setHighlight] = useState(0);
  const [avoid, setAvoid] = useState<string[]>([]);
  const [avoidText, setAvoidText] = useState('');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const apply = (v: DeclaredView) => {
    setView(v);
    const g: Record<string, GenreState> = {};
    v.declared.excluded_genres.forEach((id) => (g[id] = 'never'));
    setGenres(g);
    setAvoid(v.declared.avoid_themes);
    setFollowed(
      (['teams', 'players', 'drivers'] as const).flatMap((kind) =>
        (v.declared.followed[kind] ?? []).map((entry) => ({ kind, entry })),
      ),
    );
  };

  useEffect(() => {
    if (!profileId) return;
    let active = true;
    getDeclared(profileId)
      .then((v) => active && apply(v))
      .catch(() => active && setLoadError(true));
    return () => {
      active = false;
    };
  }, [profileId]);

  // Type-ahead: ask the registry after a short pause; a newer keystroke cancels the older request.
  useEffect(() => {
    const q = clean(followText);
    if (q.length < 2) {
      setSuggestions([]);
      return;
    }
    const ctrl = new AbortController();
    const timer = window.setTimeout(() => {
      suggestEntities(q, followKind, ctrl.signal)
        .then((list) => {
          setSuggestions(list);
          setHighlight(0);
        })
        .catch(() => !ctrl.signal.aborted && setSuggestions([]));
    }, 150);
    return () => {
      window.clearTimeout(timer);
      ctrl.abort();
    };
  }, [followText, followKind]);

  const options: GenreOption[] = useMemo(() => {
    const all = view?.options?.genres ?? [];
    if (showAll) return all;
    const byKey = new Map(all.map((g) => [g.id, g]));
    const picked = FIRST_GENRES.map((id) => byKey.get(id)).filter(Boolean) as GenreOption[];
    // Keep anything already chosen visible even when it is not in the short list.
    const extra = all.filter((g) => genres[g.id] && !FIRST_GENRES.includes(g.id));
    return [...picked, ...extra];
  }, [view, showAll, genres]);

  if (householdState === 'ready' && !profile) {
    return <Navigate to="/profiles" replace />;
  }

  const cycleGenre = (id: string) =>
    setGenres((g) => {
      const next = { ...g };
      if (!g[id]) next[id] = 'love';
      else if (g[id] === 'love') next[id] = 'never';
      else delete next[id];
      return next;
    });

  const addEntry = (kind: Exclude<FollowedKind, 'talent'>, entry: FollowedEntry) => {
    const same = (f: { kind: string; entry: FollowedEntry }) =>
      entry.id ? f.entry.id === entry.id : f.kind === kind && f.entry.name.toLowerCase() === entry.name.toLowerCase();
    if (!followed.some(same)) setFollowed((f) => [...f, { kind, entry }]);
    setFollowText('');
    setSuggestions([]);
  };

  // A picked suggestion is stored with its id under its own kind; text with no
  // suggestion is saved as typed (the server still tries to resolve it on save).
  const addFollowed = (pick?: EntitySuggestion) => {
    const s = pick ?? suggestions[highlight];
    if (s) return addEntry(s.kind, { name: s.name, id: s.id });
    const name = clean(followText);
    if (name) addEntry(followKind, { name });
  };

  const onFollowKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!suggestions.length) return;
      e.preventDefault();
      const step = e.key === 'ArrowDown' ? 1 : -1;
      setHighlight((h) => (h + step + suggestions.length) % suggestions.length);
    } else if (e.key === 'Escape') {
      setSuggestions([]);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      addFollowed();
    }
  };

  const addAvoid = (raw: string) => {
    const t = clean(raw).toLowerCase();
    if (t && !avoid.includes(t) && avoid.length < 10) setAvoid((a) => [...a, t]);
    setAvoidText('');
  };

  const onEnter = (fn: () => void) => (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      fn();
    }
  };

  const finish = () => {
    if (first) {
      selectProfile(profileId);
      navigate('/home', { replace: true });
    } else {
      navigate('/profiles', { replace: true });
    }
  };

  // Skipping a new profile's onboarding saves nothing: it keeps the defaults
  // (version 0) and learns from viewing. It is offered once, at creation.
  const skip = finish;

  const save = async () => {
    if (!view) return;
    setSaving(true);
    setMessage(null);
    const byKind = (k: FollowedKind) => followed.filter((f) => f.kind === k).map((f) => f.entry);
    const declared: Declared = {
      ...view.declared,
      excluded_genres: Object.keys(genres).filter((id) => genres[id] === 'never'),
      avoid_themes: avoid,
      followed: { ...view.declared.followed, teams: byKind('teams'), players: byKind('players'), drivers: byKind('drivers') },
    };
    try {
      const r = await putDeclared(profileId, {
        version: view.version,
        source: first ? 'onboarding' : 'settings',
        declared,
        loved_genres: Object.keys(genres).filter((id) => genres[id] === 'love'),
      });
      if (r.status === 200) {
        invalidateFollowed(profileId);
        return finish();
      }
      if (r.status === 409 && r.body.current) {
        apply(r.body.current);
        setMessage('These preferences were just changed somewhere else. We loaded the latest; check and save again.');
      } else if (r.status === 403) {
        setMessage('Preferences are read-only in booth mode.');
      } else if (r.status === 400) {
        setMessage(r.body.details?.map((d) => d.reason).join('; ') ?? 'Something in the form is not valid.');
      } else {
        setMessage("Couldn't save right now. Try again.");
      }
    } catch {
      setMessage("Couldn't save right now. Try again.");
    } finally {
      setSaving(false);
    }
  };

  const loved = Object.values(genres).filter((s) => s === 'love').length;

  return (
    <div className={`screen prefs-screen ${device}`}>
      <div className="prefs-panel" role="form" aria-labelledby="prefs-title">
        <div className="prefs-top">
          <div className="prefs-who">
            <span className="prefs-avatar" style={{ background: `hsl(${profile?.hue ?? 210} 60% 40%)` }}>
              {profile?.icon ?? '🙂'}
            </span>
            {profile?.name ?? ''}
          </div>
          <button className="prefs-skip" onClick={skip}>
            {first ? 'Skip for now' : 'Cancel'}
          </button>
        </div>

        <div>
          <h1 id="prefs-title" className="prefs-title">{first ? 'Make it yours' : 'Your preferences'}</h1>
          <p className="prefs-sub">
            {first ? 'Three quick picks, all optional. ' : ''}Change them any time from Edit on the profile screen.
          </p>
        </div>

        {loadError && <p className="prefs-msg">Couldn't load preferences. Go back and try again.</p>}
        {!view && !loadError && <p className="prefs-sub">Loading…</p>}

        {view && (
          <>
            <section className="prefs-sec">
              <div className="prefs-lbl">
                Genres <span className="prefs-hint">tap once = love · twice = never · again = clear</span>
              </div>
              <div className="prefs-chips">
                {options.map((g) => (
                  <button
                    key={g.id}
                    className={`prefs-chip ${genres[g.id] ?? ''}`}
                    aria-pressed={Boolean(genres[g.id])}
                    aria-label={`${g.label}${genres[g.id] ? `: ${genres[g.id]}` : ''}`}
                    onClick={() => cycleGenre(g.id)}
                  >
                    {genres[g.id] === 'love' && '♥ '}
                    {g.label}
                  </button>
                ))}
              </div>
              <button className="prefs-link" onClick={() => setShowAll((s) => !s)}>
                {showAll ? 'Show fewer' : `Show all ${view.options?.genres.length ?? ''}`}
              </button>
              {loved > 0 && <p className="prefs-note">Loved genres give your recommendations a head start and adjust as you watch.</p>}
            </section>

            <section className="prefs-sec">
              <div className="prefs-lbl">
                Who do you follow? <span className="prefs-hint">teams, players</span>
              </div>
              {followed.length > 0 && (
                <div className="prefs-chips">
                  {followed.map((f, i) => (
                    <span key={`${f.kind}-${f.entry.id ?? f.entry.name}`} className={`prefs-chip set${f.entry.id ? '' : ' unmatched'}`}>
                      {f.entry.name} <small>{f.entry.id ? KIND_LABEL[f.kind] : 'not matched'}</small>
                      <button
                        className="prefs-x"
                        aria-label={`Remove ${f.entry.name}`}
                        onClick={() => setFollowed((all) => all.filter((_, j) => j !== i))}
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
              <div className="prefs-row">
                <select
                  className="prefs-select"
                  value={followKind}
                  aria-label="Kind"
                  onChange={(e) => setFollowKind(e.target.value as Exclude<FollowedKind, 'talent'>)}
                >
                  {Object.entries(KIND_LABEL).map(([k, label]) => (
                    <option key={k} value={k}>{label}</option>
                  ))}
                </select>
                <input
                  className="prefs-input"
                  value={followText}
                  maxLength={MAX_TEXT}
                  placeholder="e.g. Harbor City FC, Kofi Asante-Brook"
                  aria-label="Team, player or driver"
                  role="combobox"
                  aria-expanded={suggestions.length > 0}
                  aria-controls="prefs-follow-suggestions"
                  aria-autocomplete="list"
                  onChange={(e) => setFollowText(e.target.value)}
                  onKeyDown={onFollowKey}
                />
                <button className="prefs-btn" onClick={() => addFollowed()} disabled={!clean(followText)}>Add</button>
              </div>
              {suggestions.length > 0 && (
                <ul className="prefs-suggest" id="prefs-follow-suggestions" role="listbox" aria-label="Matches">
                  {suggestions.map((s, i) => (
                    <li key={s.id} role="option" aria-selected={i === highlight}>
                      <button
                        className={`prefs-suggest-item${i === highlight ? ' on' : ''}`}
                        onMouseEnter={() => setHighlight(i)}
                        onClick={() => addFollowed(s)}
                      >
                        <span>{s.name}</span>
                        <small>
                          {KIND_LABEL[s.kind]}
                          {s.team ? ` · ${s.team}` : ''}
                          {s.number != null ? ` · #${s.number}` : ''}
                        </small>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="prefs-sec">
              <div className="prefs-lbl">
                Anything you'd rather not see? <span className="prefs-hint">free text</span>
              </div>
              {avoid.length > 0 && (
                <div className="prefs-chips">
                  {avoid.map((t) => (
                    <span key={t} className="prefs-chip set">
                      {t}
                      <button className="prefs-x" aria-label={`Remove ${t}`} onClick={() => setAvoid((a) => a.filter((x) => x !== t))}>
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
              <div className="prefs-row">
                <input
                  className="prefs-input"
                  value={avoidText}
                  maxLength={MAX_TEXT}
                  placeholder="e.g. jump scares"
                  aria-label="Theme to avoid"
                  onChange={(e) => setAvoidText(e.target.value)}
                  onKeyDown={onEnter(() => addAvoid(avoidText))}
                />
                <button className="prefs-btn" onClick={() => addAvoid(avoidText)} disabled={!clean(avoidText)}>Add</button>
              </div>
              <div className="prefs-chips">
                {AVOID_SUGGESTIONS.filter((s) => !avoid.includes(s)).map((s) => (
                  <button key={s} className="prefs-chip suggest" onClick={() => addAvoid(s)}>+ {s}</button>
                ))}
              </div>
            </section>
          </>
        )}

        {message && <p className="prefs-msg" role="alert">{message}</p>}

        <div className="prefs-foot">
          <span className="prefs-hint">{booth ? 'Booth mode: read-only' : first ? 'About 20 seconds' : ''}</span>
          <button className="prefs-btn primary" onClick={save} disabled={!view || saving || booth}>
            {saving ? 'Saving…' : first ? 'Start watching' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}
