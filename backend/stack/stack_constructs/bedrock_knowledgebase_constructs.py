from aws_cdk import (
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_s3 as s3,
    aws_opensearchserverless as opensearchserverless,
    RemovalPolicy,
    Duration,
)
from constructs import Construct

class BedrockKnowledgeBaseConstruct(Construct):
    def __init__(self, scope: Construct, construct_id: str,
                 bedrock_kb_role: iam.Role,
                 knowledge_base_data_bucket: s3.Bucket,
                 opensearch_collection: opensearchserverless.CfnCollection,
                 region: str,
                 account: str,
                 index_name: str,
                 **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
  
        # Add specific S3 permissions for Bedrock
        bedrock_kb_role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "s3:GetObject",
                "s3:ListBucket"
            ],
            resources=[
                knowledge_base_data_bucket.bucket_arn,
                f"{knowledge_base_data_bucket.bucket_arn}/*"
            ]
        ))

        # Create Bedrock Knowledge Base
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self, "BedrockKnowledgeBase",
            name=f"bedrock-knowledge-base-{self.node.addr.lower()}",
            description="Knowledge base with OpenSearch Serverless vector database and dedicated S3 data source",
            role_arn=bedrock_kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{region}::foundation-model/amazon.nova-2-multimodal-embeddings-v1:0"
                )
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="OPENSEARCH_SERVERLESS",
                opensearch_serverless_configuration=bedrock.CfnKnowledgeBase.OpenSearchServerlessConfigurationProperty(
                    collection_arn=opensearch_collection.attr_arn,
                    vector_index_name=index_name,
                    field_mapping=bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                        vector_field="vector",
                        text_field="AMAZON_BEDROCK_TEXT_CHUNK",
                        metadata_field="AMAZON_BEDROCK_METADATA"
                    )
                )
            )
        )

        # Create data source for the knowledge base
        self.data_source = bedrock.CfnDataSource(
            self, "KnowledgeBaseDataSource",
            name="s3-data-source",
            description="Dedicated S3 data source for knowledge base documents",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=knowledge_base_data_bucket.bucket_arn,
                    inclusion_prefixes=["documents/"],
                    bucket_owner_account_id=account
                )
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="FIXED_SIZE",
                    fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                        max_tokens=300,
                        overlap_percentage=20
                    )
                )
            )
        )

        # Add final dependency
        self.data_source.add_dependency(self.knowledge_base)