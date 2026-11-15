# Infrastructure

Use [deployment/README.md](deployment/README.md) for the CDK deployment. Select
settings with `--config infra/environments/<name>.json`, copied from
`environments/example.json`. Account, region, names, models and the content
bucket belong in that file; the implementation is shared across regions.

- `deployment/`: stacks, runner, data loaders, tests and smoke checks.
- `lambdas/`: API Lambda code.
- `layers/shared/`: shared Lambda layer.
- `stack/stack_constructs/api_constructs.py`: the API construct used by `deployment/stacks.py`.
