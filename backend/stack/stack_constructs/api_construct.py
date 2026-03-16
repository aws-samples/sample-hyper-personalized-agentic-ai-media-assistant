"""
Copyright 2024 Amazon.com, Inc. and its affiliates. All Rights Reserved.

Licensed under the Amazon Software License (the "License").
You may not use this file except in compliance with the License.
A copy of the License is located at

  http://aws.amazon.com/asl/

or in the "license" file accompanying this file. This file is distributed
on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
express or implied. See the License for the specific language governing
permissions and limitations under the License.
"""

import os

import aws_cdk as cdk
from aws_cdk import (Aws, Duration, Size, aws_appsync, aws_bedrock,
                     aws_dynamodb, aws_iam, aws_lambda,
                     aws_lambda_event_sources, aws_opensearchserverless,
                     aws_sqs)
from aws_cdk.aws_lambda_python_alpha import PythonFunction, PythonLayerVersion
from cdk_nag import NagSuppressions
from constructs import Construct

from .bedrock_prompts_construct import BedrockPromptsConstruct


class APIConstruct(Construct):
    """Deploys the API construct"""
    
    def __init__(self, scope: Construct, construct_id: str, 
                 data_table: aws_dynamodb.Table,
                 prompts: BedrockPromptsConstruct,
                 opensearch_collection_host: str = None,
                 title_catalog_index_name: str = None,
                 media_content_bucket_name: str = None,
                 lambda_execution_role = None,
                 **kwargs) -> None:
        super().__init__(scope, construct_id)
        self.opensearch_collection_host = opensearch_collection_host
        self.title_catalog_index_name = title_catalog_index_name
        self.media_content_bucket_name = media_content_bucket_name
        self.lambda_execution_role = lambda_execution_role
        self.data_table = data_table
        self.prompts = prompts
        self.powertools_layer = self._get_powertools_layer()
        self.requests_layer = self._get_requests_layer()
        self.lambda_function = self._create_conversation_processing_lambda()

    def _get_powertools_layer(self) -> aws_lambda.ILayerVersion:
        return aws_lambda.LayerVersion.from_layer_version_arn(
            self, "PowertoolsLayer",
            f"arn:aws:lambda:{Aws.REGION}:017000801446:layer:AWSLambdaPowertoolsPythonV3-python312-x86_64:5"
        )

    def _get_requests_layer(self) -> PythonLayerVersion:
        print(f"layer: {os.path.join(os.path.dirname(__file__), '..', 'lambdas', 'layers', 'request')}")
        return PythonLayerVersion(
            self, "RequestsLayer",
            entry=os.path.join(os.path.dirname(__file__), "..", "lambdas", "layers", "request"),
            compatible_runtimes=[aws_lambda.Runtime.PYTHON_3_12],
            layer_version_name="RequestsLayer"
        )

    def _create_conversation_processing_lambda(self) -> PythonFunction:
        folder_path = os.path.join(os.path.dirname(__file__), "..", "lambdas")
        rerank_model_region = "us-west-2"
        
        func = PythonFunction(
            self, "ConversationProcessingLambda",
            runtime=aws_lambda.Runtime.PYTHON_3_12,
            handler="lambda_handler",
            index="index.py",
            entry=os.path.join(folder_path, "create_update_conversation"),
            timeout=Duration.minutes(5),
            environment={
                "TABLE_NAME": self.data_table.table_name,
                "APPSYNC_ENDPOINT": f"https://{self.chat_api.http_dns}/event" if hasattr(self, 'chat_api') else "",
                "POWERTOOLS_METRICS_NAMESPACE": "CONVOSEARCH",
                "POWERTOOLS_SERVICE_NAME": "AGENT",
                "INTENT_CLASSIFICATION_PROMPT": self.prompts.classification_prompt.attr_arn,
                "QUERY_REWRITE_PROMPT": self.prompts.rewrite_prompt.attr_arn,
                "FINAL_RESPONSE_PROMPT": self.prompts.final_prompt.attr_arn,
                "EMBEDDING_MODEL": "amazon.nova-2-multimodal-embeddings-v1:0",
                "RERANK_MODEL": f"arn:{Aws.PARTITION}:bedrock:{rerank_model_region}::foundation-model/{aws_bedrock.FoundationModelIdentifier.AMAZON_RERANK_V1.model_id}",
                "OPENSEARCH_CLUSTER_URL": self.opensearch_collection_host,
                "RERANK_PROMPT": self.prompts.rerank_prompt.attr_arn,
                "QUOTE_FINDER_PROMPT": self.prompts.quote_finder_prompt.attr_arn,
                "TITLE_INDEX": self.title_catalog_index_name,
                "GENERAL_RESPONSE_PROMPT": self.prompts.general_q_prompt.attr_arn,
                "MEDIA_CONTENT_BUCKET_NAME" : self.media_content_bucket_name
            },
            layers=[self.powertools_layer, self.requests_layer],
            memory_size=Size.gibibytes(5).to_mebibytes(),
            role=self.lambda_execution_role
        )

        # self.data_table.grant_read_write_data(func)
        func.role.add_to_principal_policy(
            aws_iam.PolicyStatement(
                actions=["bedrock:*", "appsync:*", "aoss:*", "dynamodb:*", "s3:*"],
                resources=["*"]
            )
        )
        
        NagSuppressions.add_resource_suppressions(
            func,
            [{
                "id": "AwsSolutions-IAM5",
                "reason": "Lambda needs access to all resources",
                "appliesTo": [
                    "Resource::*",
                    "Action::appsync:*",
                    "Action::bedrock:*",
                    "Action::aoss:*",
                    "Action::s3:*"
                ]
            }],
            apply_to_children=True
        )
        
        return func