"""Validate synthesized deployment boundaries before any deployment."""
import json
import os
from pathlib import Path

import pytest

from config import load_config, stack_name

# Runs against the synthesized templates of the selected config (HPM_CONFIG,
# set by deploy.py). Skipped when that config has no synth output yet, unless
# HPM_REQUIRE_SYNTH is set (deploy.py sets it after every synth).
CFG = load_config()
OUT = Path(CFG["artifactDir"]) / "cdk.out"
if not list(OUT.glob(CFG["stackPrefix"] + "*.template.json")) and not os.environ.get("HPM_REQUIRE_SYNTH"):
    pytest.skip("Synthesize infra/deployment/app.py first (deploy.py --phase synth)", allow_module_level=True)
EXPECTED_TAGS = CFG["tags"]

def templates():
    found = list(OUT.glob(CFG["stackPrefix"] + "*.template.json"))
    assert found, "Synthesize infra/deployment/app.py first"
    return [json.loads(path.read_text()) for path in found]

def test_no_existing_bucket_policy_or_regional_account_mutation():
    for template in templates():
        for resource in template["Resources"].values():
            assert resource["Type"] != "AWS::ApiGateway::Account"
            if resource["Type"] == "AWS::S3::BucketPolicy":
                bucket = resource["Properties"]["Bucket"]
                assert isinstance(bucket, dict) and "Ref" in bucket
                assert template["Resources"][bucket["Ref"]]["Type"] == "AWS::S3::Bucket"
            assert resource["Type"] not in {"AWS::CloudFormation::Stack", "AWS::DynamoDB::GlobalTable"}

def test_application_writes_only_new_databases_and_memory():
    app = json.loads((OUT / (stack_name(CFG, "Application") + ".template.json")).read_text())
    for resource in app["Resources"].values():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"]
            if isinstance(actions, str):
                actions = [actions]
            if any(a.startswith("dynamodb:") for a in actions):
                serialized = json.dumps(statement["Resource"])
                table_ids = [key for key, val in app["Resources"].items() if val["Type"] == "AWS::DynamoDB::Table"]
                assert any(key in serialized for key in table_ids)
            assert not any(a in {"s3:PutBucketPolicy", "s3:DeleteObject"} for a in actions)
            if "s3:PutObject" in actions:
                # Only the stack's own new buckets (the profile event log) may be written.
                own_buckets = [key for key, val in app["Resources"].items() if val["Type"] == "AWS::S3::Bucket"]
                resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
                assert all(any(key in json.dumps(r) for key in own_buckets) for r in resources), resources

def test_tags_on_main_taggable_resources():
    types = {"AWS::S3::Bucket", "AWS::DynamoDB::Table", "AWS::IAM::Role", "AWS::ECR::Repository",
             "AWS::CodeBuild::Project", "AWS::Lambda::Function", "AWS::CloudFront::Distribution",
             "AWS::BedrockAgentCore::Runtime", "AWS::BedrockAgentCore::Memory", "AWS::Bedrock::KnowledgeBase",
             "AWS::BedrockAgentCore::Gateway", "AWS::OpenSearchServerless::Collection", "AWS::Logs::LogGroup"}
    for template in templates():
        for resource in template["Resources"].values():
            if resource["Type"] not in types:
                continue
            tags = resource["Properties"].get("Tags", resource["Properties"].get("UserPoolTags", {}))
            if isinstance(tags, list):
                tags = {tag["Key"]: tag["Value"] for tag in tags}
            for key, value in EXPECTED_TAGS.items():
                assert tags.get(key) == value, resource["Type"]

def test_new_runtime_wiring():
    app = json.loads((OUT / (stack_name(CFG, "Application") + ".template.json")).read_text())
    runtimes = [r for r in app["Resources"].values() if r["Type"] == "AWS::BedrockAgentCore::Runtime"]
    assert len(runtimes) == 3
    for runtime in runtimes:
        props = runtime["Properties"]
        assert props["PlatformVersion"] == CFG["runtimePlatformVersion"]
        assert props["EnvironmentVariables"]["AWS_REGION"] == CFG["region"]

def test_no_f1_table_and_loader_principal_can_write_catalog():
    app = json.loads((OUT / (stack_name(CFG, "Application") + ".template.json")).read_text())
    tables = [r["Properties"]["TableName"] for r in app["Resources"].values() if r["Type"] == "AWS::DynamoDB::Table"]
    assert f'{CFG["prefix"]}-f1' not in tables
    owner = next(r for r in app["Resources"].values() if r["Type"] == "AWS::OpenSearchServerless::AccessPolicy"
                 and r["Properties"]["Name"] == f'{CFG["prefix"]}-owner')
    policy = json.dumps(owner["Properties"]["Policy"])
    assert "aoss:WriteDocument" in policy
    for arn in CFG["dataLoaderPrincipals"] + [a for a in [os.environ.get("HPM_DEPLOYER_ARN")] if a]:
        assert arn in policy


def test_booth_group_exists_and_matches_the_lambdas():
    """The stack creates the `booth` group the household, settings and events
    Lambdas check, so booth mode works on every deployment."""
    app = json.loads((OUT / (stack_name(CFG, "Application") + ".template.json")).read_text())
    groups = [r["Properties"]["GroupName"] for r in app["Resources"].values()
              if r["Type"] == "AWS::Cognito::UserPoolGroup"]
    assert groups == ["booth"]
    root = Path(__file__).resolve().parents[2]
    for lam in ("household/lambda_function.py", "profile_settings/lambda_function.py", "profile_events/ingest.py"):
        assert '"booth" in' in (root / "infra/lambdas" / lam).read_text(), lam
    assert "BOOTH_GROUP = 'booth'" in (root / "frontend/src/lib/booth.ts").read_text()