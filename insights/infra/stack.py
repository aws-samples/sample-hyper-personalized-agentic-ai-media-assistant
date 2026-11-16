"""Insights stack: a read-only window into the deployed demo.

Owns only new resources (data bucket, layout job, API, site, and one new
OpenSearch data-access policy for its own two roles). Existing demo resources
are referenced by name or id and never modified: the user pool is imported for
the API authorizer, the tables are granted read-only on this stack's roles.
"""
import json
from pathlib import Path

from aws_cdk import (
    Stack, CfnOutput, Duration, RemovalPolicy,
    aws_apigateway as apigw, aws_cloudfront as cf, aws_cloudfront_origins as origins,
    aws_cognito as cognito, aws_dynamodb as dynamodb, aws_ecr_assets as ecr_assets,
    aws_events as events, aws_events_targets as targets, aws_iam as iam, aws_lambda as lambda_,
    aws_logs as logs, aws_opensearchserverless as aoss, aws_s3 as s3, aws_s3_deployment as deploy,
)

ROOT = Path(__file__).resolve().parents[2]
LAYOUT_KEY = "layout/layout.json"


class InsightsStack(Stack):
    def __init__(self, scope, id, *, cfg, ext, settings, web_dir, whatif_dir, **kwargs):
        super().__init__(scope, id, **kwargs)
        prefix = cfg["prefix"]
        collection_id = ext["SearchEndpoint"].split("//", 1)[1].split(".", 1)[0]
        collection_arn = f"arn:aws:aoss:{self.region}:{self.account}:collection/{collection_id}"
        search_host = ext["SearchEndpoint"].split("//", 1)[1]
        tables = {k: dynamodb.Table.from_table_name(self, f"{k.title()}Table", ext[f"{k.title()}TableName"])
                  for k in ["households", "profiles"]}
        tables["interactions"] = dynamodb.Table.from_table_name(self, "InteractionsTable", ext["UserInteractionsTableName"])

        data = s3.Bucket(self, "Data", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)

        # --- layout job: UMAP of the catalog embeddings, hourly, only on change ---
        layout = lambda_.DockerImageFunction(self, "LayoutFn",
            code=lambda_.DockerImageCode.from_image_asset(str(ROOT / "insights/layout"),
                platform=ecr_assets.Platform.LINUX_AMD64),
            architecture=lambda_.Architecture.X86_64, memory_size=3008, timeout=Duration.minutes(15),
            environment={"OPENSEARCH_HOST": search_host, "OPENSEARCH_INDEX": "catalog",
                         "DATA_BUCKET": data.bucket_name, "LAYOUT_KEY": LAYOUT_KEY},
            log_group=logs.LogGroup(self, "LayoutLogs", retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=RemovalPolicy.DESTROY))
        data.grant_read_write(layout)
        events.Rule(self, "LayoutSchedule", schedule=events.Schedule.rate(Duration.hours(1)),
                    targets=[targets.LambdaFunction(layout)])

        # --- API ---------------------------------------------------------------
        site_bucket = s3.Bucket(self, "Site", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)
        site = cf.Distribution(self, "SiteDistribution", default_root_object="index.html",
            default_behavior=cf.BehaviorOptions(origin=origins.S3BucketOrigin.with_origin_access_control(site_bucket),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cf.ResponseHeadersPolicy.SECURITY_HEADERS),
            error_responses=[cf.ErrorResponse(http_status=c, response_http_status=200, response_page_path="/index.html",
                                              ttl=Duration.seconds(0)) for c in [403, 404]])
        origin = f"https://{site.distribution_domain_name}"
        # The console downloads layout.json straight from S3 by presigned URL.
        data.add_cors_rule(allowed_methods=[s3.HttpMethods.GET], allowed_origins=[origin], max_age=3600)

        api_fn = lambda_.Function(self, "ApiFn", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(ROOT / "insights/api"), exclude=["__pycache__", "*.pyc"]),
            memory_size=1024, timeout=Duration.seconds(29),
            environment={"OPENSEARCH_HOST": search_host, "OPENSEARCH_INDEX": "catalog",
                         "DATA_BUCKET": data.bucket_name, "LAYOUT_KEY": LAYOUT_KEY,
                         "HOUSEHOLDS_TABLE": ext["HouseholdsTableName"], "PROFILES_TABLE": ext["ProfilesTableName"],
                         "INTERACTIONS_TABLE": ext["UserInteractionsTableName"],
                         "MEDIA_REGION": self.region, "INSIGHTS_USERS": ",".join(settings["users"]),
                         "ALLOWED_ORIGIN": origin},
            log_group=logs.LogGroup(self, "ApiLogs", retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=RemovalPolicy.DESTROY))
        data.grant_read(api_fn)
        tables["households"].grant(api_fn, "dynamodb:Scan")
        tables["profiles"].grant(api_fn, "dynamodb:GetItem", "dynamodb:Query")
        tables["interactions"].grant(api_fn, "dynamodb:Query")
        api_fn.add_to_role_policy(iam.PolicyStatement(actions=["s3:GetObject"],
                                                      resources=[f'arn:aws:s3:::{cfg["mediaBucket"]}/*']))

        for fn in (layout, api_fn):
            fn.add_to_role_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection_arn]))
        search_readers = [layout.role.role_arn, api_fn.role.role_arn]

        pool = cognito.UserPool.from_user_pool_id(self, "DemoUsers", ext["UserPoolId"])
        authorizer = apigw.CognitoUserPoolsAuthorizer(self, "Authorizer", cognito_user_pools=[pool])
        api = apigw.RestApi(self, "Api", rest_api_name=f"{prefix}-insights",
            default_cors_preflight_options=apigw.CorsOptions(allow_origins=[origin],
                allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type", "Authorization"]),
            deploy_options=apigw.StageOptions(stage_name="prod", throttling_rate_limit=20, throttling_burst_limit=40),
            cloud_watch_role=False)
        for kind in (apigw.ResponseType.DEFAULT_4_XX, apigw.ResponseType.DEFAULT_5_XX):
            api.add_gateway_response(f"Cors{kind.response_type}", type=kind,
                response_headers={"Access-Control-Allow-Origin": f"'{origin}'"})
        integration = apigw.LambdaIntegration(api_fn)
        for path in ["layout", "personas", "taste", "title"]:
            api.root.add_resource(path).add_method("GET", integration,
                authorization_type=apigw.AuthorizationType.COGNITO, authorizer=authorizer)
        whatif_res = api.root.add_resource("whatif")
        whatif_res.add_resource("events").add_method("GET", integration,
            authorization_type=apigw.AuthorizationType.COGNITO, authorizer=authorizer)

        # --- what-if: the demo's own decision code on a sandbox profile ----------
        memory_arn = f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:memory/{ext['MemoryId']}"
        # Progress events of streamed runs (insights-owned, expire after a day).
        runs = dynamodb.Table(self, "RunsTable",
            partition_key=dynamodb.Attribute(name="run_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="seq", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST, time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY)
        whatif_name = f"{prefix}-insights-whatif"
        whatif = lambda_.DockerImageFunction(self, "WhatIfFn", function_name=whatif_name,
            code=lambda_.DockerImageCode.from_image_asset(whatif_dir, platform=ecr_assets.Platform.LINUX_AMD64),
            # 120 s covers the background (streamed) run; the synchronous path is still bounded by API Gateway's 29 s.
            architecture=lambda_.Architecture.X86_64, memory_size=2048, timeout=Duration.seconds(120),
            environment={"OPENSEARCH_HOST": search_host, "OPENSEARCH_INDEX": "catalog",
                         "EMBEDDING_MODEL_ID": cfg["embeddingModel"], "EMBEDDING_REGION": cfg["embeddingRegion"],
                         "V2_MODEL_ID": cfg["models"]["movie"], "V2_SHARD_MODEL_ID": cfg["models"]["movie"],
                         "V2_SHARD_CACHE_TTL": "1h", "MEDIA_REGION": self.region,
                         "DYNAMODB_TABLE": ext["ProfilesTableName"], "PROFILES_TABLE": ext["ProfilesTableName"],
                         "INTERACTIONS_TABLE": ext["UserInteractionsTableName"],
                         "AGENTCORE_MEMORY_ID": ext["MemoryId"], "SKIP_LTM": "true", "PROFILE_EVENTS_STREAM": "",
                         "DATA_BUCKET": data.bucket_name, "LAYOUT_KEY": LAYOUT_KEY,
                         "INSIGHTS_USERS": ",".join(settings["users"]), "ALLOWED_ORIGIN": origin,
                         "RUNS_TABLE": runs.table_name},
            log_group=logs.LogGroup(self, "WhatIfLogs", retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=RemovalPolicy.DESTROY))
        runs.grant(whatif, "dynamodb:PutItem", "dynamodb:BatchWriteItem")
        runs.grant(api_fn, "dynamodb:Query")
        api_fn.add_environment("RUNS_TABLE", runs.table_name)
        # The streamed run is an async invocation of the same function (ARN from its fixed name: no cycle).
        whatif.add_to_role_policy(iam.PolicyStatement(actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{self.region}:{self.account}:function:{whatif_name}"]))
        whatif.configure_async_invoke(retry_attempts=0, max_event_age=Duration.minutes(5))
        data.grant_read(whatif)
        tables["profiles"].grant(whatif, "dynamodb:GetItem", "dynamodb:Query")
        tables["interactions"].grant(whatif, "dynamodb:Query")
        whatif.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{self.account}:inference-profile/*"]))
        whatif.add_to_role_policy(iam.PolicyStatement(actions=["bedrock-agentcore:RetrieveMemoryRecords"],
                                                      resources=[memory_arn]))
        whatif.add_to_role_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection_arn]))
        search_readers.append(whatif.role.role_arn)
        # A cold start (agent + strands imports) takes most of API Gateway's 29 s,
        # so a ping every 5 minutes keeps one container warm.
        events.Rule(self, "WhatIfWarm", schedule=events.Schedule.rate(Duration.minutes(5)),
                    targets=[targets.LambdaFunction(whatif, event=events.RuleTargetInput.from_object({"warmup": True}))])
        whatif_res.add_method("POST", apigw.LambdaIntegration(whatif, timeout=Duration.seconds(29)),
            authorization_type=apigw.AuthorizationType.COGNITO, authorizer=authorizer)

        aoss.CfnAccessPolicy(self, "SearchRead", name=f"{prefix}-insights", type="data",
            description="Insights console: read-only catalog access",
            policy=self.to_json_string([{"Rules": [
                {"ResourceType": "collection", "Resource": [f"collection/{prefix}-search"], "Permission": ["aoss:DescribeCollectionItems"]},
                {"ResourceType": "index", "Resource": [f"index/{prefix}-search/catalog"], "Permission": ["aoss:ReadDocument", "aoss:DescribeIndex"]}],
                "Principal": search_readers}]))

        deploy.BucketDeployment(self, "Publish", destination_bucket=site_bucket, distribution=site,
            distribution_paths=["/*"], memory_limit=512,
            sources=[deploy.Source.asset(web_dir), deploy.Source.json_data("config.json", {
                "region": self.region, "apiUrl": api.url, "userPoolId": ext["UserPoolId"],
                "clientId": ext["UserPoolClientId"], "demoUrl": ext.get("SiteUrl", "")})],
            log_group=logs.LogGroup(self, "PublishLogs", retention=logs.RetentionDays.ONE_MONTH,
                                    removal_policy=RemovalPolicy.DESTROY))

        CfnOutput(self, "InsightsUrl", value=origin)
        CfnOutput(self, "InsightsApiUrl", value=api.url)
        CfnOutput(self, "LayoutFunction", value=layout.function_name)
        CfnOutput(self, "DataBucket", value=data.bucket_name)
