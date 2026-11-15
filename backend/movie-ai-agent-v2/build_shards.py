"""Offline step 2: token-balance the catalog CSVs into shard files.

- Kids -> a single always-warm shard (fits under the cap).
- Non-kids -> greedily packed token-balanced shards under a budget.

Each shard is written as a CSV (the data) plus a rendered system-prompt .txt
(what actually gets cached at request time). A manifest.json records per-shard
title counts and BOTH estimated and model-measured token counts.

Projection = "medium": keeps cast NAMES (top 6, no character mappings) and
director/writer names for people-based recall, drops the expensive
"as (Character)" detail (that belongs to the scene-analysis pipeline).

Usage (from this directory, after build_kids_split.py; needs Bedrock access for
token measurement):
    python build_shards.py
"""

import csv
import json
import re
from pathlib import Path

import boto3
from botocore.config import Config

# --- tunables ---
SHARD_TARGET_TOKENS = 28_000   # greedy packing target
SHARD_HARD_CAP = 30_000        # a shard must never exceed this (incl. preamble)
CAST_CAP = 6                   # top-N cast names to keep
MEASURE_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION = "us-east-1"

# Non-kids routing categories by title_type. Direct-to-video features and
# shorts are films, so they shard with movies.
MOVIE_TYPES = {"movie", "tvMovie", "video", "short"}
TV_TYPES = {"tvSeries", "tvEpisode", "tvMiniSeries", "tvSpecial"}
# Anything else (videoGame) falls to the "other" group, which is not sharded.

OUT_DIR = Path("shards")
SYSTEM_PREAMBLE = (
    "You are a movie recommendation assistant for a streaming service.\n"
    "Recommend titles ONLY from the CATALOG below.\n"
    "\n"
    "Default ranking (apply UNLESS the user's request specifies otherwise):\n"
    "- Among titles that fit the request, prefer more recent releases (higher year)\n"
    "  and more popular, higher-rated titles (higher rating).\n"
    "- Relevance to the request always comes first; use recency and rating only as\n"
    "  tie-breakers between similarly relevant titles.\n"
    "- If the user asks for something different (a specific era or decade, classics,\n"
    "  hidden gems, lower-rated or obscure picks), follow the user instead.\n"
    "\n"
    "Return ONLY the recommended titles, one per line, each formatted EXACTLY as:\n"
    "  <Title> [<title_id>]\n"
    "where <title_id> is the bracketed id shown for that entry in the catalog\n"
    "(e.g. \"Quantum Storm [tt1001001]\"). Return NOTHING else.\n\n"
    "CATALOG:\n"
)

_brt = boto3.client("bedrock-runtime", region_name=REGION,
                    config=Config(retries={"max_attempts": 4, "mode": "adaptive"}))


def _names(items, keep_roles=None, cap=None):
    out = []
    for it in (items or []):
        m = re.match(r"^(.*?) as \((.*)\)$", it)
        name = m.group(1).strip() if m else it.strip()
        role = (m.group(2).lower() if m else "")
        if keep_roles and not any(r in role for r in keep_roles):
            continue
        if name:
            out.append(name)
    return out[:cap] if cap else out


def _split(cell: str):
    return [x for x in (cell or "").split("; ") if x]


def render(row: dict) -> str:
    """Medium projection for one catalog row (from the split CSVs)."""
    plot = (row.get("plot") or "")[:100]
    genres = ", ".join(_split(row.get("genres", "")))
    cast = ", ".join(_names(_split(row.get("principalCastMembers", "")), cap=CAST_CAP))
    crew = ", ".join(_names(_split(row.get("principalCrewMembers", "")),
                            keep_roles=["director", "writer"]))
    return (f"- {row.get('originalTitle')} ({row.get('premiere_year')}, {row.get('title_type')}) "
            f"[{row.get('title_id')}] | {genres} | rating {row.get('imdb_rating')} | {plot}\n"
            f"  cast: {cast} | crew: {crew}")


def measure_tokens(text: str) -> int:
    r = _brt.converse(modelId=MEASURE_MODEL, system=[{"text": text}],
                      messages=[{"role": "user", "content": [{"text": "hi"}]}],
                      inferenceConfig={"maxTokens": 1})
    return r["usage"]["inputTokens"]


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def calibrate_ratio(rendered: list[str]) -> float:
    """chars-per-token for this projection, from a real measurement of a sample."""
    sample = "\n".join(rendered[:200])
    toks = measure_tokens(SYSTEM_PREAMBLE + sample)
    return len(SYSTEM_PREAMBLE + sample) / toks


def pack(rows: list[dict], ratio: float) -> list[list[dict]]:
    """Greedy packing by estimated tokens, stable order by title_id.

    Uses the fewest shards that fit SHARD_TARGET_TOKENS, then spreads titles
    evenly across them, so no shard is a tiny tail (a tail under the model's
    cache minimum would run uncached and still cost a fan-out call)."""
    rows = sorted(rows, key=lambda r: r.get("title_id", ""))
    preamble_est = len(SYSTEM_PREAMBLE) / ratio
    ests = [len(render(row)) / ratio + 1 for row in rows]  # +1 for the joining newline
    body_cap = SHARD_TARGET_TOKENS - preamble_est
    n = max(1, -(-int(sum(ests)) // int(body_cap)))  # ceil
    target = sum(ests) / n
    shards, cur, cur_tok = [], [], 0.0
    for row, est in zip(rows, ests):
        if cur and len(shards) < n - 1 and (cur_tok + est > target or cur_tok + est > body_cap):
            shards.append(cur)
            cur, cur_tok = [], 0.0
        cur.append(row)
        cur_tok += est
    if cur:
        shards.append(cur)
    return shards


def write_shard(prefix: str, idx: int, rows: list[dict], fieldnames: list[str],
                category: str) -> dict:
    OUT_DIR.mkdir(exist_ok=True)
    sid = f"{prefix}_{idx:02d}"
    csv_path = OUT_DIR / f"{sid}.csv"
    txt_path = OUT_DIR / f"{sid}.system.txt"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        w.writeheader()
        w.writerows(rows)
    system_text = SYSTEM_PREAMBLE + "\n".join(render(r) for r in rows)
    txt_path.write_text(system_text, encoding="utf-8")
    actual = measure_tokens(system_text)
    status = "OK" if actual <= SHARD_HARD_CAP else "OVER_CAP"
    print(f"  {sid}: {len(rows):4} titles  {actual:6} tokens  [{status}]")
    return {"shard": sid, "category": category, "titles": len(rows), "tokens": actual,
            "csv": str(csv_path), "system_txt": str(txt_path), "status": status}


def title_category(row: dict) -> str:
    """Map a title_type to a non-kids routing category: movie / tv / other."""
    t = (row.get("title_type") or "").strip()
    if t in MOVIE_TYPES:
        return "movie"
    if t in TV_TYPES:
        return "tv"
    return "other"


def write_group(prefix: str, category: str, rows: list[dict], fieldnames: list[str],
                ratio: float, manifest: dict) -> None:
    """Token-balance one category's rows into shards and record them."""
    if not rows:
        return
    print(f"\n[{category}] {len(rows)} titles -> token-balanced shards:")
    for i, chunk in enumerate(pack(rows, ratio)):
        manifest["shards"].append(write_shard(prefix, i, chunk, fieldnames, category))


def _clean_out_dir() -> None:
    """Remove stale shard files so renamed/rebalanced runs don't leave orphans."""
    if OUT_DIR.exists():
        for p in list(OUT_DIR.glob("*.csv")) + list(OUT_DIR.glob("*.system.txt")):
            p.unlink()


def main() -> None:
    kids = load("movie_index_kids.csv")
    non_kids = load("movie_index_non_kids.csv")
    fieldnames = list(kids[0].keys())

    ratio = calibrate_ratio([render(r) for r in non_kids])
    print(f"calibrated chars/token = {ratio:.2f} (medium projection)")

    OUT_DIR.mkdir(exist_ok=True)
    _clean_out_dir()

    manifest = {"projection": "medium", "cast_cap": CAST_CAP,
                "shard_target_tokens": SHARD_TARGET_TOKENS,
                "shard_hard_cap": SHARD_HARD_CAP, "shards": []}

    # Kids: single always-warm safety shard.
    print("\n[kids] single always-warm shard:")
    manifest["shards"].append(write_shard("kids", 0, kids, fieldnames, "kids"))

    # Non-kids: split by movie / tv / other, then token-balance each group.
    groups = {"movie": [], "tv": [], "other": []}
    for row in non_kids:
        groups[title_category(row)].append(row)

    write_group("nonkids_movie", "movie", groups["movie"], fieldnames, ratio, manifest)
    write_group("nonkids_tv", "tv", groups["tv"], fieldnames, ratio, manifest)
    # "other" is video games (videoGame): not watchable, so never sharded or
    # recommended (retrieval.NEVER_RECOMMEND_TYPES keeps them out of search too).
    if groups["other"]:
        types = sorted({r.get("title_type") for r in groups["other"]})
        print(f"\n[other] {len(groups['other'])} titles left out ({', '.join(types)})")

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    total_titles = sum(s["titles"] for s in manifest["shards"])
    over = [s["shard"] for s in manifest["shards"] if s["status"] != "OK"]
    by_cat = {}
    for s in manifest["shards"]:
        by_cat.setdefault(s["category"], [0, 0])
        by_cat[s["category"]][0] += 1
        by_cat[s["category"]][1] += s["titles"]
    print(f"\nwrote {len(manifest['shards'])} shards, {total_titles} titles total -> {OUT_DIR}/manifest.json")
    for cat, (nshards, ntitles) in by_cat.items():
        print(f"  {cat}: {nshards} shard(s), {ntitles} titles")
    print(f"over-cap shards: {over or 'none'}")


if __name__ == "__main__":
    main()
