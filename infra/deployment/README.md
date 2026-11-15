# Application deployment

This is the shared CDK implementation. Account, region, resource names, tags,
model IDs, inference endpoints and the content bucket come from an environment
JSON file. There is no region-specific entrypoint.

## Layout

- `infra/deployment/`: stacks, deployment runner, data loaders, custom-resource
  handlers, tests and smoke checks.
- `infra/environments/example.json`: template for your environment file.
- `infra/.artifacts/<name>/`: ignored CDK assemblies, image digests and outputs.
- `frontend/dist-deploy/<name>/`: ignored compiled UI for that environment.

## Configuration

Copy `infra/environments/example.json` to `infra/environments/<name>.json`
(ignored by git) and replace every `<...>` placeholder. The runner refuses a
file that still contains placeholders.

| Setting | Purpose |
|---|---|
| `name` | Unique local environment name; separates generated artifacts |
| `account`, `region` | Target AWS account and deployment region |
| `prefix` | Physical resource names and derived AgentCore runtime names |
| `stackPrefix` | CloudFormation stack names; defaults to the title-cased prefix |
| `qualifier` | Dedicated CDK bootstrap namespace |
| `tags` | Tags for supported resources and the bootstrap stack |
| `runtimePlatformVersion` | AgentCore platform `V2` (default), or `V1` |
| `models` | Movie, memory, scene, sports, voice, KB and web-grounding models |
| `embeddingModel`, `embeddingRegion` | Catalog embedding model and its endpoint (used for indexing and queries) |
| `groundingRegion` | Endpoint for the gateway's Nova web-grounding Lambda |
| `mediaBucket` | Private content bucket in your account for posters and micro-drama videos. `load_media.py` creates it |
| `dataLoaderPrincipals` | Optional extra IAM role ARNs allowed to write the catalog index (the deploying role is always allowed) |
| `sceneBucket`, `scriptsBucket`, `scriptsPrefix`, `sceneRegion` | Optional. Your own scene-analysis output and episode scripts for the scene-analysis agent. Leave unset to skip them |
| `soccerUrl`, `soccerVerticalUrl` | Live-match video paths on the app's own distribution (default `/media/soccer/{horizontal,vertical}/football.mp4`) |
| `enableWarmer` | Turn on the shard cache-warmer schedule |
| `homeRailsSource` | Which Home rows `GET /home` serves: `seed`, `personalize` or `connect` |

Check AWS service and model availability in your region, and configure the
matching inference profiles, before deploying.

The catalog uses Amazon Nova Multimodal Embeddings with 1,024 dimensions. The
scripts Knowledge Base uses Titan Text Embeddings V2 in the deployment region.
These are separate indexes.

## AgentCore Runtime V2

The movie, sports and voice runtimes use the configured platform version. V2
restores initialized containers from snapshots. Reusable imports and static
assets initialize before the health endpoint becomes available; per-request
identity and session state stay in request and connection handlers.

The installed CDK bindings do not expose `PlatformVersion`, so the stack sets
it with a CDK property override. The pinned CDK validator may warn that
`PlatformVersion` is unexpected; CloudFormation accepts it.

V2 creation and updates can take several minutes while snapshots are
prepared. Containers must become healthy within 120 seconds and fit the 2.5 KB
environment-variable limit.

## Deploy

Use Python 3.12, Node 22+ and AWS CDK CLI 2.1128.1 or newer.

**Credentials.** The scripts and the CDK CLI use the standard AWS credential
chain: `AWS_PROFILE` (including IAM Identity Center / SSO profiles, after
`aws sso login`), environment keys, CloudShell or an instance role. Before
changing anything, each script checks that the credentials belong to the
`account` in your environment file and prints the identity it uses. The
deploying role is added to the search collection's data-access policy, so the
same credentials can run `load_catalog.py`. To accept only an EC2 instance
profile (and ignore any profile or keys on the machine), set
`HPM_INSTANCE_PROFILE_ONLY=1`.

```bash
export AWS_PROFILE=<your-profile>   # signed in already (e.g. aws sso login for an SSO profile)
```

The main [README](../../README.md#deploy) has the prerequisites and the
one-command deploy:

```bash
.venv/bin/python infra/deployment/deploy.py --config $CONFIG --create-user <login>
```

`--phase all` (the default) runs these phases in order; each can also run
alone with `--phase <name>`:

| Phase | What it does |
|---|---|
| `synth` | Synthesizes the templates and runs the template tests (also first in `all` and `application`) |
| `bootstrap` | Dedicated CDK bootstrap stack with your `qualifier` |
| `build` | Build stack, then builds the three agent images in CodeBuild |
| `application` | Application stack, with the runtimes pinned to the built image digests |
| `frontend` | Creates the media signing key if missing (`media_signing_key.py`), builds the UI, deploys the Frontend stack |
| `data` | `load_media.py`, `load_catalog.py`, `seed_video_mapping.py`, `backend/live-sports-agent/load_events_dynamodb.py`, `load_commentary_dynamodb.py` |
| `insights` | `insights/infra/deploy_insights.py` (needs Docker) |
| `check` | `smoke.py` (resources and wiring), then `e2e.py` (viewer flows on the loaded data) |
| `user` | Only `--create-user <login>` |

Each loader and check also runs on its own with `--config $CONFIG`, and all
are safe to re-run. Optional after the deploy: the Tears of Steel notebooks in
`notebooks/tears-of-steel/` for movie playback and scene analysis.

`--create-user <login>` adds a Cognito user with a random permanent password
and prints it once with the app and insights URLs. It refuses an existing
login rather than resetting its password.

`e2e.py` signs in as a temporary user and checks the household and Home rows
(catalog titles with presigned posters), typed recommendations and a
micro-drama request on the movie runtime, micro-drama playback, the clips
feed, the spoiler-safe match state (1-1 before the clip's goal, 2-1 after) and
the sports runtime's answer at both moments.

Run `build`, then `application`, after backend agent changes; images are
tagged by source hash and deployed by immutable ECR digest, and `application`
refuses to deploy when the source changed after the last build.

Five stacks use the configured `stackPrefix`:

| Suffix | Resources |
|---|---|
| Bootstrap | Dedicated asset bucket, registry and deployment roles |
| Build | Three ECR repositories, ARM64 CodeBuild project, logs |
| Application | Cognito, DynamoDB tables, search collection and indexes, KB and its source bucket, memory, three runtimes, web-search gateway and Lambda, API, cache-warmer schedule |
| Frontend | Private UI bucket, private media bucket, CloudFront with OAC, frontend asset deployment |
| Insights | Insights console: its own bucket, CloudFront, read-only API and the layout and What-if Lambdas |

## Data

Everything the demo needs ships in this repository. There is no public
bucket; the loaders copy the data into your account.

| Loader | Source in the repo | Destination |
|---|---|---|
| `load_media.py --only catalog` | `data/catalog/posters.zip` | `s3://<mediaBucket>/posters/<file>.jpg` |
| `load_media.py --only clips` | `data/media/microdramas-*.zip` | `s3://<mediaBucket>/clips/microdramas/<name>.mp4` |
| `load_media.py --only sports` | `data/media/football-*.zip` | Frontend stack `MediaBucket`, `media/soccer/{horizontal,vertical}/football.mp4` |
| `load_catalog.py` | `data/catalog/*.json` | `catalog` index (Nova embeddings, poster `image` = `s3://<mediaBucket>/posters/...`) |
| `seed_video_mapping.py` | `seed/title_video_mapping.json` | `<prefix>-videos` table |
| `backend/live-sports-agent/load_events_dynamodb.py` | `backend/live-sports-agent/data/match_events.json`, `match_meta.json` | `<prefix>-events` table (events plus the match's `META` item) |
| `backend/live-sports-agent/load_commentary_dynamodb.py` | `backend/live-sports-agent/data/match_commentary.vtt` | `<prefix>-commentary` table |

Notes:

- `load_media.py` creates `mediaBucket` when it is missing (Block Public
  Access, SSE-S3, TLS-only bucket policy). `--only sports` needs the Frontend
  stack; without `--only`, the sports part is skipped with a warning until that
  stack exists.
- `load_catalog.py` must run as the deploying role or a role listed in
  `dataLoaderPrincipals`; both are in the collection's data-access policy. The
  role also needs `aoss:APIAccessAll` on the collection and `bedrock:InvokeModel`
  on the embedding model. `--dry-run` prints counts without AWS calls.
- The match-state Lambda and the sports agent read only the two match tables,
  so the live-sports screens fail until those loaders have run.
- The CloudFront `/media/*` path is served only with signed cookies, which
  signed-in viewers get from `GET /media/session`. `media_signing_key.py`
  registers the public key and stores the private key in the Secrets Manager
  secret `<prefix>/media-signing-key`. `--rotate` replaces it.
- Sign-in uses the Cognito user pool from the Application stack; self sign-up
  is off. Create users with `deploy.py --phase user --create-user <login>`
  (the UI does not handle temporary passwords, so give console- or CLI-created
  users a permanent password with `admin-set-user-password --permanent`). Each
  user gets the sample household from `seed/default_household.json` at first
  sign-in.
- Scene analysis (`request_type: "analysis"`) reads the `chapters`,
  `segments` and `audio-segments` indexes and scripts under `scriptsPrefix` in
  the KB source bucket. The notebooks in `notebooks/tears-of-steel/` fill them
  for Tears of Steel; use them as the pattern for your own
  titles. Without that data the agent answers that scene details aren't
  available.
- Title-detail highlight cuts: the Tears of Steel notebook uploads an action
  and a romantic cut with their descriptions. For other titles, put clips
  under `s3://<mediaBucket>/clips/highlights/vod/{horizontal,vertical}/`,
  describe them in `seed/highlight_descriptions.json`, run
  `annotate_highlights.py`, and add the title to `vodHighlights` in your
  environment file (default `{"tt2285752": "tears-of-steel"}`) and to
  `frontend/src/lib/vodHighlights.ts`.
- Enable `enableWarmer` once the catalog is loaded and model access is ready.
- Stateful resources and indexes are retained when the stacks are deleted.

## Cost

Most resources are pay-per-use (AgentCore runtimes, Lambda, API Gateway,
DynamoDB on-demand, S3, CloudFront, Bedrock model calls). Two bill by the hour
while the stacks exist, even with no traffic:

| Resource | Idle cost (approximate) |
|---|---|
| OpenSearch Serverless collection `<prefix>-search` (vector search, standby replicas disabled) | Minimum of 1 OCU (0.5 indexing + 0.5 search), about $0.24 per OCU-hour, so roughly $175 a month, plus storage |
| Kinesis Data Streams `<prefix>-profile-events` (on-demand) | About $0.04 per stream-hour, roughly $30 a month, plus data |

The shard cache warmer (`enableWarmer`) calls the movie model once per shard
every 50 minutes; turn it off when you are not demoing. Prices vary by region
and change over time; check [OpenSearch Service pricing](https://aws.amazon.com/opensearch-service/pricing/)
and [Kinesis Data Streams pricing](https://aws.amazon.com/kinesis/data-streams/pricing/)
before you deploy. Delete the deployment when you are done (next section).

## Clean up

```bash
.venv/bin/python infra/deployment/destroy.py --config $CONFIG          # lists what would be deleted
.venv/bin/python infra/deployment/destroy.py --config $CONFIG --yes    # deletes it
```

Deleting a stack keeps its stateful resources on purpose
(`RemovalPolicy.RETAIN`), so a mistaken delete loses no data. `destroy.py`
deletes the stacks and those resources, in this order:

1. The stacks Insights, Frontend, Application and Build, each waited for.
2. What they keep or leave behind: the stacks' S3 buckets (emptied first,
   all object versions), the DynamoDB tables, the Cognito user pool, the
   search collection and its security and data-access policies, the ECR
   repositories, log groups, the media signing key (secret, SSM parameter and
   CloudFront public key) and the Tears of Steel Bedrock Data Automation
   projects.
3. The CDK staging bucket and container repository, then the Bootstrap stack.

It finds everything by exact name from your environment file (`prefix`,
`stackPrefix`, `qualifier`), so other deployments in the same account and
Region are never matched, and it prints the identity it uses first. Your
content bucket `mediaBucket` is kept unless you add `--include-media-bucket`,
because several deployments can share it. A full run takes 30 to 40 minutes,
mostly CloudFront and the search collection. If it stops part way, run it
again: what is already gone is skipped. The deletions are permanent.

To delete by hand instead, delete the stacks newest first (Insights,
Frontend, Application, Build) and then the resources in step 2, named
`<prefix>-*`, `<StackPrefix>*` and `/aws/bedrock-agentcore/runtimes/<runtimePrefix>_*`
(`runtimePrefix` is `prefix` with `-` replaced by `_`), and last the
bootstrap leftovers `cdk-<qualifier>-assets-<account>-<region>`,
`cdk-<qualifier>-container-assets-<account>-<region>` and the
`<StackPrefix>Bootstrap` stack.

## Verification

```bash
HPM_CONFIG=$CONFIG .venv/bin/python -m pytest infra/deployment -q
.venv/bin/python infra/deployment/deploy.py --config $CONFIG --phase synth
.venv/bin/python infra/deployment/deploy.py --config $CONFIG --phase check
```

The template tests read the synthesized assembly of the environment in
`HPM_CONFIG` and skip when there is none. `--phase synth` runs them against
freshly generated templates: isolation, tags, runtime regions,
credential selection, embedding protocols, index signing and custom-resource
boundaries.

The smoke checks verify runtime readiness, KB retrieval, gateway citations,
Cognito login, authenticated API reads, the movie-runtime ping, signed-cookie
access to `/media/*` (refused without cookies, served with them), CloudFront
routes and frontend endpoint wiring. They create and delete a temporary user in
the pool. Use `--application-only` or `--frontend-only` to run part of them.

AWS references:

- [Model regional availability](https://docs.aws.amazon.com/bedrock/latest/userguide/models-region-compatibility.html)
- [Runtime platform versions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html)
- [Snapshot-safe initialization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html)
- [Nova web grounding](https://docs.aws.amazon.com/nova/latest/nova2-userguide/web-grounding.html)
- [CloudFormation index mapping types](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-opensearchserverless-index-propertymapping.html)
