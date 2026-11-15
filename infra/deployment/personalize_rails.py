"""Write Amazon Personalize batch-inference output into Home rail rows.

    .venv/bin/python infra/deployment/personalize_rails.py \
        --top s3://<bucket>/batch-out/upv2_top/upv2_top.jsonl.out \
        --genre s3://<bucket>/batch-out/upv2_genre/upv2_genre.jsonl.out \
        --sims s3://<bucket>/batch-out/sims/sims.jsonl.out \
        --profiles profiles.json            # dry run: prints the rows
        [--apply]                           # writes them
    .venv/bin/python infra/deployment/personalize_rails.py --profiles profiles.json --clear --apply

Rows go to the recommendation-cache table under P13N# keys only
(household_core.P13N_PREFIX), so seed and cold-start rows are never touched.
GET /home serves them only when the household Lambda runs with
HOME_RAILS_SOURCE=personalize (config key `homeRailsSource`); `seed` brings
the old Home back, and --clear removes the rows themselves.

Inputs, each a local path or s3:// URI, one JSON object per line, as
Personalize batch jobs write them:
  --top    User-Personalization-v2, {"input": {"userId"}}             -> hero + Top Picks
  --genre  the same with a genre filter, {"input": {"userId", "filterValues": {"GENRE"}}}
                                                                    -> Because You Like <genre>
  --sims   Similar-Items, {"input": {"itemId"}}                        -> Because You Watched <anchor>

--profiles maps batch user ids to household profiles and names each
profile's Because-You-Watched anchor (the itemId its --sims request used):
  {"persona-action-fan": {"profile_id": "p-...", "anchor": "tt1001001",
                          "anchor_title": "Quantum Storm"}}
anchor_title is optional (looked up in the committed catalog CSVs). A batch
user id that is itself a profile id needs no entry unless it has an anchor.
Every other batch user (synthetic training users) is ignored.
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
import household_core as core  # noqa: E402

TOP_N = 20          # Top Picks length, after the hero
BYW_N = 8           # Because You Watched length
GENRE_N = 10        # Because You Like <genre> length
MAX_GENRE_RAILS = 2
CATALOG_CSVS = [ROOT / "backend/movie-ai-agent-v2/movie_index_non_kids.csv",
                ROOT / "backend/movie-ai-agent-v2/movie_index_kids.csv"]


# --- pure logic ------------------------------------------------------------

def parse_batch(text: str) -> list:
    """Successful records of one batch output file ({"input", "output", "error"} per line)."""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("error") or not (rec.get("output") or {}).get("recommendedItems"):
            continue
        records.append(rec)
    return records


def genre_slug(filter_values: dict) -> str:
    """The GENRE placeholder value, unquoted ('"sci-fi"' -> 'sci-fi')."""
    return (filter_values or {}).get("GENRE", "").strip().strip('"').strip()


def genre_label(slug: str) -> str:
    return "-".join(part.capitalize() for part in slug.split("-"))


def resolve_profiles(mapping: dict, batch_user_ids) -> dict:
    """batch user id -> {profile_id, anchor?, anchor_title?} for the users we write."""
    out = {}
    for uid in batch_user_ids:
        entry = mapping.get(uid)
        if entry:
            if not core.PROFILE_ID_RE.match(entry.get("profile_id", "")):
                raise ValueError(f"{uid}: profile_id must look like p-<32 hex>")
            out[uid] = entry
        elif core.PROFILE_ID_RE.match(uid):
            out[uid] = {"profile_id": uid}
    return out


def build_rows(profile_id: str, top: list, genres: list, sims: list | None,
               anchor_title: str | None, now: datetime, prefix: str = core.P13N_PREFIX,
               source: str = core.PERSONALIZE_SOURCE, genre_key: str = "p13n_genre_") -> list:
    """Generated rows (P13N# by default) for one profile, in GET /home's seed shape.

    top: ranked item ids; genres: [(slug, ranked item ids)]; sims: ranked
    items similar to the anchor. Titles are de-duplicated across the page in
    rail order (hero, Top Picks, Because You Watched, genre rails); empty
    rails are skipped. connect_rails.py passes prefix=CONN#, source=connect.
    """
    stamp = core.iso(now)
    seen = set()

    def take(items, n):
        picked = []
        for t in items:
            if t not in seen and len(picked) < n:
                seen.add(t)
                picked.append(t)
        return picked

    rows = []
    hero = take(top, 1)
    if hero:
        rows.append({"profile_id": profile_id, "sk": prefix + core.HERO_SK, "title_id": hero[0],
                     "source": source, "generated_at": stamp})
    rails = [("top_picks", "Top Picks for You", top, TOP_N)]
    if sims:
        title = f"Because You Watched {anchor_title}" if anchor_title else "Because You Watched"
        rails.append(("because_you_watched", title, sims, BYW_N))
    for slug, items in genres[:MAX_GENRE_RAILS]:
        rails.append((f"{genre_key}{slug}", f"Because You Like {genre_label(slug)}", items, GENRE_N))
    rank = 0
    for key, title, items, n in rails:
        picked = take(items, n)
        if not picked:
            continue
        rank += 1
        rows.append({"profile_id": profile_id, "sk": prefix + core.ROWS_PREFIX + key, "rank": rank,
                     "display_title": title, "items": [{"title_id": t} for t in picked],
                     "source": source, "generated_at": stamp})
    return rows


def plan(top_recs: list, genre_recs: list, sim_recs: list, mapping: dict, titles: dict, now: datetime) -> dict:
    """profile_id -> its P13N# rows, from parsed batch records."""
    top = {r["input"]["userId"]: r["output"]["recommendedItems"] for r in top_recs}
    genres = {}
    for r in genre_recs:
        slug = genre_slug(r["input"].get("filterValues"))
        if slug:
            genres.setdefault(r["input"]["userId"], []).append((slug, r["output"]["recommendedItems"]))
    sims = {r["input"]["itemId"]: r["output"]["recommendedItems"] for r in sim_recs}
    users = resolve_profiles(mapping, list(dict.fromkeys([*top, *genres])))
    out = {}
    for uid, entry in users.items():
        anchor = entry.get("anchor")
        rows = build_rows(entry["profile_id"], top.get(uid, []), genres.get(uid, []),
                          [t for t in sims.get(anchor, []) if t != anchor] if anchor else None,
                          entry.get("anchor_title") or titles.get(anchor), now)
        if rows:
            out[entry["profile_id"]] = rows
    return out


def stale_keys(existing_sks, rows, prefix: str = core.P13N_PREFIX) -> list:
    """Keys under `prefix` a profile has that the new rows no longer use."""
    keep = {r["sk"] for r in rows}
    return [sk for sk in existing_sks if sk.startswith(prefix) and sk not in keep]


def check_keys(rows, deletes, prefix: str = core.P13N_PREFIX):
    """The writer may only ever touch keys under its own prefix."""
    bad = [r["sk"] for r in rows if not r["sk"].startswith(prefix)]
    bad += [sk for sk in deletes if not sk.startswith(prefix)]
    if bad:
        raise ValueError(f"refusing to write non-{prefix} keys: {bad}")


# --- I/O -----------------------------------------------------------------------

def read_text(uri: str, s3) -> str:
    if uri.startswith("s3://"):
        bucket, _, key = uri[5:].partition("/")
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    return Path(uri).read_text()


def catalog_titles() -> dict:
    titles = {}
    for path in CATALOG_CSVS:
        if path.exists():
            with path.open(newline="") as f:
                titles.update({r["title_id"]: r["originalTitle"] for r in csv.DictReader(f)})
    return titles


def existing_p13n(table, profile_id, prefix: str = core.P13N_PREFIX) -> list:
    from boto3.dynamodb.conditions import Key
    resp = table.query(KeyConditionExpression=Key("profile_id").eq(profile_id) & Key("sk").begins_with(prefix),
                       ProjectionExpression="sk")
    return [i["sk"] for i in resp.get("Items", [])]


def write_profile(table, profile_id, rows, deletes, prefix: str = core.P13N_PREFIX):
    check_keys(rows, deletes, prefix)
    with table.batch_writer() as batch:
        for row in rows:
            batch.put_item(Item=core.to_dynamo(row))
        for sk in deletes:
            batch.delete_item(Key={"profile_id": profile_id, "sk": sk})


def main():
    from config import DEFAULT_CONFIG, load_config
    from deploy import aws_environment

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--top")
    parser.add_argument("--genre")
    parser.add_argument("--sims")
    parser.add_argument("--profiles", help="JSON map of batch user id -> {profile_id, anchor, anchor_title}")
    parser.add_argument("--clear", action="store_true", help="delete the mapped profiles' P13N# rows instead")
    parser.add_argument("--apply", action="store_true", help="write to DynamoDB (default: dry run)")
    args = parser.parse_args()
    cfg = load_config(args.config)
    os.environ.update(aws_environment(cfg["region"]))
    import boto3
    session = boto3.Session(region_name=cfg["region"])
    assert session.client("sts").get_caller_identity()["Account"] == cfg["account"]
    table = session.resource("dynamodb").Table(f'{cfg["prefix"]}-recommendation-cache')
    mapping = json.loads(Path(args.profiles).read_text()) if args.profiles else {}

    if args.clear:
        pids = [e["profile_id"] for e in mapping.values()] + [k for k in mapping if core.PROFILE_ID_RE.match(k)]
        for pid in dict.fromkeys(pids):
            sks = existing_p13n(table, pid)
            print(f"{pid}: delete {len(sks)} P13N# rows" + ("" if args.apply else " (dry run)"))
            if args.apply and sks:
                write_profile(table, pid, [], sks)
        return

    s3 = session.client("s3")
    recs = {name: parse_batch(read_text(uri, s3)) if uri else []
            for name, uri in (("top", args.top), ("genre", args.genre), ("sims", args.sims))}
    rows_by_profile = plan(recs["top"], recs["genre"], recs["sims"], mapping, catalog_titles(),
                           datetime.now(timezone.utc))
    if not rows_by_profile:
        print("No mapped profiles in the batch output; nothing to write.")
        return
    for pid, rows in rows_by_profile.items():
        deletes = stale_keys(existing_p13n(table, pid), rows)
        check_keys(rows, deletes)
        print(f"{pid}: {len(rows)} rows, {len(deletes)} stale" + ("" if args.apply else " (dry run)"))
        for r in rows:
            label = r.get("display_title") or "hero"
            ids = [i["title_id"] for i in r.get("items", [])] or [r.get("title_id")]
            print(f"  {r['sk']:<45} {label}: {len(ids)} -> {', '.join(ids[:5])}{' ...' if len(ids) > 5 else ''}")
        if args.apply:
            write_profile(table, pid, rows, deletes)
    print("Written." if args.apply else "Dry run only; pass --apply to write.")


if __name__ == "__main__":
    main()
