"""Write Amazon Connect Predictive Insights recommendations into Home rail rows.

    .venv/bin/python infra/deployment/connect_rails.py --profiles profiles.json            # dry run
    .venv/bin/python infra/deployment/connect_rails.py --profiles profiles.json --apply    # writes
    .venv/bin/python infra/deployment/connect_rails.py --profiles profiles.json --clear --apply

The Connect counterpart of personalize_rails.py. Predictive Insights has no
batch job, so this calls GetProfileRecommendations live for each profile and
writes the result to the recommendation-cache table under CONN# keys only
(household_core.CONN_PREFIX); seed, cold-start and P13N# rows are never
touched. GET /home serves them only when the household Lambda runs with
HOME_RAILS_SOURCE=connect (config key `homeRailsSource`).

Rails, per profile (same shape and de-duplication as personalize_rails.build_rows):
  hero + Top Picks           --recommender, with the --exclude-filter (watched titles)
  Because You Like <genre>   the --genre-filter first; if the service refuses it
                             (InternalServerException in Preview), a CandidateIds
                             re-rank of the genre's 50 best-rated unwatched titles
                             (Connect ignores filters on a CandidateIds request, so
                             the entry's `watched` list is removed client-side)
  Because You Watched        not served: the API has no field for the anchor title

--profiles maps any key to a household profile and its Connect ProfileId:
  {"persona-action-fan": {"profile_id": "p-...", "connect_profile_id": "<32 hex>",
                          "genres": ["action", "sci-fi"], "watched": ["tt..."]}}
Entries without connect_profile_id are skipped.
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra/lambdas/household"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import household_core as core  # noqa: E402
import personalize_rails as pr  # noqa: E402

PREFIX = core.CONN_PREFIX
SOURCE = core.CONNECT_SOURCE
GENRE_KEY = "conn_genre_"
FETCH_N = 40          # Top Picks request size: hero + TOP_N with room for de-duplication
CANDIDATES_N = 50     # GetProfileRecommendations CandidateIds maximum
EXCLUDED_TYPES = {"videoGame", "tvEpisode"}   # not in the Connect catalog (generate_dataset.py)


# --- pure logic ------------------------------------------------------------

def catalog_genres(paths=pr.CATALOG_CSVS) -> dict:
    """title_id -> (lower-case genres, imdb rating) for recommendable titles."""
    out = {}
    for path in paths:
        if not Path(path).exists():
            continue
        with Path(path).open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                genres = [g.strip().lower() for g in (r.get("genres") or "").split(";") if g.strip()]
                if r.get("title_type") in EXCLUDED_TYPES or not genres:
                    continue
                try:
                    rating = float(r.get("imdb_rating") or 0)
                except ValueError:
                    rating = 0.0
                out[r["title_id"]] = (genres, rating)
    return out


def genre_candidates(catalog: dict, genre: str, exclude=(), n: int = CANDIDATES_N) -> list:
    """The genre's best-rated titles not in `exclude`, for a CandidateIds re-rank."""
    skip = set(exclude)
    pool = [(rating, tid) for tid, (genres, rating) in catalog.items() if genre in genres and tid not in skip]
    return [tid for _, tid in sorted(pool, key=lambda p: (-p[0], p[1]))[:n]]


def profile_rows(entry: dict, fetch, catalog: dict, now: datetime) -> tuple[list, dict]:
    """CONN# rows for one profile plus a note of how each genre rail was filled.

    fetch(n, filters=None, candidates=None) -> ranked item ids; it raises on
    a service error.
    """
    notes = {}
    top = fetch(FETCH_N, filters=[{"Name": "exclude-watched"}])
    used = top[:1 + pr.TOP_N]
    genres = []
    for genre in entry.get("genres", []):
        try:
            items = fetch(pr.GENRE_N + len(used), filters=[{"Name": "genre", "Values": {"GENRE": genre}}])
            notes[genre] = "service filter"
        except Exception as e:  # noqa: BLE001 -- Preview genre filter fails server-side
            # Connect ignores RecommenderFilters when CandidateIds is set, so watched
            # titles are removed from the candidates here instead.
            cands = genre_candidates(catalog, genre, exclude=[*used, *entry.get("watched", [])])
            items = fetch(pr.GENRE_N, candidates=cands) if cands else []
            notes[genre] = f"CandidateIds re-rank ({type(e).__name__} on the genre filter)"
        genres.append((genre, items))
    rows = pr.build_rows(entry["profile_id"], top, genres, None, None, now,
                         prefix=PREFIX, source=SOURCE, genre_key=GENRE_KEY)
    return rows, notes


def mapped_entries(mapping: dict) -> list:
    out = []
    for key, entry in mapping.items():
        if not entry.get("connect_profile_id"):
            continue
        if not core.PROFILE_ID_RE.match(entry.get("profile_id", "")):
            raise ValueError(f"{key}: profile_id must look like p-<32 hex>")
        out.append(entry)
    return out


# --- I/O -----------------------------------------------------------------------

def connect_fetcher(cp, domain, recommender, filter_names, connect_profile_id):
    """fetch() for profile_rows: logical filter names -> the domain's filter names."""
    def fetch(n, filters=None, candidates=None):
        kw = dict(DomainName=domain, ProfileId=connect_profile_id, RecommenderName=recommender, MaxResults=n)
        if filters:
            kw["RecommenderFilters"] = [{**f, "Name": filter_names[f["Name"]]} for f in filters]
        if candidates:
            kw["CandidateIds"] = candidates
        resp = cp.get_profile_recommendations(**kw)
        return [r["CatalogItem"]["Id"] for r in resp.get("Recommendations", [])]
    return fetch


def main():
    from config import DEFAULT_CONFIG, load_config
    from deploy import aws_environment

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--profiles", required=True,
                        help="JSON map of key -> {profile_id, connect_profile_id, genres}")
    parser.add_argument("--domain", default="hpm-recsys-exp", help="Customer Profiles domain")
    parser.add_argument("--recommender", default="hpm-rfy2", help="recommended-for-you recommender")
    parser.add_argument("--exclude-filter", default="hpm-exclude-watched")
    parser.add_argument("--genre-filter", default="hpm-genre", help="filter with a $GENRE placeholder")
    parser.add_argument("--clear", action="store_true", help="delete the mapped profiles' CONN# rows instead")
    parser.add_argument("--apply", action="store_true", help="write to DynamoDB (default: dry run)")
    args = parser.parse_args()
    cfg = load_config(args.config)
    os.environ.update(aws_environment(cfg["region"]))
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    assert session.client("sts").get_caller_identity()["Account"] == cfg["account"]
    table = session.resource("dynamodb").Table(f'{cfg["prefix"]}-recommendation-cache')
    entries = mapped_entries(json.loads(Path(args.profiles).read_text()))
    suffix = "" if args.apply else " (dry run)"

    if args.clear:
        for pid in dict.fromkeys(e["profile_id"] for e in entries):
            sks = pr.existing_p13n(table, pid, PREFIX)
            print(f"{pid}: delete {len(sks)} CONN# rows{suffix}")
            if args.apply and sks:
                pr.write_profile(table, pid, [], sks, PREFIX)
        return

    # Catalog CreatedAt was loaded in ms and botocore parses it as seconds; keep timestamps raw.
    session._session.get_component("response_parser_factory").set_parser_defaults(timestamp_parser=lambda x: x)
    cp = session.client("customer-profiles")
    names = {"exclude-watched": args.exclude_filter, "genre": args.genre_filter}
    catalog = catalog_genres()
    now = datetime.now(timezone.utc)
    for entry in entries:
        pid = entry["profile_id"]
        fetch = connect_fetcher(cp, args.domain, args.recommender, names, entry["connect_profile_id"])
        rows, notes = profile_rows(entry, fetch, catalog, now)
        deletes = pr.stale_keys(pr.existing_p13n(table, pid, PREFIX), rows, PREFIX)
        pr.check_keys(rows, deletes, PREFIX)
        print(f"{pid}: {len(rows)} rows, {len(deletes)} stale{suffix}")
        for genre, how in notes.items():
            print(f"  genre {genre}: {how}")
        for r in rows:
            label = r.get("display_title") or "hero"
            ids = [i["title_id"] for i in r.get("items", [])] or [r.get("title_id")]
            print(f"  {r['sk']:<45} {label}: {len(ids)} -> {', '.join(ids[:5])}{' ...' if len(ids) > 5 else ''}")
        if args.apply:
            pr.write_profile(table, pid, rows, deletes, PREFIX)
    print("Written." if args.apply else "Dry run only; pass --apply to write.")


if __name__ == "__main__":
    main()
