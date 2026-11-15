"""Tag and retain the log groups automatically created by these new runtimes."""
import os
import re

import boto3


def handler(event, context):
    props = event["ResourceProperties"]
    runtime = props["RuntimeId"]
    if not re.fullmatch(re.escape(os.environ["RUNTIME_PREFIX"]) + r"_(movie|sports|voice)-[A-Za-z0-9]+", runtime):
        raise ValueError("Only this deployment's runtime log groups are allowed")
    name = f"/aws/bedrock-agentcore/runtimes/{runtime}-DEFAULT"
    if event["RequestType"] != "Delete":
        logs = boto3.client("logs")
        try:
            logs.create_log_group(logGroupName=name, tags=props["Tags"])
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        logs.put_retention_policy(logGroupName=name, retentionInDays=30)
        logs.tag_resource(resourceArn=f'arn:aws:logs:{os.environ["AWS_REGION"]}:{props["Account"]}:log-group:{name}',
            tags=props["Tags"])
    return {"PhysicalResourceId": name}
