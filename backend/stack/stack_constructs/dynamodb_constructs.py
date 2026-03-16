from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
)
from constructs import Construct

class DynamoDbConstruct(Construct):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create user_conversation_history_table
        user_conversation_history_table = dynamodb.Table(
            self, "UserConversationHistoryTable",
            table_name="user_conversation_history",
            partition_key=dynamodb.Attribute(
                name="pk",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY  # Use RETAIN for production
        )

        # Create user_conversation_history_table
        title_video_mapping_table = dynamodb.Table(
            self, "TitleVideoMappingTable",
            table_name="title_video_mapping",
            partition_key=dynamodb.Attribute(
                name="title_id",
                type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY  # Use RETAIN for production
        )

        # Create user_conversation_history_table
        user_watch_log_table = dynamodb.Table(
            self, "UserWatchLogTable",
            table_name="user_watch_log",
            partition_key=dynamodb.Attribute(
                name="username",
                type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="current_time",
                type=dynamodb.AttributeType.NUMBER
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY  # Use RETAIN for production
        )

        CfnOutput(
            self, "UserConversationHistoryTableArn",
            value=user_conversation_history_table.table_arn,
            description="ARN of the UserConversationHistoryTable"
        )

        CfnOutput(
            self, "UserConversationHistoryTableName",
            value=user_conversation_history_table.table_name,
            description="Name of the UserConversationHistoryTable"
        )

        CfnOutput(
            self, "TitleVideoMappingTableArn",
            value=title_video_mapping_table.table_arn,
            description="ARN of the TitleVideoMappingTable"
        )

        CfnOutput(
            self, "TitleVideoMappingTableName",
            value=title_video_mapping_table.table_name,
            description="Name of the TitleVideoMappingTable"
        )

        CfnOutput(
            self, "UserWatchLogTableArn",
            value=user_watch_log_table.table_arn,
            description="ARN of the UserWatchLogTable"
        )

        CfnOutput(
            self, "UserWatchLogTableName",
            value=user_watch_log_table.table_name,
            description="Name of the UserWatchLogTable"
        )

        # Store table references as instance attributes for external access
        self.user_conversation_history_table = user_conversation_history_table
        self.title_video_mapping_table = title_video_mapping_table
        self.user_watch_log_table = user_watch_log_table