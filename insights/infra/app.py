"""CDK app for the insights console only. It never instantiates the demo stacks."""
import json
import os
import sys
from pathlib import Path

import aws_cdk as cdk

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "infra/deployment"), str(Path(__file__).resolve().parent)]
from config import load_config, stack_name  # noqa: E402  (demo config, read only)
from stack import InsightsStack  # noqa: E402

cfg = load_config()
ext = json.loads(Path(os.environ["INSIGHTS_EXT"]).read_text())
settings = json.loads((Path(__file__).resolve().parent / "insights.json").read_text())

app = cdk.App()
for key, value in {**cfg["tags"], "Component": "insights"}.items():
    cdk.Tags.of(app).add(key, value)
InsightsStack(app, stack_name(cfg, "Insights"), cfg=cfg, ext=ext, settings=settings,
              web_dir=os.environ["INSIGHTS_WEB_DIR"], whatif_dir=os.environ["INSIGHTS_WHATIF_DIR"],
              env=cdk.Environment(account=cfg["account"], region=cfg["region"]),
              synthesizer=cdk.DefaultStackSynthesizer(qualifier=cfg["qualifier"], generate_bootstrap_version_rule=True))
app.synth()
