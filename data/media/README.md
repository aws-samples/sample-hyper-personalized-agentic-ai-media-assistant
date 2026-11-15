# Demo media

Video shipped with the demo, packed as zip files (each under 50 MB) so they can
live in the repository. Nothing here is served from a public bucket: you upload
it into your own account with `infra/deployment/load_media.py`.

| Zip | Entries | Uploaded to |
|---|---|---|
| `football-horizontal.zip` | `football/horizontal/football.mp4` (1920x1080) | `s3://<Frontend MediaBucket>/media/soccer/horizontal/football.mp4` |
| `football-vertical.zip` | `football/vertical/football.mp4` (1080x1920) | `s3://<Frontend MediaBucket>/media/soccer/vertical/football.mp4` |
| `microdramas-1.zip`, `microdramas-2.zip` | `microdramas/<name>.mp4` (1080x1920, 30 s, with audio) | `s3://<mediaBucket>/clips/microdramas/<name>.mp4` |

The catalog posters (`data/catalog/posters.zip`, entries `posters/<file>.jpg`)
are uploaded by the same script to `s3://<mediaBucket>/posters/<file>.jpg`.

## Content

- **Football** (`football*.zip`): a 60-second broadcast-style clip of a football
  match, sample footage supplied by the project owner for use in this open-source
  demo. It drives the synthetic live match `SYN-MAT-0001` (fictional teams and
  players); the match events are keyed to the video's seconds, so both
  renditions keep the original timeline exactly (same duration, no trim). The
  horizontal rendition is the full frame scaled to 1080p; the vertical one is a
  centre crop of the same frames. H.264 High, yuv420p, 25 fps, `+faststart`, no
  audio track (the commentary is a caption feed).
- **Micro-dramas** (`microdramas-*.zip`): eight 30-second vertical short films,
  AI-generated demo content made for this project. They are the playable titles
  of the catalog (`md0000001`..`md0000008`, see
  `data/catalog/microdrama_dataset.json`). H.264, AAC audio, `+faststart`.

## Upload

```bash
# Content bucket (posters + micro-dramas): any time after editing the config.
.venv/bin/python infra/deployment/load_media.py --config infra/environments/<name>.json --only catalog --only clips
# Live-sports video: after the frontend phase (needs the Frontend stack's MediaBucket).
.venv/bin/python infra/deployment/load_media.py --config infra/environments/<name>.json --only sports
```

Without `--only` all parts are uploaded (sports is skipped with a warning when
the Frontend stack does not exist yet). `mediaBucket` from the config is created
if it does not exist: private (Block Public Access), SSE-S3, TLS-only, in the
config region. Re-runs skip objects whose content has not changed.
