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
import os
from constructs import Construct
from aws_cdk import (
    aws_lambda as _lambda,
    custom_resources as custom_resources,
    Aws,
    Duration,
    CustomResource,
    BundlingOptions,
    aws_iam as iam
)


class OpenSearchIndexConstruct(Construct):
    """A CDK construct that creates an opensearch index from a given OpenSearch serverless Collection.

    Args:
        scope (Construct): The scope in which this construct is defined
        host (str): The opensearch host
        region (str): The region where the Opensearch server is running
        index_name (str): The name of the index
        **kwargs: Additional keyword arguments to pass to the parent Construct class
    """
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        lambda_role: iam.Role,
        opensearch_host: str,
        kb_index_name: str,
        title_catalog_index_name: str,
        bda_video_level_detail_index_name:str, 
        bda_video_chapter_index_name: str,
        bda_video_chapter_audio_segment_index_name: str,
        bda_video_chapter_segment_index_name: str,
        **kwargs
    ) -> None:

        super().__init__(scope, construct_id, **kwargs)

        # Create a custom IAM role for Lambda
        create_index_function = _lambda.Function(
            self,
            "CreateOpenSearchIndex",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset(
                    os.path.join(os.path.dirname(__file__), "../lambdas", "opensearch_index"),
                    bundling=BundlingOptions(
                        image=_lambda.Runtime.PYTHON_3_13.bundling_image,
                        command=[
                            "bash", "-c",
                            "pip install --no-cache -r requirements.txt -t /asset-output && cp -au . /asset-output"
                        ],
                    ),
                ),
            description="Create an OpenSearch Index",
            timeout=Duration.minutes(10),
            memory_size=256,
            role=lambda_role
        )

        provider = custom_resources.Provider(
            self,
            "CreateOpenSearchIndexProvider",
            on_event_handler=create_index_function,
        )

        create_index_custom_resource = CustomResource(
            self,
            "OpenSearchIndexCustomResource",
            service_token=provider.service_token,
            properties={
                "region": Aws.REGION,
                "kb_index_name": kb_index_name,
                "title_catalog_index_name": title_catalog_index_name,
                "bda_video_level_detail_index_name": bda_video_level_detail_index_name, 
                "bda_video_chapter_index_name": bda_video_chapter_index_name,
                "bda_video_chapter_audio_segment_index_name": bda_video_chapter_audio_segment_index_name,
                "bda_video_chapter_segment_index_name": bda_video_chapter_segment_index_name,
                "opensearch_host": opensearch_host
            },
            
        )