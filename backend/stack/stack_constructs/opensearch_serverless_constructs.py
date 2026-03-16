from aws_cdk import (
    aws_opensearchserverless as opensearchserverless,
    aws_iam as iam,
    aws_s3 as s3,
    RemovalPolicy,
    Duration,
)
from constructs import Construct
from cdk_nag import NagSuppressions
import json

class OpenSearchServerlessConstruct(Construct):
    def __init__(self, scope: Construct, construct_id: str,
                 bedrock_kb_role: iam.Role,
                 lambda_role: iam.Role,
                 account, 
                 region,  **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.ecs_task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        
        self.ecs_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Retrieve*",
                    "bedrock:Rerank",
                    "polly:SynthesizeSpeech",
                    "transcribe:StartStreamTranscription",
                    "transcribe:StartTranscriptionJob",
                    "transcribe:GetTranscriptionJob",
                    "translate:TranslateText",
                    "cloudwatch:PutMetricData",
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:ListBucket",
                    "dynamodb:*",
                    "es:*",
                    "aoss:*",
                    "bedrock-agentcore:*",
                    "secretsmanager:*",
                    "lambda:*"
                ],
                resources=["*"],
            )
        )

        self.convo_search_lambda_execution_role = iam.Role(
            self,
            "ConvoSearchLambdaExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
        )
        self.convo_search_lambda_execution_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )       
        self.convo_search_lambda_execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:*", 
                    "appsync:*", 
                    "aoss:*", 
                    "dynamodb:*", 
                    "s3:*"
                ],
                resources=["*"],
            )
        )

        # Create unique collection name
        self.collection_name = f"bedrock-kb-{self.node.addr.lower()[:5]}"
        # Create encryption policy for OpenSearch Serverless
        self.encryption_policy = opensearchserverless.CfnSecurityPolicy(
            self, "KnowledgeBaseEncryptionPolicy",
            name=f"bedrock-kb-enc-{self.node.addr.lower()[:5]}",
            type="encryption",
            policy=json.dumps({
                "Rules": [
                    {
                        "Resource": [f"collection/{self.collection_name}"],
                        "ResourceType": "collection"
                    }
                ],
                "AWSOwnedKey": True
            })
        )

        # Create network policy for OpenSearch Serverless
        self.network_policy = opensearchserverless.CfnSecurityPolicy(
            self, "KnowledgeBaseNetworkPolicy",
            name=f"bedrock-kb-network-{self.node.addr.lower()[:5]}",
            type="network",
            policy=json.dumps([{
                "Rules": [
                    {
                        "Resource": [f"collection/{self.collection_name}"],
                        "ResourceType": "collection"
                    }
                ],
                "AllowFromPublic": True
            }])
        )

        # Create data access policy with the Bedrock role ARN
        self.data_access_policy = opensearchserverless.CfnAccessPolicy(
            self, "KnowledgeBaseDataAccessPolicy",
            name=f"bedrock-kb-ds-policy-{self.node.addr.lower()[:5]}",
            type="data",
            policy=json.dumps([{
                "Rules": [
                    {
                        "Resource": [f"collection/{self.collection_name}"],
                        "Permission": [
                            "aoss:CreateCollectionItems",
                            "aoss:DeleteCollectionItems",
                            "aoss:UpdateCollectionItems",
                            "aoss:DescribeCollectionItems"
                        ],
                        "ResourceType": "collection"
                    },
                    {
                        "Resource": [f"index/{self.collection_name}/*"],
                        "Permission": [
                            "aoss:CreateIndex",
                            "aoss:DeleteIndex",
                            "aoss:UpdateIndex",
                            "aoss:DescribeIndex",
                            "aoss:ReadDocument",
                            "aoss:WriteDocument"
                        ],
                        "ResourceType": "index"
                    }
                ],
                "Principal": [
                    bedrock_kb_role.role_arn, 
                    lambda_role.role_arn,
                    self.ecs_task_role.role_arn,
                    self.convo_search_lambda_execution_role.role_arn,
                ]
            }])
        )

        # Create OpenSearch Serverless collection
        self.opensearch_collection = opensearchserverless.CfnCollection(
            self, "KnowledgeBaseCollection",
            name=self.collection_name,
            type="VECTORSEARCH",
            description="OpenSearch Serverless collection for Bedrock Knowledge Base"
        )

        # Set proper dependencies
        self.opensearch_collection.add_dependency(self.network_policy)
        self.opensearch_collection.add_dependency(self.encryption_policy)
        self.opensearch_collection.add_dependency(self.data_access_policy)

        # Grant OpenSearch Serverless permissions to Bedrock role
        bedrock_kb_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "aoss:APIAccessAll"
            ],
            resources=[self.opensearch_collection.attr_arn]
        ))

        # Create dedicated S3 bucket for knowledge base data source
        self.knowledge_base_data_bucket = s3.Bucket(
            self, "KnowledgeBaseDataBucket",
            bucket_name=f"{account}-{region}-bedrock-kb-data-{self.node.addr.lower()[:6]}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            versioned=True,
            public_read_access=False,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="DeleteIncompleteMultipartUploads",
                    abort_incomplete_multipart_upload_after=Duration.days(7)
                )
            ]
        )
        
        self.knowledge_base_data_bucket.grant_read(bedrock_kb_role)

        NagSuppressions.add_resource_suppressions(
            self.ecs_task_role,
            suppressions=[{
                "id": "AwsSolutions-IAM5",
                "reason": "Task role needs access to various AWS services with dynamic resource names"
            }]
        )
      

