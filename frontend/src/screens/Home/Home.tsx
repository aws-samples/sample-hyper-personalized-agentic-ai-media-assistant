import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Play } from 'lucide-react';
import { useDevice } from '../../components/device';
import { Poster } from '../../components/Poster';
import { RailTrack } from '../../components/RailTrack';
import { getHighlights, matchStateConfigured } from '../../lib/sportsStateClient';
import {
  groupHighlightsToClips,
  highlightThumbUrl,
  type HighlightClipGroup,
} from '../../lib/highlightClips';
import { HighlightClipsFeed } from '../Clips/HighlightClipsFeed';
import { MovieRail } from '../../components/MovieRail';
import { SplitMatchArt } from '../../components/SplitMatchArt';
import { LIVE_GAMES } from '../../data/sports';
import { joinOffsetForGame } from '../../lib/sportsClient';
import { NO_PROFILE } from '../../data/profiles';
import { useFollowed } from '../../lib/followed';
import { useSession } from '../../lib/session';
import { useActiveProfile, useHousehold } from '../../lib/household';
import { apiConfigured } from '../../lib/api';
import { useHome } from '../../lib/useHome';
import { useMyList } from '../../lib/myList';
import { LiveRows } from './LiveRows';
import { useContentNav } from '../../lib/contentNav';
import { detailFromMovie } from '../TitleDetail/detail';
import './Home.css';

interface FollowRail {
  key: string;
  name: string;
  groups: HighlightClipGroup[];
  /** Name shown on a card: the followed player in that clip. */
  label: (g: HighlightClipGroup) => string;
}

const avatarGrad = (hue: number) =>
  `linear-gradient(145deg, hsl(${hue} 65% 46%), hsl(${hue + 25} 60% 32%))`;

export function Home() {
  const device = useDevice();
  const navigate = useNavigate();
  const { username, selectProfile, signOut } = useSession();
  const { profiles } = useHousehold();
  // RequireProfile guarantees an active profile; NO_PROFILE only satisfies types.
  const profile = useActiveProfile() ?? NO_PROFILE;
  // Who this profile follows (L3), matched against the live content.
  const followed = useFollowed(profile.id);
  const [menuOpen, setMenuOpen] = useState(false);
  const { openTitle, openGame, openGameWatch } = useContentNav();
  const screenRef = useRef<HTMLDivElement>(null);

  // Movie hero + rails come from GET /home (recommendation cache). A profile
  // that follows someone in a live event gets that event as the hero instead,
  // and the Live Now rail; a profile that follows no one sees neither.
  const useLiveRows = apiConfigured() && Boolean(username);
  const home = useHome(useLiveRows ? profile.id : undefined);
  const carousels = home.rails;
  const carouselsState = home.state;
  const heroGame = followed.liveGames[0];
  // Wait for the follows before showing a movie hero, so it never flips to a live game.
  const heroMovie = heroGame || !followed.ready ? null : home.hero;

  // My List (watch_list) for the active live profile. No-ops for mock personas.
  const myList = useMyList(username ?? undefined, profile.backendProfile);

  // "Because you follow X" -- highlight clips of each followed player from the
  // live match feed; a profile that follows only a team gets that team's
  // highlights instead. Unbounded here (pre-game Home, no spoiler window).
  // Same fetch+group recipe as SportsEntry.
  const [allGroups, setAllGroups] = useState<HighlightClipGroup[]>([]);
  const wantsHighlights = followed.players.length > 0 || followed.teams.some((t) => t.gameId === 'g-soccer');
  useEffect(() => {
    if (!wantsHighlights || !matchStateConfigured()) {
      setAllGroups([]);
      return;
    }
    let active = true;
    getHighlights().then((hl) => active && setAllGroups(groupHighlightsToClips(hl)));
    return () => {
      active = false;
    };
  }, [wantsHighlights]);

  const followRails = useMemo<FollowRail[]>(() => {
    const byPlayer = followed.players
      .map((p) => {
        const names = new Set(p.names.map((n) => n.toLowerCase()));
        const groups = allGroups.filter((g) => g.members.some((m) => m.player && names.has(m.player.toLowerCase())));
        return { key: p.id, name: p.name, groups, label: (g: HighlightClipGroup) =>
          g.members.find((m) => m.player && names.has(m.player.toLowerCase()))?.player ?? p.name };
      })
      .filter((r) => r.groups.length > 0);
    if (byPlayer.length) return byPlayer;
    return followed.teams
      .map((t) => {
        const names = new Set(t.names.map((n) => n.toLowerCase()));
        const groups = allGroups.filter((g) => g.members.some((m) => m.team && names.has(m.team.toLowerCase())));
        return { key: t.id, name: t.name, groups, label: (g: HighlightClipGroup) => g.lead.player ?? t.name };
      })
      .filter((r) => r.groups.length > 0);
  }, [allGroups, followed]);

  // Each rail clip's vertical still (served with the frontend), keyed by clip
  // number so the card can render the frame.
  const thumbs = useMemo(() => {
    const map: Record<number, string> = {};
    for (const g of followRails.flatMap((r) => r.groups)) {
      const url = highlightThumbUrl(g, 'vertical');
      if (url) map[g.n] = url;
    }
    return map;
  }, [followRails]);
  const [watching, setWatching] = useState<{ rail: FollowRail; start: number } | null>(null);

  const pickPersona = (id: string) => {
    selectProfile(id);
    setMenuOpen(false);
    screenRef.current?.scrollTo({ top: 0, behavior: 'auto' });
  };

  const handleSignOut = () => {
    signOut();
    navigate('/login');
  };

  const profileSwitcher = (
    <div className="profile-switch">
      <button
        className={`nav-avatar-trigger ${menuOpen ? 'open' : ''}`}
        onClick={() => setMenuOpen((o) => !o)}
        title="Switch profile"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
      >
        <span className="nav-avatar" style={{ background: avatarGrad(profile.hue) }}>
          {profile.icon}
        </span>
        <span className="nav-caret" aria-hidden="true">▾</span>
      </button>
      {menuOpen && (
        <>
          <div className="switch-backdrop" onClick={() => setMenuOpen(false)} />
          <div className="switch-menu">
            <span className="switch-head">Switch profile</span>
            {profiles.map((p) => (
              <button
                key={p.id}
                className={`switch-item ${p.id === profile.id ? 'on' : ''}`}
                onClick={() => pickPersona(p.id)}
              >
                <span className="switch-avatar" style={{ background: avatarGrad(p.hue) }}>{p.icon}</span>
                <span className="switch-copy">
                  <span className="switch-name">{p.name}</span>
                </span>
                {p.id === profile.id && <span className="switch-check">✓</span>}
              </button>
            ))}
            <button className="switch-signout" onClick={handleSignOut}>Sign out</button>
          </div>
        </>
      )}
    </div>
  );

  const heroInfo = heroMovie ? (
    <div className="hero-info">
      <div className="hero-badges">
        <span className="hero-rank">Top pick for {profile.name}</span>
        <span className="hero-league">
          {[heroMovie.genres.slice(0, 3).join(' · '), heroMovie.year].filter(Boolean).join(' · ')}
        </span>
      </div>
      <h1 className="hero-title">{heroMovie.title}</h1>
      {heroMovie.plot && <p className="hero-synopsis">{heroMovie.plot}</p>}
      <div className="hero-actions">
        <button className="pill-btn">▶ Play</button>
        <button className="pill-btn secondary" onClick={() => openTitle(detailFromMovie(heroMovie))}>
          ⓘ More Info
        </button>
      </div>
    </div>
  ) : heroGame ? (
    <div className="hero-info">
      <div className="hero-badges">
        <span className="badge-live">LIVE</span>
        <span className="hero-league">{heroGame.league}</span>
      </div>
      <h1 className="hero-title">
        {heroGame.title ?? `${heroGame.home.city} vs ${heroGame.away.city}`}
      </h1>
      <div className="score-ticker">
        <span className="st-team">
          <span className="st-dot" style={{ background: `hsl(${heroGame.home.hue} 70% 50%)` }} />
          {heroGame.home.code} <b>{heroGame.home.score}</b>
        </span>
        <span className="st-sep">·</span>
        <span className="st-team">
          <b>{heroGame.away.score}</b> {heroGame.away.code}
          <span className="st-dot" style={{ background: `hsl(${heroGame.away.hue} 70% 50%)` }} />
        </span>
        <span className="st-clock">{heroGame.clock} · {heroGame.status}</span>
      </div>
      <div className="hero-actions">
        <button className="pill-btn" onClick={() => openGameWatch(heroGame, joinOffsetForGame(heroGame))}>
          ▶ Join Live
        </button>
        <button className="pill-btn secondary" onClick={() => openGame(heroGame)}>
          ✦ Catch me up
        </button>
      </div>
    </div>
  ) : null;

  const heroBg = heroMovie?.posterUrl ? (
    <img className="hero-poster" src={heroMovie.posterUrl} alt="" />
  ) : heroGame ? (
    <Poster hueA={heroGame.home.hue} hueB={heroGame.away.hue} ratio="16/9" rounded={0} showLabel={false} style={{ height: '100%' }} />
  ) : null;

  // "Live Now" rail — only for a profile that follows someone in a live event;
  // games with a follow come first. Cards open the SportsEntry (catch-up) flow.
  const liveGames = [...followed.liveGames, ...LIVE_GAMES.filter((g) => !followed.liveGames.includes(g))];
  const liveNowRail = followed.liveGames.length > 0 && (
    <section className="live-now">
      <h2 className="live-now-title">Live Now</h2>
      <div className="live-now-track">
        {liveGames.map((game) => {
          return (
          <button key={game.id} className="live-card" onClick={() => openGame(game)}>
            <div className="live-card-art">
              <SplitMatchArt game={game} />
              <span className="badge-live">LIVE</span>
              <span className="live-card-score">
                {`${game.home.score} – ${game.away.score}`}
              </span>
            </div>
            <span className="live-card-name">
              {game.title ?? `${game.home.city} vs ${game.away.city}`}
            </span>
            <span className="live-card-sub">
              {game.league} · {game.clock}
            </span>
          </button>
          );
        })}
      </div>
    </section>
  );

  // "My List" rail — the profile's saved titles (watch_list). Editable: each
  // card has a ✓ toggle to remove. Only shown for live personas with items.
  const myListRail = useLiveRows && myList.movies.length > 0 && (
    <MovieRail
      title="My List"
      movies={myList.movies}
      resetKey={`mylist:${profile.backendProfile}`}
      onSelect={(m) => openTitle(detailFromMovie(m))}
      onToggleMyList={myList.toggle}
      myListIds={myList.ids}
    />
  );

  // "Because you follow X" rails -- one per followed player with highlights
  // (or per followed team). Clicking a card plays it in the shared clip player
  // (single mode -> end card offers the rest of that rail's clips).
  const followRailsJsx = followRails.map((rail) => (
    <section key={rail.key} className={`rail ${device} follow-hl-rail`}>
      <div className="rail-head">
        <h3 className="rail-title">Because you follow {rail.name}</h3>
      </div>
      <RailTrack resetKey={`follow:${rail.key}:${rail.groups.length}`}>
        {rail.groups.map((g, i) => {
          const isGoal = g.lead.type === 'goal' || g.lead.type === 'penalty';
          const minute = g.lead.minute.replace(/\.$/, '');
          const type = g.lead.type.charAt(0).toUpperCase() + g.lead.type.slice(1);
          const who = rail.label(g);
          return (
            <button key={g.n} className="card portrait follow-hl-card" onClick={() => setWatching({ rail, start: i })}>
              <div className="card-art">
                {thumbs[g.n] ? (
                  <img className="follow-hl-thumb-img" src={thumbs[g.n]} alt={`${who} — ${type} ${minute}'`} loading="lazy" />
                ) : (
                  <Poster hueA={isGoal ? 355 : 210} hueB={20} ratio="2/3" rounded={10} showLabel={false} />
                )}
                <span className={`follow-hl-chip ${isGoal ? 'goal' : ''}`}>{isGoal ? 'GOAL' : type}</span>
                <span className="follow-hl-play">
                  <Play size={16} fill="currentColor" />
                </span>
                <div className="follow-hl-cap">
                  <span className="follow-hl-cap-name">{who}</span>
                  <span className="follow-hl-cap-sub">{minute}' · {type}</span>
                </div>
              </div>
            </button>
          );
        })}
      </RailTrack>
    </section>
  ));

  const followWatch = watching && (
    <HighlightClipsFeed
      groups={watching.rail.groups}
      startIndex={watching.start}
      tag={`Because you follow ${watching.rail.name}`}
      onClose={() => setWatching(null)}
    />
  );

  // ------- TV / DESKTOP -------
  if (device === 'tv') {
    return (
      <div className="screen home-screen tv" ref={screenRef}>
        <nav className="topnav">
          <div className="topnav-left">
            <span className="brand-mark"><span className="brand-dot" /> streamAI</span>
            <a className="nav-tab active">Home</a>
            <a className="nav-tab">New &amp; Hot</a>
            <a className="nav-tab">Sports</a>
            <a className="nav-tab" onClick={() => navigate('/clips')}>Clips</a>
          </div>
          <div className="topnav-right">
            <button className="nav-icon" onClick={() => navigate('/search')} title="AI Search" aria-label="AI Search">
              <svg width="22" height="22" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12.332 6.88789C12.36 6.95796 12.43 7 12.5 7C12.57 7 12.668 6.88789 12.668 6.88789L13.592 4.58959L15.888 3.66466C15.9212 3.65083 15.9495 3.62748 15.9694 3.59755C15.9894 3.56763 16 3.53246 16 3.4965C16 3.42643 15.888 3.32833 15.888 3.32833L13.592 2.4034L12.668 0.105105C12.612 -0.035035 12.388 -0.035035 12.332 0.105105L11.408 2.4034L9.112 3.32833C9.07883 3.34216 9.0505 3.36551 9.03057 3.39544C9.01064 3.42537 9 3.46053 9 3.4965C9 3.56657 9.112 3.66466 9.112 3.66466L11.408 4.58959L12.332 6.88789Z" fill="currentColor" stroke="none" />
                <path d="M11 11.0049L15 15.0049" />
                <path d="M11.9008 8C11.439 10.2838 9.41929 12.0049 7 12.0049C4.24 12.0049 2 9.76488 2 7.00488C2 5.58488 2.59 4.30488 3.54 3.39488C4.44 2.53488 5.66 2.00488 7 2.00488C7.34177 2.00488 7.67659 2.03964 8 2.10581" />
              </svg>
            </button>
            {profileSwitcher}
          </div>
        </nav>

        {heroInfo && (
          <header className="hero tv">
            <div className="hero-bg">
              {heroBg}
              <div className="hero-scrim tv" />
            </div>
            {heroInfo}
          </header>
        )}

        <div className="home-rows">
          {myListRail}
          {liveNowRail}
          {followRailsJsx}
          {useLiveRows && (
            <LiveRows
              carousels={carousels}
              state={carouselsState}
              profileSlug={profile.backendProfile}
              onSelect={(m) => openTitle(detailFromMovie(m))}
              onToggleMyList={myList.toggle}
              myListIds={myList.ids}
            />
          )}
        </div>
        {followWatch}
      </div>
    );
  }

  // ------- MOBILE -------
  return (
    <div className="screen home-screen mobile" ref={screenRef}>
      <div className="mobile-topbar">
        <span className="brand-mark"><span className="brand-dot" /> streamAI</span>
        <div className="mobile-top-right">
          {profileSwitcher}
        </div>
      </div>
      <div className="filter-row">
        {['Shows', 'Films', 'Live', 'Sports', 'Games'].map((f, i) => (
          <span key={f} className={`chip ${i === 2 ? 'chip-active' : ''}`}>{f}</span>
        ))}
      </div>

      {heroInfo && (
        <header className="hero mobile">
          <div className="hero-bg">
            {heroBg}
            <div className="hero-scrim mobile" />
          </div>
          {heroInfo}
        </header>
      )}

      <div className="home-rows">
        {myListRail}
        {liveNowRail}
        {followRailsJsx}
        {useLiveRows && (
          <LiveRows
            carousels={carousels}
            state={carouselsState}
            profileSlug={profile.backendProfile}
            onSelect={(m) => openTitle(detailFromMovie(m))}
            onToggleMyList={myList.toggle}
            myListIds={myList.ids}
          />
        )}
      </div>
      {followWatch}

      <nav className="bottom-nav">
        {[
          { i: '⌂', l: 'Home', on: true },
          { i: '⚡', l: 'Clips', to: '/clips' },
          { i: '🔍', l: 'Search', to: '/search' },
          { i: profile.icon, l: 'Profile' },
        ].map((t) => (
          <button
            key={t.l}
            className={`bn-item ${t.on ? 'on' : ''}`}
            onClick={t.to ? () => navigate(t.to) : undefined}
          >
            <span className="bn-icon">{t.i}</span>
            <span className="bn-label">{t.l}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}
