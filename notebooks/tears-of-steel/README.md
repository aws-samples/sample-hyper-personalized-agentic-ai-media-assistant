# Tears of Steel: playback and scene analysis

*Tears of Steel* (2012) is (c) Blender Foundation | [mango.blender.org](https://mango.blender.org), licensed under [Creative Commons Attribution 3.0](https://creativecommons.org/licenses/by/3.0/). The catalog ships its entry (`tt2285752`) and poster. These notebooks add the film itself and its scene analysis to a deployment. Nothing here is stored in the repository: the film is downloaded from Blender, and the analysis is written to your own account.

| Notebook | What it does | Needed for |
|---|---|---|
| `1-video-scenes.ipynb` | Downloads the 1080p film from an official Blender mirror (SHA-256 checked), converts it to `tears-of-steel.mp4`, uploads it to `s3://<mediaBucket>/clips/movies/`, adds it to the video mapping table, runs Bedrock Data Automation on it, optionally recognizes celebrities with Rekognition, writes the `chapters`, `segments` and `audio-segments` indexes, and cuts two highlights with their descriptions | Playback, "ask about this scene", and the title-page highlight (an action and a romantic cut, about 10 s each, picked per profile) |
| `2-script-knowledge-base.ipynb` | Extracts the script you downloaded (PDF through Bedrock Data Automation, or a text file), cleans it, uploads it with its `video_name` metadata to the stack's Knowledge Base source and syncs the Knowledge Base | Optional: answers grounded in the script |

Both are adapted from the v1 sample's [`bda-video.ipynb`](https://github.com/aws-samples/sample-hyper-personalized-agentic-ai-media-assistant/blob/v1/backend/tools/bda-video.ipynb) and [`script_analysis.ipynb`](https://github.com/aws-samples/sample-hyper-personalized-agentic-ai-media-assistant/blob/v1/backend/tools/script_analysis.ipynb). The differences: they read the indexes, buckets, tables and Knowledge Base from your environment file and the stack outputs; they use the deployment's existing resources instead of creating new ones; and re-running them replaces the film's data instead of adding copies.

## Before you start

- Deploy the stacks and run the loaders (`infra/deployment/README.md`).
- Python 3.10+ with Jupyter. `requirements.txt` installs `boto3` and an `ffmpeg` binary (`imageio-ffmpeg`); a system `ffmpeg` is used when present.
- Credentials for the deployment account: the deploying role, or a role listed in `dataLoaderPrincipals` in your environment file (only those may write to the search collection). The role also needs Bedrock Data Automation, `s3` on the content bucket and the Knowledge Base source bucket, `dynamodb:PutItem` on the video table, `bedrock:StartIngestionJob`, and `rekognition:RecognizeCelebrities` if you keep celebrity detection on.
- About 2.5 GB of disk space.
- The script, for the second notebook: download it from [Blender Studio](https://studio.blender.org/projects/tears-of-steel/55f344892beb3300251b0172/?asset=34) (free registration). It is not redistributed here.

## Run

From the repository root, with the same credentials you deployed with:

```bash
.venv/bin/python -m pip install -r notebooks/tears-of-steel/requirements.txt jupyter
.venv/bin/jupyter lab notebooks/tears-of-steel/
```

Open a notebook (it imports `tears_of_steel.py` next to it), set `CONFIG` in the first code cell to your environment file, for example `"../../infra/environments/demo.json"`, and run all cells. Without a browser, run it headless after setting `CONFIG`:

```bash
.venv/bin/jupyter nbconvert --to notebook --execute --inplace --ExecutePreprocessor.timeout=3600 notebooks/tears-of-steel/1-video-scenes.ipynb
```

- Notebook 1 takes about 10 minutes, mostly the Bedrock Data Automation job. Bedrock Data Automation bills per minute of video (the film is 12 minutes); celebrity detection makes one Rekognition call per shot (about 170).
- Set `LOCAL_FILM` to skip the download if you already have the film.
- New index documents are searchable about a minute after the notebook finishes.

Then open the app, find *Tears of Steel* (also on the Action Fan's Sci-Fi Epic row and My List), press Play and ask, for example, "What's happening in this scene?" `infra/deployment/e2e.py` also checks playback and two scene questions once the film is set up.

## For your own titles

Use the same steps with your own video: change `TITLE_ID`, `VIDEO_NAME` and `VIDEO_KEY` in `tears_of_steel.py`, add the title to the catalog and `infra/deployment/seed/title_video_mapping.json`, and map the title to its `video_name` in `frontend/src/lib/sceneAgentClient.ts`. The scene indexes and the Knowledge Base filter key on `video_name`.
