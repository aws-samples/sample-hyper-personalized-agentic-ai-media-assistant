import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useLocation, useNavigate, type Location } from 'react-router-dom';
import { Volume2, VolumeX } from 'lucide-react';
import { useDevice } from '../../components/device';
import { MovieRail } from '../../components/MovieRail';
import { StarRating } from '../../components/StarRating';
import { VOD_TITLES } from '../../data/titles';
import { useSession } from '../../lib/session';
import { useActiveProfile } from '../../lib/household';
import { useMyList } from '../../lib/myList';
import { track } from '../../lib/events';
import { hasVodHighlight, vodHighlightUrl } from '../../lib/vodHighlights';
import { ATTRIBUTIONS } from '../../data/attributions';
import { type DetailItem, movieFromDetail, detailFromTitle, detailFromMovie } from './detail';
import './TitleDetail.css';

interface TitleDetailProps {
  item: DetailItem;
  onClose: () => void;
  /** Start playback (routes to /watch/:id). */
  onPlay: () => void;
}

/** Up to `n` catalog titles most similar to the item, ranked by how many
 *  genres they share (then by match %), so the picks actually make sense. */
function moreLikeThis(item: DetailItem, n = 8) {
  const genres = new Set(item.genres.map((g) => g.toLowerCase()));
  return VOD_TITLES.filter((t) => t.id !== item.id && t.name !== item.title)
    .map((t) => ({
      t,
      overlap: t.genres.filter((g) => genres.has(g.toLowerCase())).length,
    }))
    .filter((x) => x.overlap > 0)
    .sort((a, b) => b.overlap - a.overlap || b.t.match - a.t.match)
    .slice(0, n)
    .map((x) => x.t);
}

export function TitleDetail({ item, onClose, onPlay }: TitleDetailProps) {
  const device = useDevice();
  const navigate = useNavigate();
  const location = useLocation();
  const background = (location.state as { background?: Location } | null)?.background;

  // The current title is whatever the route resolved; "More like this" swaps it
  // by navigating to /title/:id (a new URL), so the view stays in sync.
  const active = item;
  const scrollRef = useRef<HTMLDivElement>(null);

  // My List toggle for the active live profile. Only offered when the item has
  // a backend id and the profile has a backend list (otherwise the button is
  // hidden — mock/hero items aren't persistable).
  const { username, profileId } = useSession();
  const activeProfile = useActiveProfile();
  const backendProfile = activeProfile?.backendProfile;
  const myList = useMyList(username ?? undefined, backendProfile);
  const canList = Boolean(active.id && username && backendProfile);
  const inList = active.id ? myList.inList(active.id) : false;

  // Like: an explicit taste signal for the title (feedback `like`). It is sent
  // once; there is no "unlike" signal, so the button stays on for this title.
  const [likedId, setLikedId] = useState<string | null>(null);
  const liked = Boolean(active.id) && likedId === active.id;
  const like = () => {
    if (!active.id || liked) return;
    track('feedback', 'title_detail', { item_id: active.id, value: 'like' });
    setLikedId(active.id);
  };

  // Mark as watched: the viewer says they have seen this title. Scored like a
  // finished view (and excluded from recommendations); sent once per title.
  const [watchedId, setWatchedId] = useState<string | null>(null);
  const watched = Boolean(active.id) && watchedId === active.id;
  const markWatched = () => {
    if (!active.id || watched) return;
    track('mark_watched', 'title_detail', { item_id: active.id });
    setWatchedId(active.id);
  };

  // Highlight reel that autoplays in the hero for titles that have one
  // (lib/vodHighlights.ts; none ship with the demo). The API picks the clip whose genre description is
  // closest to this profile's taste. Horizontal cut; started with sound
  // (the modal opened on a click), falling back to muted if the browser blocks.
  const heroVideoRef = useRef<HTMLVideoElement>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewMuted, setPreviewMuted] = useState(false);

  useEffect(() => {
    let live = true;
    setPreviewUrl(null);
    if (active.id && hasVodHighlight(active.id, profileId)) {
      vodHighlightUrl(active.id, profileId, 'horizontal').then((u) => live && setPreviewUrl(u));
    }
    return () => {
      live = false;
    };
  }, [active.id, profileId]);

  useEffect(() => {
    const v = heroVideoRef.current;
    if (!v || !previewUrl) return;
    v.muted = false;
    setPreviewMuted(false);
    v.play().catch(() => {
      v.muted = true;
      setPreviewMuted(true);
      v.play().catch(() => {});
    });
  }, [previewUrl]);

  const togglePreviewMute = () => {
    const v = heroVideoRef.current;
    const next = !previewMuted;
    setPreviewMuted(next);
    if (v) {
      v.muted = next;
      if (!next) v.play().catch(() => {});
    }
  };

  // Reset scroll to the top whenever a different title is shown.
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0, behavior: 'auto' });
  }, [item]);

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const gradient =
    active.hueA != null && active.hueB != null
      ? `radial-gradient(120% 120% at 20% 10%, hsl(${active.hueA} 70% 42%) 0%, hsl(${active.hueA} 65% 22%) 42%, hsl(${active.hueB} 60% 14%) 100%)`
      : 'linear-gradient(160deg, #1c2230, #0b0e14)';

  // ReactNode rather than string: the rating entry is the StarRating element
  // component instead of a plain star-and-score string.
  const metaBits = [
    active.year ? String(active.year) : null,
    active.rating ? <StarRating value={active.rating} showScale /> : null,
    active.maturity ? active.maturity.toUpperCase() : null,
    active.genres.slice(0, 3).join(' · ') || null,
  ].filter(Boolean) as ReactNode[];

  // "More like this" as real Movie cards (same presentation as Home/Search):
  // poster image + title + star rating + genre, via the shared MovieRail.
  const relatedMovies = moreLikeThis(active).map((t) => movieFromDetail(detailFromTitle(t)));

  return (
    <div className={`title-detail ${device}`} role="dialog" aria-modal="true">
      <div className="td-dim" onClick={onClose} />

      <div className="td-panel" ref={scrollRef}>
        <div className="td-hero">
          {previewUrl ? (
            <video
              ref={heroVideoRef}
              className="td-hero-img td-hero-video"
              src={previewUrl}
              autoPlay
              loop
              playsInline
              muted={previewMuted}
            />
          ) : active.posterUrl ? (
            <img className="td-hero-img" src={active.posterUrl} alt="" />
          ) : (
            <div className="td-hero-img" style={{ background: gradient }} />
          )}
          <div className="td-hero-scrim" />
          <button className="td-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
          {previewUrl && (
            <button
              className="td-hero-mute"
              onClick={togglePreviewMute}
              aria-label={previewMuted ? 'Unmute' : 'Mute'}
              title={previewMuted ? 'Unmute' : 'Mute'}
            >
              {previewMuted ? <VolumeX size={18} /> : <Volume2 size={18} />}
            </button>
          )}
          <div className="td-hero-title">
            <h1>{active.title}</h1>
          </div>
        </div>

        <div className="td-body">
          {metaBits.length > 0 && (
            <div className="td-meta">
              {metaBits.map((m, i) => (
                <span key={i}>{m}</span>
              ))}
            </div>
          )}

          <div className="td-actions">
            <button className="td-btn" onClick={onPlay}>
              ▶ Play
            </button>
            {canList && (
              <button
                className={`td-btn secondary ${inList ? 'on' : ''}`}
                aria-pressed={inList}
                onClick={() => myList.toggle(movieFromDetail(active))}
              >
                {inList ? '✓ My List' : '＋ My List'}
              </button>
            )}
            {canList && (
              <button
                className={`td-btn secondary ${liked ? 'on' : ''}`}
                aria-pressed={liked}
                onClick={like}
              >
                {liked ? '♥ Liked' : '♡ Like'}
              </button>
            )}
            {canList && (
              <button
                className={`td-btn secondary ${watched ? 'on' : ''}`}
                aria-pressed={watched}
                onClick={markWatched}
              >
                {watched ? '✓ Watched' : '◉ Mark as watched'}
              </button>
            )}
          </div>

          {active.why && (
            <div className="td-why">
              <span className="ai-tag">✦ Why you'll love this</span>
              <p>{active.why}</p>
            </div>
          )}

          {active.plot && <p className="td-plot">{active.plot}</p>}

          {active.cast && active.cast.length > 0 && (
            <p className="td-cast">
              <span>Cast</span> {active.cast.slice(0, 6).join(', ')}
            </p>
          )}

          {active.id && ATTRIBUTIONS[active.id] && (
            <p className="td-cast td-credit">
              <span>Credit</span> {ATTRIBUTIONS[active.id].text} ·{' '}
              <a href={ATTRIBUTIONS[active.id].licenseUrl} target="_blank" rel="noopener noreferrer">
                {ATTRIBUTIONS[active.id].license}
              </a>
            </p>
          )}

          {relatedMovies.length > 0 && (
            <div className="td-more">
              <MovieRail
                title="More like this"
                movies={relatedMovies}
                resetKey={relatedMovies.map((m) => m.id).join(',')}
                onSelect={(m) =>
                  navigate(`/title/${m.id}`, { state: { background, item: detailFromMovie(m) } })
                }
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
