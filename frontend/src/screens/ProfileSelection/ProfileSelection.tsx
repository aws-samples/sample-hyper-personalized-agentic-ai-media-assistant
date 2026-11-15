import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useDevice } from '../../components/device';
import { useHousehold } from '../../lib/household';
import { Poster } from '../../components/Poster';
import { useSession } from '../../lib/session';
import { useBoothMode } from '../../lib/booth';
import { addMember } from '../../lib/api';
import type { DemoProfile } from '../../data/profiles';
import './ProfileSelection.css';

// NOTE: A featured "#1 in TV Shows Today" promo (with real key art) used to live
// here. It's temporarily removed for a plain profile screen — restore from git
// history if we want the featured hero back.

/** Matches household_core.MAX_MEMBERS / MAX_NAME_LEN; the API enforces both. */
const MAX_MEMBERS = 5;
const MAX_NAME = 20;

export function ProfileSelection() {
  const device = useDevice();
  const navigate = useNavigate();
  const { selectProfile } = useSession();
  const { profiles, members, removed, state, added, remove, restore } = useHousehold();
  const [focused, setFocused] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState('');
  const [kids, setKids] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Profile awaiting the remove confirmation, and the Removed-list status.
  const [removing, setRemoving] = useState<DemoProfile | null>(null);
  const [restoring, setRestoring] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  // Booth users (Cognito group `booth`) have a read-only household; the API
  // refuses the same calls. The last profile can't be removed.
  const booth = useBoothMode();
  const canAdd = state === 'ready' && !booth && members < MAX_MEMBERS;
  const canManage = state === 'ready' && !booth;
  const canRemove = canManage && members > 1;

  const confirmRemove = async () => {
    if (!removing || busy) return;
    setBusy(true);
    setError(null);
    const err = await remove(removing.id);
    setBusy(false);
    if (err) {
      setError(err);
      return;
    }
    setRemoving(null);
  };

  const doRestore = async (id: string) => {
    if (restoring) return;
    setRestoring(id);
    setListError(null);
    const err = await restore(id);
    setRestoring(null);
    if (err) setListError(err);
  };

  const choose = (id: string) => {
    if (editing) {
      navigate(`/preferences/${id}`);
      return;
    }
    selectProfile(id);
    navigate('/home');
  };

  const openAdd = () => {
    setName('');
    setKids(false);
    setError(null);
    setAdding(true);
  };

  const create = async (e: FormEvent) => {
    e.preventDefault();
    const clean = name.replace(/\s+/g, ' ').trim();
    if (!clean || busy) return;
    setBusy(true);
    setError(null);
    const r = await addMember(clean, kids).catch(() => null);
    setBusy(false);
    if (!r || !r.ok) {
      setError(r?.error ?? "Couldn't create the profile. Try again.");
      return;
    }
    added(r.member);
    setAdding(false);
    // Onboarding ("Make it yours") runs only here, for a profile just created.
    // Kids profiles skip it: their limit is set, and taste is learned from viewing.
    if (kids) {
      selectProfile(r.member.profileId);
      navigate('/home');
    } else {
      navigate(`/preferences/${r.member.profileId}?first=1`);
    }
  };

  const dialog = adding && (
    <div className="add-profile-scrim" role="presentation" onClick={() => !busy && setAdding(false)}>
      <form
        className="add-profile-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-profile-title"
        onClick={(e) => e.stopPropagation()}
        onSubmit={create}
      >
        <h2 id="add-profile-title">Add profile</h2>
        <label className="add-profile-field">
          <span>Name</span>
          <input
            autoFocus
            value={name}
            maxLength={MAX_NAME}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Sam"
          />
        </label>
        <label className="add-profile-kids">
          <input type="checkbox" checked={kids} onChange={(e) => setKids(e.target.checked)} />
          <span>
            Kids profile <small>Only titles suitable for children</small>
          </span>
        </label>
        {error && (
          <p className="add-profile-error" role="alert">
            {error}
          </p>
        )}
        <div className="add-profile-actions">
          <button type="button" className="add-profile-btn" onClick={() => setAdding(false)} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="add-profile-btn primary" disabled={busy || !name.trim()}>
            {busy ? 'Creating…' : 'Continue'}
          </button>
        </div>
      </form>
    </div>
  );

  const removeDialog = removing && (
    <div className="add-profile-scrim" role="presentation" onClick={() => !busy && setRemoving(null)}>
      <div
        className="add-profile-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="remove-profile-title"
        aria-describedby="remove-profile-desc"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="remove-profile-title">Remove {removing.name}?</h2>
        <p id="remove-profile-desc" className="remove-profile-note">
          The profile is hidden from this screen. Its settings, preferences and watch history are kept, so you can
          restore it later from Edit.
        </p>
        {error && (
          <p className="add-profile-error" role="alert">
            {error}
          </p>
        )}
        <div className="add-profile-actions">
          <button type="button" className="add-profile-btn" onClick={() => setRemoving(null)} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="add-profile-btn primary" onClick={confirmRemove} disabled={busy} autoFocus>
            {busy ? 'Removing…' : 'Remove'}
          </button>
        </div>
      </div>
    </div>
  );

  const removedList = editing && canManage && removed.length > 0 && (
    <section className="removed-profiles" aria-labelledby="removed-profiles-title">
      <h2 id="removed-profiles-title">Removed profiles</h2>
      <ul>
        {removed.map((p) => (
          <li key={p.id}>
            <span className="removed-avatar" aria-hidden="true">
              {p.icon}
            </span>
            <span className="removed-name">{p.name}</span>
            <button
              type="button"
              className="add-profile-btn"
              onClick={() => doRestore(p.id)}
              disabled={Boolean(restoring) || members >= MAX_MEMBERS}
              aria-label={`Restore ${p.name}`}
            >
              {restoring === p.id ? 'Restoring…' : 'Restore'}
            </button>
          </li>
        ))}
      </ul>
      {members >= MAX_MEMBERS && (
        <p className="removed-hint">A household can have {MAX_MEMBERS} profiles. Remove one to restore another.</p>
      )}
      {listError && (
        <p className="add-profile-error" role="alert">
          {listError}
        </p>
      )}
    </section>
  );

  const grid = (
    <div className={`profile-grid ${device} ${editing ? 'editing' : ''}`}>
      {profiles.map((p) => (
        <div key={p.id} className="profile-slot">
          <button
            className={`profile-card ${(focused ?? profiles[0]?.id) === p.id ? 'focused' : ''}`}
            onMouseEnter={() => setFocused(p.id)}
            onFocus={() => setFocused(p.id)}
            onClick={() => choose(p.id)}
            aria-label={editing ? `Edit preferences for ${p.name}` : p.name}
          >
            <span
              className={`profile-avatar ${p.kid ? 'kids' : ''}`}
              style={
                p.kid
                  ? undefined
                  : {
                      background: `linear-gradient(145deg, hsl(${p.hue} 65% 46%), hsl(${p.hue + 30} 60% 30%))`,
                    }
              }
            >
              <span className="profile-emoji">{p.icon}</span>
              {p.kid && <span className="kid-tag">KIDS</span>}
              {editing && (
                <span className="edit-badge" aria-hidden="true">
                  ✎
                </span>
              )}
            </span>
            <span className="profile-name">{p.name}</span>
          </button>
          {editing && canRemove && (
            <button
              type="button"
              className="profile-remove"
              onClick={() => {
                setError(null);
                setRemoving(p);
              }}
              aria-label={`Remove ${p.name}`}
            >
              Remove
            </button>
          )}
        </div>
      ))}
      {canAdd && !editing && (
        <button className="profile-card add-card" onClick={openAdd}>
          <span className="profile-avatar edit-avatar">
            <span className="edit-pencil">+</span>
          </span>
          <span className="profile-name">Add profile</span>
        </button>
      )}
      <button className="profile-card edit-card" onClick={() => setEditing((e) => !e)} aria-pressed={editing}>
        <span className="profile-avatar edit-avatar">
          <span className="edit-pencil">{editing ? '✓' : '✎'}</span>
        </span>
        <span className="profile-name">{editing ? 'Done' : 'Edit'}</span>
      </button>
    </div>
  );

  // ---- Mobile: full-bleed cinematic bg + frosted profile panel (Netflix) ----
  if (device === 'mobile') {
    return (
      <div className="screen profile-screen mobile-netflix">
        <div className="profile-bg">
          <Poster hueA={220} hueB={285} ratio="2/3" rounded={0} showLabel={false} style={{ height: '100%' }} />
          <div className="profile-bg-scrim" />
        </div>

        <div className="profile-top">
          <div className="brand-mark">
            <span className="brand-dot" /> streamAI
          </div>
        </div>

        <div className="profile-panel">
          <h1 className="profile-heading">{editing ? 'Pick a profile to edit' : 'Choose your profile'}</h1>
          {grid}
          {removedList}
        </div>
        {dialog}
        {removeDialog}
      </div>
    );
  }

  // ---- TV / Desktop: logo top-left, profile picker centered (Netflix) ----
  return (
    <div className="screen profile-screen">
      <div className="profile-backdrop">
        <Poster hueA={220} hueB={285} ratio="16/9" rounded={0} showLabel={false} style={{ height: '100%' }} />
        <div className="profile-backdrop-scrim" />
      </div>

      <div className="profile-topbar">
        <div className="brand-mark">
          <span className="brand-dot" /> streamAI
        </div>
      </div>

      <div className="profile-content">
        <h1 className="profile-heading">{editing ? 'Pick a profile to edit' : "Who's watching?"}</h1>
        {grid}
        {removedList}
        <p className="tv-hint">
          <span className="dpad">◀ ▶</span> Navigate · <span className="ok">OK</span> Select
        </p>
      </div>
      {dialog}
      {removeDialog}
    </div>
  );
}
