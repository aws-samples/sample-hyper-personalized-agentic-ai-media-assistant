"""Stage the what-if container's build context.

Copies the demo's own decision code, UNCHANGED, next to the what-if sources:
the movie agent package, the stream processor (L1/L2), the clips feed and the
shared layer helpers. The sources are read, never edited; the copies exist
only in the build directory.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "backend/movie-ai-agent-v2"
COPIES = [
    (ROOT / "infra/lambdas/profile_events", ["processor.py", "l1.py", "l2.py", "contract.py"]),
    (ROOT / "infra/lambdas/clips_feed", ["lambda_function.py", "clips_policy.py"]),
    (ROOT / "infra/layers/shared/python", ["ibc_common.py"]),
    (ROOT / "insights/whatif", ["handler.py", "sandbox.py", "agent_run.py", "clips_run.py", "stream.py",
                                "Dockerfile", "requirements.txt"]),
]


def stage(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    bundle = out / "bundle"
    bundle.mkdir(parents=True)
    # Agent: all modules + prompts + shards; skip CSVs, evals, docs and build scripts' data.
    for f in AGENT.glob("*.py"):
        shutil.copy2(f, bundle / f.name)
    for d in ["prompts", "shards"]:
        if (AGENT / d).exists():
            shutil.copytree(AGENT / d, bundle / d, ignore=shutil.ignore_patterns("__pycache__"))
    for src_dir, names in COPIES:
        for name in names:
            dest = out if name in ("Dockerfile", "requirements.txt") else bundle
            if dest is bundle and (bundle / name).exists() and src_dir != ROOT / "insights/whatif":
                raise SystemExit(f"module name clash in what-if bundle: {name}")
            shutil.copy2(src_dir / name, dest / name)
    return out


if __name__ == "__main__":
    print(stage(Path(sys.argv[1])))
