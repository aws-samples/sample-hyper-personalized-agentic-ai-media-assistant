"""API construct — recommendations + My List API.

Provisions the recommendation reader Lambdas (get_top_picks / get_carousels /
get_list / get_video_url) plus the My List writers (save_list /
remove_from_list), their permissions (read — and for the writers, write — the
profiles table + query the catalog OpenSearch collection), an *additive* OpenSearch Serverless data-access policy for the
reader roles (does not touch the agent's existing policy), and an API Gateway
REST endpoint protected by a Cognito authorizer on the demo user pool.
"""

import os

from aws_cdk import (
    Stack,
    CfnOutput,
    Duration,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_opensearchserverless as aoss,
    aws_logs as logs,
)
from constructs import Construct

_INFRA_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_LAMBDAS_DIR = os.path.join(_INFRA_ROOT, "lambdas")
_LAYERS_DIR = os.path.join(_INFRA_ROOT, "layers")


class ApiConstruct(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        user_pool,
        agent_user_pool_id: str | None = None,
        table_name: str,
        collection_name: str,
        collection_id: str,
        opensearch_host: str,
        opensearch_index: str,
        video_mapping_table_name: str = "title_video_mapping",
        video_bucket_arns: list[str] | None = None,
        events_table_name: str = "match-events",
        access_policy_name: str = "recs-access",
        create_cloudwatch_role: bool = True,
        api_name: str = "recs-api",
        manage_log_groups: bool = False,
    ) -> None:
        super().__init__(scope, construct_id)

        region = Stack.of(self).region
        account = Stack.of(self).account
        collection_arn = f"arn:aws:aoss:{region}:{account}:collection/{collection_id}"

        def function_logs(name):
            return logs.LogGroup(self, name + "Logs", retention=logs.RetentionDays.ONE_MONTH) if manage_log_groups else None

        # Shared layer: hydrate/normalize helpers (keeps functions DRY).
        shared_layer = _lambda.LayerVersion(
            self,
            "SharedLayer",
            code=_lambda.Code.from_asset(os.path.join(_LAYERS_DIR, "shared")),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="Shared hydrate/normalize helpers for recommendation Lambdas",
        )

        common_env = {
            "TABLE_NAME": table_name,
            "OPENSEARCH_HOST": opensearch_host,
            "OPENSEARCH_INDEX": opensearch_index,
        }

        rec_table = dynamodb.Table.from_table_name(self, "RecTable", table_name)

        def make_reader_fn(fn_id: str, code_dir: str) -> _lambda.Function:
            """Create a single-purpose read Lambda with DynamoDB + OpenSearch access."""
            fn = _lambda.Function(
                self,
                fn_id,
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler="lambda_function.lambda_handler",
                code=_lambda.Code.from_asset(os.path.join(_LAMBDAS_DIR, code_dir)),
                layers=[shared_layer],
                timeout=Duration.seconds(30),
                memory_size=256,
                environment=common_env,
                log_group=function_logs(fn_id),
            )
            rec_table.grant_read_data(fn)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=["aoss:APIAccessAll"],
                    resources=[collection_arn],
                )
            )
            return fn

        top_picks_fn = make_reader_fn("GetTopPicksFn", "get_top_picks")
        carousels_fn = make_reader_fn("GetCarouselsFn", "get_carousels")
        # get_list ("My List") reads the watch_list item + hydrates via OpenSearch,
        # so it shares the reader setup (DynamoDB read + aoss access + shared layer).
        get_list_fn = make_reader_fn("GetListFn", "get_list")

        # --- save_list / remove_from_list: mutate a profile's watch_list -----
        # DynamoDB read+write only (no OpenSearch). Reuse the shared layer for
        # build_pk/respond helpers; TABLE_NAME is the only env they need.
        def make_writer_fn(fn_id: str, code_dir: str) -> _lambda.Function:
            fn = _lambda.Function(
                self,
                fn_id,
                runtime=_lambda.Runtime.PYTHON_3_12,
                handler="lambda_function.lambda_handler",
                code=_lambda.Code.from_asset(os.path.join(_LAMBDAS_DIR, code_dir)),
                layers=[shared_layer],
                timeout=Duration.seconds(15),
                memory_size=256,
                environment={"TABLE_NAME": table_name},
                log_group=function_logs(fn_id),
            )
            rec_table.grant_read_write_data(fn)
            return fn

        save_list_fn = make_writer_fn("SaveListFn", "save_list")
        remove_list_fn = make_writer_fn("RemoveFromListFn", "remove_from_list")

        # --- get_video_url: resolve a presigned S3 URL for VOD playback -----
        # Reads the `title_video_mapping` table and presigns the object. Needs
        # DynamoDB read on that table + S3 GetObject on the video bucket(s).
        video_url_fn = _lambda.Function(
            self,
            "GetVideoUrlFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset(os.path.join(_LAMBDAS_DIR, "get_video_url")),
            layers=[shared_layer],
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={"VIDEO_MAPPING_TABLE": video_mapping_table_name},
            log_group=function_logs("VideoUrl"),
        )
        dynamodb.Table.from_table_name(
            self, "VideoMappingTable", video_mapping_table_name
        ).grant_read_data(video_url_fn)

        # S3 read for presigning. Scope to the configured bucket(s) when known;
        # otherwise fall back to GetObject on any bucket (read-only). The
        # deployment passes the configured content bucket.
        s3_resources = (
            [f"{arn.rstrip('/')}/*" for arn in video_bucket_arns]
            if video_bucket_arns
            else ["arn:aws:s3:::*/*"]
        )
        video_url_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=s3_resources,
            )
        )

        # --- match_state: playhead-bounded live sports state ----------------
        # Self-contained (no shared layer / OpenSearch): the match-state engine
        # reads the match structure and the events from the events table,
        # bounded by the playhead. The data/ copies are not deployed. Serves the
        # sports player's scoreboard (/state), UI setup (/meta), jump-to-goal
        # rail (/highlights), and catch-up reel (/reel).
        match_state_fn = _lambda.Function(
            self,
            "MatchStateFn",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=_lambda.Code.from_asset(os.path.join(_LAMBDAS_DIR, "match_state"),
                                         exclude=["data", "__pycache__", "*.pyc"]),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={"EVENTS_TABLE": events_table_name},
            log_group=function_logs("MatchState"),
        )
        dynamodb.Table.from_table_name(
            self, "EventsTable", events_table_name
        ).grant_read_data(match_state_fn)

        # S3 read so /state can presign player-face circle avatars. Scope to the
        # configured video bucket(s) when known (faces live in the same
        # content bucket); otherwise fall back to read-only GetObject.
        match_state_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=s3_resources,
            )
        )

        # Additive aoss data-access policy for our Lambda roles only.
        # (aoss policies are additive; this does not modify the agent's policy.)
        access_policy_doc = Stack.of(self).to_json_string(
            [
                {
                    "Rules": [
                        {
                            "ResourceType": "collection",
                            "Resource": [f"collection/{collection_name}"],
                            "Permission": ["aoss:DescribeCollectionItems"],
                        },
                        {
                            "ResourceType": "index",
                            "Resource": [f"index/{collection_name}/*"],
                            "Permission": ["aoss:ReadDocument", "aoss:DescribeIndex"],
                        },
                    ],
                    "Principal": [
                        top_picks_fn.role.role_arn,
                        carousels_fn.role.role_arn,
                        get_list_fn.role.role_arn,
                    ],
                }
            ]
        )
        aoss.CfnAccessPolicy(
            self,
            "RecsDataAccessPolicy",
            name=access_policy_name,
            type="data",
            policy=access_policy_doc,
        )

        # --- API Gateway (Cognito-protected) --------------------------------
        api = apigw.RestApi(
            self,
            "RecApi",
            rest_api_name=api_name,
            cloud_watch_role=create_cloudwatch_role,
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["GET", "POST", "PUT", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization"],
            ),
        )

        # Trust the in-stack pool, plus (optionally) the agent-owned pool so a
        # frontend logged into the agent's pool can also call this API.
        user_pools = [user_pool]
        if agent_user_pool_id:
            user_pools.append(
                cognito.UserPool.from_user_pool_id(self, "AgentUserPool", agent_user_pool_id)
            )

        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "Authorizer",
            cognito_user_pools=user_pools,
        )

        def add_method(path: str, verb: str, fn: _lambda.Function) -> None:
            api.root.add_resource(path).add_method(
                verb,
                apigw.LambdaIntegration(fn),
                authorization_type=apigw.AuthorizationType.COGNITO,
                authorizer=authorizer,
            )

        def add_get(path: str, fn: _lambda.Function) -> None:
            add_method(path, "GET", fn)

        def add_post(path: str, fn: _lambda.Function) -> None:
            add_method(path, "POST", fn)

        add_get("get_top_picks", top_picks_fn)
        add_get("get_carousels", carousels_fn)
        add_get("get_video_url", video_url_fn)

        # My List (watch_list): read + mutate.
        add_get("get_list", get_list_fn)
        add_post("save_list", save_list_fn)
        add_post("remove_from_list", remove_list_fn)

        # Sports match-state routes (one Lambda, dispatched by path).
        add_get("state", match_state_fn)
        add_get("meta", match_state_fn)
        add_get("highlights", match_state_fn)
        add_get("reel", match_state_fn)

        self.api = api
        # Exposed so a stack can add routes behind the same authorizer and layer.
        self.authorizer = authorizer
        self.shared_layer = shared_layer
        CfnOutput(self, "ApiUrl", value=api.url)
