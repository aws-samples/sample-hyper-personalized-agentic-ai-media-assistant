"""Environment-configured CDK app: Build, Application and (phase=frontend) Frontend stacks."""
import os

import aws_cdk as cdk
from stacks import BuildStack, ApplicationStack, FrontendStack

from config import load_config, stack_name

cfg = load_config()
if os.environ.get("CDK_DEFAULT_ACCOUNT", cfg["account"]) != cfg["account"]:
    raise ValueError("Unexpected deployment account")

app = cdk.App()
app.node.set_context("@aws-cdk/core:defaultCrossStackReferences", "strong")
for key, value in cfg["tags"].items():
    cdk.Tags.of(app).add(key, value)
env = cdk.Environment(account=cfg["account"], region=cfg["region"])

def options():
    return dict(env=env, synthesizer=cdk.DefaultStackSynthesizer(
        qualifier=cfg["qualifier"], generate_bootstrap_version_rule=True))

build = BuildStack(app, stack_name(cfg, "Build"), cfg=cfg, **options())
services = ApplicationStack(app, stack_name(cfg, "Application"), cfg=cfg, build=build, **options())
if app.node.try_get_context("phase") == "frontend":
    FrontendStack(app, stack_name(cfg, "Frontend"), cfg=cfg, **options())
app.synth()
