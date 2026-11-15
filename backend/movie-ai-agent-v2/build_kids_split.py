"""Offline: split the movie catalog into kids vs non-kids CSVs by MPAA rating.

Reads the repository catalog (data/catalog/*.json), the same files
infra/deployment/load_catalog.py indexes, so no search collection is needed.

Single source of truth for the kids boundary is KIDS_MPAA_RATINGS below — the
same ratings the recommendation agent's prompts filter on. A title is
classified kids if ANY of its mpaa_ratings is in the allowlist (matching the
agent's should/minimum_should_match:1 behavior). Titles with no rating, or only
non-kids ratings, go to non-kids (safe default).

Micro-dramas (title_type "micro-drama") are not sharded: the agent finds them
through catch-all search on the catalog index.

Usage (from this directory):
    python build_kids_split.py && python build_shards.py
"""

import csv
import json
from pathlib import Path

# --- Single source of truth: the kids-content MPAA allowlist ---
# Mirrors the recommendation agent's prompt filter. Keep this list authoritative in code.
KIDS_MPAA_RATINGS = ("g", "tv-y", "tv-y7", "tv-g")  # TV-PG is parental guidance, not kids

CATALOG_DIR = Path(__file__).resolve().parents[2] / "data" / "catalog"
# Title types served by search only, never written to the shard CSVs.
UNSHARDED_TYPES = {"micro-drama"}

FIELDS = ["title_id", "originalTitle", "title_type", "genres", "imdb_rating",
          "imdb_url", "mpaa_ratings", "plot", "premiere_date", "premiere_year",
          "principalCastMembers", "principalCrewMembers", "awards"]

# Same genre normalisation as infra/deployment/load_catalog.py: the v1 synthetic
# catalog spells science fiction "scifi", the agent's vocabulary "sci-fi".
GENRE_ALIASES = {"scifi": "sci-fi"}

KIDS_CSV = "movie_index_kids.csv"
NON_KIDS_CSV = "movie_index_non_kids.csv"


def load_catalog(directory: Path = CATALOG_DIR) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        rows.extend(data if isinstance(data, list) else [data])
    return sorted(rows, key=lambda r: r["title_id"])


def ratings_of(src: dict) -> list[str]:
    r = src.get("mpaa_ratings") or []
    if isinstance(r, str):
        r = [r]
    return [str(x).strip().lower() for x in r if str(x).strip()]


def is_kids(src: dict) -> bool:
    return any(r in KIDS_MPAA_RATINGS for r in ratings_of(src))


def normalise(src: dict) -> dict:
    genres = [GENRE_ALIASES.get(str(g).lower(), str(g).lower()) for g in src.get("genres") or []]
    return {**src, "genres": list(dict.fromkeys(genres))}


def fmt(v) -> str:
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    return "" if v is None else str(v)


def split(rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """(kids, non_kids, mixed) for the shardable rows."""
    kids, non_kids, mixed = [], [], []
    for src in rows:
        if src.get("title_type") in UNSHARDED_TYPES:
            continue
        rs = ratings_of(src)
        if any(r in KIDS_MPAA_RATINGS for r in rs):
            kids.append(src)
            if any(r not in KIDS_MPAA_RATINGS for r in rs):  # e.g. rated both g AND pg
                mixed.append(src)
        else:
            non_kids.append(src)
    return kids, non_kids, mixed


def main() -> None:
    hits = [normalise(r) for r in load_catalog()]
    kids, non_kids, mixed = split(hits)

    for path, rows in ((KIDS_CSV, kids), (NON_KIDS_CSV, non_kids)):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, quoting=csv.QUOTE_ALL)
            w.writeheader()
            for src in rows:
                w.writerow({k: fmt(src.get(k)) for k in FIELDS})

    skipped = len(hits) - len(kids) - len(non_kids)
    print(f"catalog total: {len(hits)}  (not sharded: {skipped} {sorted(UNSHARDED_TYPES)})")
    print(f"kids allowlist: {KIDS_MPAA_RATINGS}")
    print(f"  -> {KIDS_CSV}: {len(kids)} titles")
    print(f"  -> {NON_KIDS_CSV}: {len(non_kids)} titles")
    if mixed:
        print(f"\n  NOTE: {len(mixed)} title(s) have BOTH a kids and a non-kids rating "
              f"(classified kids by ANY-match). Review for a safety split:")
        for src in mixed[:20]:
            print(f"    - {src.get('originalTitle')} [{src.get('title_id')}] ratings={ratings_of(src)}")


if __name__ == "__main__":
    main()
