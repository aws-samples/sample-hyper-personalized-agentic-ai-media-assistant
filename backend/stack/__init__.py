import os
from aws_cdk import (
    Stack,
    CfnOutput,
    Aws,
    aws_ecr_assets as ecr_assets,
    aws_opensearchserverless as opensearchserverless,
    aws_iam as iam,
    aws_s3 as s3
)
from aws_cdk import Duration
from aws_cdk.aws_stepfunctions import Wait, WaitTime
from constructs import Construct
from cdk_nag import NagSuppressions, NagPackSuppression
from .stack_constructs import (
    ServerAccessLogsBucketConstruct,
    BucketConstruct,
    CognitoConstruct,
    CloudFrontDistributionConstruct,
    DockerImageAssetConstruct,
    VPCConstruct,
    FargateNLBConstruct,
    get_backend_language,
    BackendLanguage,
    DynamoDbConstruct,
    S2sUserAffinityConstruct,
    S2sMovieShowtimesConstruct,
    BedrockKnowledgeBaseConstruct,
    OpenSearchServerlessConstruct,
    BedrockKBServiceRoleConstruct,
    OpenSearchIndexConstruct,
    BedrockPromptsConstruct,
    APIConstruct
)

class OpensearchServerlessStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.bedrock_kb_service_role = BedrockKBServiceRoleConstruct(
            self, "BedrockServiceRole", region=self.region
        )


        # Create a custom IAM role for Lambda
        self.lambda_role = iam.Role(
            self, "OpenSearchIndexLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
        )

        # Attach managed policy (e.g., AWSLambdaBasicExecutionRole for logs)
        self.lambda_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )

        # Optionally, add an inline policy
        self.lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["aoss:*"],
                resources=["*"]
            )
        )
        # Step 2: Create OpenSearch Serverless with the service role
        self.opensearch_serverless = OpenSearchServerlessConstruct(
            self, "OpenSearchServerless",
            bedrock_kb_role=self.bedrock_kb_service_role.bedrock_kb_role,
            lambda_role=self.lambda_role,
            account = self.account,
            region=self.region
        )

        self.kb_index_name = "bedrock-knowledge-base-index"
        self.title_catalog_index_name = "title_catalog_index"
        self.bda_video_level_detail_index_name = "bda-video-level-detail-index" 
        self.bda_video_chapter_index_name = "bda-video-chapter-level-detail-index"
        self.bda_video_chapter_audio_segment_index_name = "bda-video-chapter-audio-segment-level-detail-index"
        self.bda_video_chapter_segment_index_name = "bda-video-chapter-segment-level-detail-index"
        self.opensearch_index = OpenSearchIndexConstruct(self, 
                                                         "OpenSearchIndex", 
                                                         lambda_role=self.lambda_role,
                                                         opensearch_host=self.opensearch_serverless.opensearch_collection.attr_collection_endpoint, 
                                                         kb_index_name=self.kb_index_name,
                                                         title_catalog_index_name=self.title_catalog_index_name,
                                                         bda_video_level_detail_index_name=self.bda_video_level_detail_index_name, 
                                                         bda_video_chapter_index_name=self.bda_video_chapter_index_name,
                                                         bda_video_chapter_audio_segment_index_name=self.bda_video_chapter_audio_segment_index_name,
                                                         bda_video_chapter_segment_index_name=self.bda_video_chapter_segment_index_name
                                                         )

        CfnOutput(
            self, "OpenSearchCollectionEndpoint",
            value=self.opensearch_serverless.opensearch_collection.attr_collection_endpoint,
            description="OpenSearch Serverless collection endpoint"
        )

        CfnOutput(
            self, "OpenSearchCollectionTitleCatalogIndex",
            value=self.title_catalog_index_name,
            description="OpenSearch Serverless collection Title Catalog index name"
        )

        CfnOutput(
            self, "OpenSearchCollectionKBKindex",
            value=self.kb_index_name,
            description="OpenSearch Serverless collection Knowledge Bases for Bedrock index name"
        )

        CfnOutput(
            self, "OpenSearchCollectionBDAVideoLevelIndex",
            value=self.bda_video_level_detail_index_name,
            description="OpenSearch Serverless collection BDA video level index name"
        )

        CfnOutput(
            self, "OpenSearchCollectionBDAVideoChapterLevelIndex",
            value=self.bda_video_chapter_index_name,
            description="OpenSearch Serverless collection BDA video chapter level index name"
        )

        CfnOutput(
            self, "OpenSearchCollectionBDAVideoChapterAudioSegmentIndex",
            value=self.bda_video_chapter_audio_segment_index_name,
            description="OpenSearch Serverless collection BDA video chapter audio segment index name"
        )

        CfnOutput(
            self, "OpenSearchCollectionBDAVideoChapterSegmentIndex",
            value=self.bda_video_chapter_segment_index_name,
            description="OpenSearch Serverless collection BDA video chapter segment index name"
        )

        
        NagSuppressions.add_stack_suppressions(self,
                                    [ 
                                        NagPackSuppression(id="AwsSolutions-IAM4",
                                                        reason="The IAM role, requires AWS managed policies"),
                                        NagPackSuppression(id="AwsSolutions-IAM5",
                                                        reason="The IAM policy requires wildcards"),
                                        NagPackSuppression(id="AwsSolutions-S1",
                                                        reason="No S3 access log required"),
                                        NagPackSuppression(id="AwsSolutions-L1",
                                                           reason="The Lambda runtime version is managed by CDK Bucket Deployment construct")
                                    ]
                                    )

class BedrockKBStack(Stack):
    def __init__(self, scope: Construct, 
                 construct_id: str, 
                 bedrock_kb_role: iam.Role, 
                 lambda_role: iam.Role,
                 knowledge_base_data_bucket: s3.Bucket,
                 aoss_collection: opensearchserverless.CfnCollection,
                 aoss_host: str,
                 kb_index_name: str,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        
        # Create Bedrock Knowledge Base with both role and collection
        self.bedrock_kb = BedrockKnowledgeBaseConstruct(
            self, "BedrockKnowledgeBase",
            bedrock_kb_role=bedrock_kb_role,
            opensearch_collection=aoss_collection,
            knowledge_base_data_bucket = knowledge_base_data_bucket,
            region=self.region,
            account=self.account,
            index_name=kb_index_name
        )

        # self.bedrock_kb.node.add_dependency(self.opensearch_index)


        CfnOutput(
            self, "KnowledgeBaseDataBucket",
            value=knowledge_base_data_bucket.bucket_name,
            description="S3 bucket for knowledge base documents"
        )

        CfnOutput(
            self, "KnowledgeBaseId",
            value=self.bedrock_kb.knowledge_base.attr_knowledge_base_id,
            description="Bedrock Knowledge Base ID"
        )

        CfnOutput(
            self, "KnowledgeBaseDataSourceId",
            value=self.bedrock_kb.data_source.attr_data_source_id,
            description="Bedrock Knowledge Base Data Source ID"
        )

        NagSuppressions.add_stack_suppressions(
            self,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="AWS managed policies are used for demo purposes",
                ),
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Wildcard permissions are used for demo purposes",
                ),
                NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="The Lambda runtime version is managed by CDK Bucket Deployment construct",
                )
            ]
        )

class BackendStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, kb_id: str, aoss_stack: OpensearchServerlessStack, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Get the path to the frontend directory
        # need to npm run build and push the build folder to the frontend directory first
        
        frontend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "react-client/dist")
    
        # Get backend language from context
        backend_language = get_backend_language(self.node.root)
    
        #######################
        ### BUCKET RESOURCES ##
        #######################
        
        # Create server access logs bucket
        server_access_logs_bucket = ServerAccessLogsBucketConstruct(
            self, 
            "ServerAccessLogsBucket"
        )
        
        # Create website bucket
        website_bucket = BucketConstruct(
            self, 
            "WebsiteBucket", 
            server_access_logs_bucket
        )
        
        # Create website bucket
        media_content_bucket = BucketConstruct(
            self, 
            "MediaContentBucket", 
            server_access_logs_bucket
        )

        ########################
        ### COGNITO RESOURCES ##
        ########################
        
        # Create Cognito resources
        cognito_construct = CognitoConstruct(
            self, 
            "Cognito", 
            region=Aws.REGION, 
            bucket=website_bucket
        )
        
        #######################
        ### DOCKER RESOURCES ##
        #######################
        
        # Create Docker image asset from the local Dockerfile
        # Set image name and dockerfile based on backend language
        if backend_language == BackendLanguage.PYTHON:
            docker_image_asset = DockerImageAssetConstruct(
                self,
                "WebSocketPythonImage",
                directory=os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/python-server",
                file="Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64
            )
        else:
            docker_image_asset = DockerImageAssetConstruct(
                self,
                "WebSocketJavaImage",
                directory=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                file="Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64
            )
        #######################
        ### FARGATE SERVICE ###
        #######################

        # Create VPC
        vpc = VPCConstruct(
            self,
            "VPC"
        )
        dynamodb_tables = DynamoDbConstruct(self, "DynamoDbTables")
        convo_search_bedrock_prompt = BedrockPromptsConstruct(self,"ConvoSearchBedrockPrompt")
        aoss_host=aoss_stack.opensearch_serverless.opensearch_collection.attr_collection_endpoint
        api_convosearch = APIConstruct(self, "ConvoSearchAPI", 
                 data_table=dynamodb_tables.user_conversation_history_table,
                 prompts=convo_search_bedrock_prompt,
                 opensearch_collection_host=aoss_host,
                 title_catalog_index_name=aoss_stack.title_catalog_index_name,
                 media_content_bucket_name=media_content_bucket.bucket_name,
                 lambda_execution_role = aoss_stack.opensearch_serverless.convo_search_lambda_execution_role)

        # Create Fargate service with NLB
        fargate_service = FargateNLBConstruct(
            self,
            "FargateService",
            container_image=docker_image_asset.image,
            vpc=vpc.vpc,
            cognito_user_pool_id=cognito_construct.user_pool.user_pool_id,
            cognito_client_id=cognito_construct.user_pool_client.user_pool_client_id,
            kb_id=kb_id,
            api_convosearch=api_convosearch,
            aoss_stack=aoss_stack
        )
        
        ##########################
        ### CLOUDFRONT RESOURCES #
        ##########################

        # Create CloudFront distribution
        cloudfront_distribution = CloudFrontDistributionConstruct(
            self,
            "CloudFrontDistribution",
            website_bucket=website_bucket,
            load_balancer=fargate_service.load_balancer,
            ui_assets_path=frontend_path
        )
        
        #####################
        ### STACK OUTPUTS ###
        #####################

        CfnOutput(
            self,
            "RegionName",
            value=self.region,
            export_name=f"{Stack.of(self).stack_name}RegionName",
        )
        
        CfnOutput(
            self,
            "UserPoolId",
            value=cognito_construct.user_pool.user_pool_id,
            description="Cognito User Pool ID",
        )
        
        CfnOutput(
            self,
            "UserPoolClientId",
            value=cognito_construct.user_pool_client.user_pool_client_id,
            description="Cognito User Pool Client ID",
        )
        
        CfnOutput(
            self,
            "WebsiteBucketName",
            value=website_bucket.bucket_name,
            description="S3 bucket hosting the static website",
        )

        CfnOutput(
            self,
            "MediaContentBucketName",
            value=media_content_bucket.bucket_name,
            description="S3 bucket for storing media content",
        )
        
        CfnOutput(
            self,
            "CloudFrontDistributionDomainName",
            value=cloudfront_distribution.distribution.distribution_domain_name,
            description="CloudFront distribution domain name",
        )
        
        CfnOutput(
            self,
            "LoadBalancerDNS",
            value=fargate_service.load_balancer.load_balancer_dns_name,
            description="Load balancer DNS name",
        )

        CfnOutput(
            self, "OpenSearchServerlessHost",
            value=aoss_host,
            description="Opensearch Serverless Endpoint"
        )

        CfnOutput(
            self, "BDA-VideoLevelIndexName",
            value=aoss_stack.bda_video_level_detail_index_name,
            description="BDA Video Level Index Name"
        )

        CfnOutput(
            self, "BDA-VideoChapterLevelIndexName",
            value=aoss_stack.bda_video_chapter_index_name,
            description="BDA Video Chapter Level Index Name"
        )

        CfnOutput(
            self, "BDA-VideoChapterAudioLevelIndexName",
            value=aoss_stack.bda_video_chapter_audio_segment_index_name,
            description="BDA Video Chapter Audio Level Index Name"
        )

        CfnOutput(
            self, "BDA-VideoChapterSegmentLevelIndexName",
            value=aoss_stack.bda_video_chapter_segment_index_name,
            description="BDA Video Chapter Segment Level Index Name"
        )
        
        CfnOutput(
            self, "ConvoSearchLambdaFunctionArn",
            value=api_convosearch.lambda_function.function_arn,
            description="Conversation Search Lambda Function ARN"
        )
        
        ##########################
        ## CDK NAG SUPPRESSIONS ##
        ##########################
        
        NagSuppressions.add_stack_suppressions(
            self,
            [
                NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="AWS managed policies are used for demo purposes",
                ),
                NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Wildcard permissions are used for demo purposes",
                ),
                NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason="Server access logs are not required for this demo application",
                ),
                NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="The Lambda runtime version is managed by CDK Bucket Deployment construct",
                ),
                NagPackSuppression(
                    id="AwsSolutions-APIG1",
                    reason="Access logging not required for development environment"
                ),
                NagPackSuppression(
                    id="AwsSolutions-APIG3",
                    reason="API gateway stage is not associated with WAFv2 web ACL"
                ),
                NagPackSuppression(
                    id="AwsSolutions-APIG4",
                    reason="Access logging not required for development environment"
                ),
                NagPackSuppression(
                    id="AwsSolutions-APIG6",
                    reason="Access logging not required for development environment"
                ),
                NagPackSuppression(
                    id="AwsSolutions-COG4",
                    reason="Cognito integration not required for API Gateway"
                ),
                NagPackSuppression(
                    id="AwsSolutions-APIG4",
                    reason="API Gateway does not require authorization"
                ),
                NagPackSuppression(
                    id="AwsSolutions-DDB3",
                    reason="DynamoDB does not have point in time recovery"
                ),
                NagPackSuppression(
                    id="AwsSolutions-S10",
                    reason="The S3 Bucket or bucket policy does not require requests to use SSL. Development environment."
                ),
                NagPackSuppression(
                    id="AwsSolutions-SQS3",
                    reason="The SQS queue is not used as a dead letter queue and doesn't have DLQ enabled"
                ),
                NagPackSuppression(
                    id="AwsSolutions-SQS4",
                    reason="The SQS queue does not require requests to use SSL"
                )
            ],
        )







        
