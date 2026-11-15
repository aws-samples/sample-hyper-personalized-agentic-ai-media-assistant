"""Shared routing vocabulary: catalog rating/type lists, the genre vocabulary,
and the hard-filter keys that decide the path in understand.py.

HARD filters (person / year / rating / kids / awards / exact title /
genre-exclude) send a request to "catch_all" (OpenSearch filter + hybrid KNN);
a pure mood/genre request goes to "fanout"; an open request goes to "taste".
"""

from __future__ import annotations

# --- Catalog vocabulary (shared with catchall.build_filters) ----------------

# Kids MPAA allowlist — single source of truth (mirrors build_kids_split.py).
KIDS_MPAA_RATINGS = ("g", "tv-y", "tv-y7", "tv-g")  # TV-PG is parental guidance, not kids
TV_TYPES = ["tvSeries", "tvEpisode", "tvMiniSeries", "tvSpecial"]

# Controlled genre vocabulary (the actual tokens in the 'genres' field).
GENRE_VOCAB = {
    "drama", "thriller", "action", "comedy", "adventure", "crime", "sci-fi",
    "fantasy", "mystery", "animation", "romance", "horror", "biography",
    "family", "history", "war", "sport", "music", "musical", "short",
    "western", "documentary", "talk-show", "reality-tv", "news", "game-show",
}
GENRE_SYNONYMS = {
    "science fiction": "sci-fi", "scifi": "sci-fi", "sci fi": "sci-fi",
    "rom-com": "romance", "romcom": "romance", "biopic": "biography",
    "docuseries": "documentary", "documentaries": "documentary",
    "kids": "family", "children": "family", "scary": "horror",
}


def normalize_genres(vals) -> list[str]:
    """Map synonyms to catalog tokens and drop anything not in the vocabulary,
    so an unknown genre becomes no clause rather than a zero-match filter."""
    out: list[str] = []
    for v in vals or []:
        g = GENRE_SYNONYMS.get(str(v).strip().lower(), str(v).strip().lower())
        if g in GENRE_VOCAB and g not in out:
            out.append(g)
    return out


# HARD filters decide the path. genres_include + keywords are SOFT.
FILTER_KEYS = (
    "person", "genres_exclude", "min_year", "max_year",
    "min_rating", "kids", "awards", "exact_title",
)
