from aws_cdk import (
    aws_iam as iam,
)
from constructs import Construct

class BedrockKBServiceRoleConstruct(Construct):
    def __init__(self, scope: Construct, construct_id: str, region: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create IAM role for Bedrock Knowledge Base
        self.bedrock_kb_role = iam.Role(
            self, "BedrockKnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonBedrockFullAccess")
            ]
        )

        # Add basic Bedrock permissions
        self.bedrock_kb_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "bedrock:InvokeModel",
            ],
            resources=[f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0", 
                        f"arn:aws:bedrock:{region}::foundation-model/amazon.nova-2-multimodal-embeddings-v1:0"]
        ))
