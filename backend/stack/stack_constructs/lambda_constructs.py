from aws_cdk import (
    Duration,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_dynamodb as dynamodb,
)
from constructs import Construct

class S2sUserAffinityConstruct(Construct):
    def __init__(self, scope: Construct, construct_id: str, 
                 user_affinity_table: dynamodb.Table, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create IAM role for the Lambda function
        lambda_role = iam.Role(
            self, "UserAffinityLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
            ]
        )

        # Grant DynamoDB permissions
        user_affinity_table.grant_read_write_data(lambda_role)

        # Create the Lambda function
        self.lambda_function = _lambda.Function(
            self, "UserAffinityFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("stack/lambdas/s2s-user-affinity"),  # Path to your Lambda code
            role=lambda_role,
            timeout=Duration.seconds(30),
            memory_size=256,
        )

        # Store table reference for external access
        self.user_affinity_table = user_affinity_table

class S2sMovieShowtimesConstruct(Construct):
    def __init__(self, scope: Construct, construct_id: str, 
                 movie_ticket_office_table: dynamodb.Table, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create IAM role for the Lambda function
        lambda_role = iam.Role(
            self, "MovieShowtimesLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
            ]
        )

        # Grant DynamoDB permissions
        movie_ticket_office_table.grant_read_write_data(lambda_role)

        # Create the Lambda function
        self.lambda_function = _lambda.Function(
            self, "MovieShowtimesFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset("stack/lambdas/s2s-movie-showtimes"),  # Path to your Lambda code
            role=lambda_role,
            timeout=Duration.seconds(30),
            memory_size=256
        )


        # Store table reference for external access
        self.movie_ticket_office_table = movie_ticket_office_table
