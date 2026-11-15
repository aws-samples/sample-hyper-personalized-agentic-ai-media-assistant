# Demo catalog

The movie catalog the recommendation agent searches: 509 titles, all safe to
redistribute. `infra/deployment/load_catalog.py` embeds and indexes it into
the deployment's `catalog` search index; `load_media.py` uploads the posters.

| File | Titles | Source | Licence |
|------|--------|--------|---------|
| `movie_dataset.json` | 500 synthetic movies, `tt1001001`–`tt1001500` | [sample-hyper-personalized-agentic-ai-media-assistant](https://github.com/aws-samples/sample-hyper-personalized-agentic-ai-media-assistant) (`data/movie_dataset.json`), unchanged | MIT-0 |
| `exclusive_movie_dataset.json` | *Tears of Steel* (2012), `tt2285752` | same repository (`data/exclusive_movie_dataset.json`), unchanged | Film and poster: CC BY 3.0, (c) Blender Foundation, [mango.blender.org](https://mango.blender.org) |
| `microdrama_dataset.json` | 8 vertical micro-dramas, `md0000001`–`md0000008` | made for this demo | AI-generated demo content, released with this repository |
| `posters.zip` | `posters/<file>.jpg` for every title above | v1 poster set (400x400), the micro-drama posters, and the Tears of Steel poster resized to 400 px wide | as the title it belongs to |

The synthetic titles, plots, cast and crew are generated sample data, not
real productions. Only the micro-dramas have playable video
(`data/media/microdramas-*.zip`).

## Schema

One JSON array per file, one object per title:

| Field | Type | Notes |
|-------|------|-------|
| `title_id` | string | Unique. Index documents are keyed by it. |
| `originalTitle` | string | |
| `title_type` | string | `movie` or `micro-drama` |
| `genres` | string[] | Lowercase ids. The v1 files spell science fiction `scifi`; the loader maps it to `sci-fi`, the id the agent and the genre settings use. |
| `keywords` | string[] | |
| `plot` | string | |
| `principalCastMembers` | string[] | `"Name as (Character)"` |
| `principalCrewMembers` | string[] | `"Name as (role)"` |
| `mpaa_ratings` | string[] | Lowercase (`g`, `pg`, `pg-13`, `r`, `tv-14`, ...). Kids titles are those with any of `g`, `tv-y`, `tv-y7`, `tv-g`. |
| `imdb_rating` | number | Sample rating, 0-10 |
| `premiere_date` | string | `YYYYMMDD` |
| `premiere_year` | integer | |
| `awards` | string[] | |
| `title_text` | string | The text that is embedded: genres, keywords, plot, title, cast, crew, awards and decade, one per line. Built by the loader when missing. |
| `image` | string | Poster file. The v1 files say `images/<file>.jpg` (and a URL for Tears of Steel); the micro-drama file says `posters/<file>.jpg`. The loader indexes every poster as `s3://<mediaBucket>/posters/<file>.jpg`. |
| `imdb_url` | string | v1 files only; the synthetic ids do not resolve on IMDb. |
| `synopsis` | string | Present on a few v1 titles; not indexed. |

## Changing the catalog

1. Edit or add a JSON file here (and its posters in `posters.zip`).
2. `python infra/deployment/load_catalog.py --dry-run` to check counts, then
   run it without `--dry-run` to index.
3. Rebuild the agent's fan-out shards: in `backend/movie-ai-agent-v2/`,
   `python build_kids_split.py && python build_shards.py`, then copy
   `shards/*.system.txt` and `shards/manifest.json` to
   `backend/shard-warmer/shards/`.
4. If option texts no longer match the catalog, edit
   `infra/deployment/seed/tappable_options.json` and run
   `python scripts/build_tappable_pool.py --local`.
