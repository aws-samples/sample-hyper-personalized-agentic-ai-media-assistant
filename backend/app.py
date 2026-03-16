#!/usr/bin/env python3
#
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
# and limitations under the License.
#

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks
from stack import BackendStack, OpensearchServerlessStack, BedrockKBStack
import os

USAGE_METRIC = 'uksb-1tupboc45'
SOLUTION_NAME = "Bedrock Nova Sonic Solution"

app = cdk.App()

aoss_stack = OpensearchServerlessStack(app, "OpenSearchServerlessStack", 
                                       description=f'({USAGE_METRIC})(tag: {SOLUTION_NAME})',
                                        env=cdk.Environment(
                                            account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                                            region=os.environ.get("CDK_DEFAULT_REGION")
                                        ))

bedrockKB_stack = BedrockKBStack(app, 
                                 "BedrockKBStack",
                                 description=f'({USAGE_METRIC})(tag: {SOLUTION_NAME})',
                                 bedrock_kb_role=aoss_stack.bedrock_kb_service_role.bedrock_kb_role,
                                 lambda_role=aoss_stack.lambda_role,
                                 knowledge_base_data_bucket=aoss_stack.opensearch_serverless.knowledge_base_data_bucket,
                                 aoss_collection=aoss_stack.opensearch_serverless.opensearch_collection,
                                 aoss_host=aoss_stack.opensearch_serverless.opensearch_collection.attr_collection_endpoint,
                                 kb_index_name=aoss_stack.kb_index_name,
                                 env=cdk.Environment(
                                     account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                                     region=os.environ.get("CDK_DEFAULT_REGION")
                                ))
backend_stack = BackendStack(
    app, 
    "NovaSonicSolutionBackendStack",
    description=f'({USAGE_METRIC})(tag: {SOLUTION_NAME})',
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION")
    ),
    kb_id = bedrockKB_stack.bedrock_kb.knowledge_base.attr_knowledge_base_id,
    aoss_stack=aoss_stack
)
cdk.Aspects.of(app).add(AwsSolutionsChecks())

app.synth()
