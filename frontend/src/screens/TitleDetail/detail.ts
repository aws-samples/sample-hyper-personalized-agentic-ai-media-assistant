import type { Movie } from '../../lib/api';
import type { Title } from '../../data/types';

/**
 * Normalized shape the detail overlay renders. Kept independent of any single
 * source so it can be fed from live API movies, curated hero titles, or the
 * mock catalog via the small adapters below.
 */
export interface DetailItem {
  id?: string;
  title: string;
  posterUrl?: string | null;
  plot?: string | null;
  genres: string[];
  year?: number | null;
  rating?: number | null;
  maturity?: string | null;
  cast?: string[];
  /** One-line GenAI "why you'll love this" reason. */
  why?: string | null;
  /** Gradient fallback hues when there's no poster image. */
  hueA?: number;
  hueB?: number;
  /** Presigned S3 URL for playback, when the title has a real video. */
  contentUrl?: string | null;
}

/** Adapt a live API movie (carousel card) into a DetailItem. */
export function detailFromMovie(m: Movie): DetailItem {
  return {
    id: m.id,
    title: m.title,
    posterUrl: m.posterUrl,
    plot: m.plot,
    genres: m.genres ?? [],
    year: m.year,
    rating: m.rating,
    maturity: m.maturity,
    cast: m.cast ?? [],
    why: m.blurb ?? null,
    contentUrl: m.contentUrl ?? null,
  };
}

/** Adapt a DetailItem back into a Movie (for optimistic My List inserts). */
export function movieFromDetail(item: DetailItem): Movie {
  return {
    id: item.id ?? '',
    title: item.title,
    posterUrl: item.posterUrl ?? null,
    rating: item.rating ?? null,
    genres: item.genres ?? [],
    plot: item.plot ?? null,
    cast: item.cast ?? [],
    maturity: item.maturity ?? null,
    year: item.year ?? null,
    blurb: item.why ?? undefined,
    contentUrl: item.contentUrl ?? null,
  };
}

/** Adapt a mock catalog title into a DetailItem (gradient poster fallback). */
export function detailFromTitle(t: Title): DetailItem {
  return {
    id: t.id,
    title: t.name,
    posterUrl: t.poster ?? null,
    plot: t.synopsis,
    genres: t.genres ?? [],
    year: t.year,
    rating: t.imdb,
    maturity: t.maturity,
    cast: t.cast ?? [],
    why: t.whyYoullLove,
    hueA: t.hueA,
    hueB: t.hueB,
  };
}
