"""Shared catalog data loaded once at import.

- SHARD_TEXT:   shard id -> cached system prompt (the fan-out payload)
- BY_CATEGORY:  category ("kids"|"movie"|"tv") -> [shard ids]
- META:         title_id -> {title, year, imdb, awards_n}

META powers the candidate line ("Title [id] | year | rating X | awards N |
genres: ... | plot") that both retrieval paths hand to the reduce step. genres
and plot are the reranker's relevance signal; awards_n is the acclaim tiebreaker
(the index has no vote-count field), derived from the "; "-joined awards column.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from config import SHARD_DIR

_ROOT = Path(__file__).parent

# --- Shards -----------------------------------------------------------------

_manifest = json.loads((SHARD_DIR / "manifest.json").read_text())

SHARD_TEXT: dict[str, str] = {}
BY_CATEGORY: dict[str, list[str]] = {}
# shard id -> {title_id: title_type}: what each shard may return. Fan-out keeps
# only ids from the shards it asked (a model can invent an id), and the kids
# shard -- which mixes movies and series -- is narrowed to the requested format.
SHARD_TITLES: dict[str, dict[str, str]] = {}
for _s in _manifest["shards"]:
    _txt_path = SHARD_DIR / Path(_s["system_txt"]).name
    SHARD_TEXT[_s["shard"]] = _txt_path.read_text(encoding="utf-8")
    BY_CATEGORY.setdefault(_s["category"], []).append(_s["shard"])
    _csv_path = SHARD_DIR / Path(_s["csv"]).name
    if _csv_path.exists():
        with open(_csv_path, encoding="utf-8") as _f:
            SHARD_TITLES[_s["shard"]] = {_r["title_id"]: _r.get("title_type", "")
                                         for _r in csv.DictReader(_f)}

# --- Title metadata ---------------------------------------------------------

META: dict[str, dict] = {}
for _fname in ("movie_index_kids.csv", "movie_index_non_kids.csv"):
    _path = _ROOT / _fname
    if not _path.exists():
        continue
    for _r in csv.DictReader(open(_path, encoding="utf-8")):
        _aw = _r.get("awards", "") or ""
        META[_r["title_id"]] = {
            "title": _r.get("originalTitle", ""),
            "year": _r.get("premiere_year", ""),
            "imdb": _r.get("imdb_rating", ""),
            "awards_n": len([a for a in _aw.split("; ") if a.strip()]),
        }


PLOT_MAX_CHARS = 200  # keep the reduce prompt lean across ~35 candidates


def candidate_line(tid: str, title: str = "", year: str = "", imdb: str = "",
                   genres=None, plot: str = "") -> str:
    """Format one candidate line for the reduce step, enriching from META.

    genres/plot are the reranker's RELEVANCE signal (imdb/awards are only acclaim
    tiebreakers). They come from the retrieval hit (OpenSearch DISPLAY_FIELDS
    carry both); when absent the line degrades gracefully to the acclaim-only
    form. plot is truncated to keep the prompt small across many candidates.
    """
    m = META.get(tid, {})
    title = m.get("title") or title or tid
    year = year or m.get("year", "")
    imdb = imdb or m.get("imdb", "")
    awards_n = m.get("awards_n", 0)
    line = f"{title} [{tid}] | {year} | rating {imdb} | awards {awards_n}"

    if isinstance(genres, (list, tuple)):
        genres = ", ".join(str(g) for g in genres if g)
    genres = (genres or "").strip()
    if genres:
        line += f" | genres: {genres}"

    plot = (plot or "").strip().replace("\n", " ")
    if plot:
        if len(plot) > PLOT_MAX_CHARS:
            plot = plot[:PLOT_MAX_CHARS - 1].rstrip() + "…"
        line += f" | {plot}"
    return line


MOVIE_TYPES = {"movie", "tvMovie", "video", "short"}
TV_TYPES = {"tvSeries", "tvEpisode", "tvMiniSeries", "tvSpecial"}


def select_shards(category: str, kids: bool = False) -> list[str]:
    """Shards to fan out over for a category ("both" = movie + tv).

    A kids request reads only the kids shard, whatever the format: every title
    in it passed the kids rating list offline, and no other shard is safe."""
    if kids:
        return BY_CATEGORY.get("kids", [])
    if category == "both":
        return BY_CATEGORY.get("movie", []) + BY_CATEGORY.get("tv", [])
    return BY_CATEGORY.get(category, BY_CATEGORY.get("movie", []))
