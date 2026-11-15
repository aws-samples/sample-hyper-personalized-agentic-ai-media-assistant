"""Environment-configured resources; external S3 names are references, never constructs.

No source-data loaders, bucket policy updates, imported database mutations, or
Lambda@Edge resources are part of this application.
"""
import ast
import json
import os
import sys
from pathlib import Path

from aws_cdk import (
    Stack, CfnOutput, CfnResource, CustomResource, Duration, RemovalPolicy,
    custom_resources as cr,
    aws_s3 as s3, aws_s3_assets as assets, aws_s3_deployment as deploy,
    aws_ecr as ecr, aws_codebuild as codebuild, aws_iam as iam,
    aws_cognito as cognito, aws_dynamodb as dynamodb,
    aws_opensearchserverless as aoss, aws_bedrockagentcore as agentcore,
    aws_lambda as lambda_, aws_events as events, aws_events_targets as targets,
    aws_logs as logs, aws_cloudfront as cf, aws_cloudfront_origins as origins,
    aws_apigateway as apigw, aws_kinesis as kinesis, aws_kinesisfirehose as firehose,
    aws_sqs as sqs, aws_lambda_event_sources as sources, aws_ssm as ssm,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "infra"))
from stack.stack_constructs.api_constructs import ApiConstruct

AGENTS = {"movie": "movie-ai-agent-v2", "sports": "live-sports-agent", "voice": "movie-voice-agent"}
# Title highlight cuts (optional) in the content bucket: <prefix><orientation>/<base>_NN.mp4.
HIGHLIGHT_PREFIX = "clips/highlights/vod/"


def data_loaders(cfg):
    """IAM principals that may write the search indexes: the deploying role
    (HPM_DEPLOYER_ARN, set by deploy.py) plus cfg dataLoaderPrincipals."""
    found = [os.environ.get("HPM_DEPLOYER_ARN", ""), *cfg.get("dataLoaderPrincipals", [])]
    return list(dict.fromkeys(arn for arn in found if arn))


class BuildStack(Stack):
    def __init__(self, scope, id, *, cfg, **kwargs):
        super().__init__(scope, id, **kwargs)
        source = assets.Asset(self, "AgentSource", path=str(ROOT / "backend"),
            exclude=[".env", "**/.env", "**/.env.*", "**/.venv/**",
                     "**/__pycache__/**", "**/.bedrock_agentcore*", "**/test_harness/**",
                     # Match data is loaded into DynamoDB offline; runtimes never read it.
                     "live-sports-agent/data/**"])
        self.image_tag = source.asset_hash[:24]
        self.repositories = {}
        commands = [
            'aws ecr get-login-password --region "$AWS_DEFAULT_REGION" | docker login --username AWS --password-stdin "$REGISTRY"'
        ]
        for name, directory in AGENTS.items():
            repo = ecr.Repository(self, name.title() + "Image", repository_name=f'{cfg["prefix"]}-{name}',
                image_scan_on_push=True, removal_policy=RemovalPolicy.RETAIN)
            self.repositories[name] = repo
            commands += [f'docker build -t {repo.repository_uri}:{self.image_tag} {directory}',
                         f'docker push {repo.repository_uri}:{self.image_tag}']
        self.project = codebuild.Project(self, "BuildAgents",
            project_name=f'{cfg["prefix"]}-agents',
            source=codebuild.Source.s3(bucket=source.bucket, path=source.s3_object_key),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                compute_type=codebuild.ComputeType.SMALL, privileged=True,
                environment_variables={"REGISTRY": codebuild.BuildEnvironmentVariable(
                    value=f"{self.account}.dkr.ecr.{self.region}.amazonaws.com")}),
            timeout=Duration.minutes(60),
            build_spec=codebuild.BuildSpec.from_object({"version": "0.2", "phases": {"build": {"commands": commands}}}),
            logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(
                log_group=logs.LogGroup(self, "BuildLogs", retention=logs.RetentionDays.ONE_MONTH))))
        for repo in self.repositories.values():
            repo.grant_pull_push(self.project)
        CfnOutput(self, "BuildProject", value=self.project.project_name)
        CfnOutput(self, "ImageTag", value=self.image_tag)


class ApplicationStack(Stack):
    def __init__(self, scope, id, *, cfg, build, **kwargs):
        super().__init__(scope, id, **kwargs)
        prefix = cfg["prefix"]
        runtime_prefix = cfg["runtimePrefix"]
        models = cfg["models"]
        tags = cfg["tags"]
        self.pool = cognito.UserPool(self, "Users", user_pool_name=f"{prefix}-users",
            self_sign_up_enabled=False, sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            removal_policy=RemovalPolicy.RETAIN)
        self.client = self.pool.add_client("Web", generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True))
        # Booth (kiosk) mode: users added to this group share demo logins. The
        # household, profile_settings and profile_events Lambdas make their
        # household and settings read-only and keep their viewing out of the
        # personas' long-term taste. Empty by default (README: booth mode).
        cognito.CfnUserPoolGroup(self, "BoothGroup", user_pool_id=self.pool.user_pool_id, group_name="booth",
            description="Shared demo logins: read-only household and settings, no long-term taste learning")
        tables = {}
        # Unified profile (docs/unified-profile-design.md §5.1): households map a
        # Cognito sub to members; each member's opaque profile_id keys the profile,
        # recommendation-cache and user-interactions items.
        for name, pk, sk, ttl in [("profiles", "user_id", "data_type", None), ("videos", "title_id", None, None),
                                  ("events", "match_id", "event_sk", None), ("commentary", "match_id", "commentary_sk", None),
                                  ("households", "account_id", "sk", None),
                                  ("recommendation-cache", "profile_id", "sk", "expires_at"),
                                  ("user-interactions", "profile_id", "sk", "expires_at")]:
            logical = "".join(part.title() for part in name.split("-"))
            tables[name] = dynamodb.Table(self, logical + "Table", table_name=f"{prefix}-{name}",
                partition_key=dynamodb.Attribute(name=pk, type=dynamodb.AttributeType.STRING),
                sort_key=dynamodb.Attribute(name=sk, type=dynamodb.AttributeType.STRING) if sk else None,
                time_to_live_attribute=ttl,
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST, removal_policy=RemovalPolicy.RETAIN,
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True))
            CfnOutput(self, logical + "TableName", value=tables[name].table_name)

        # All indexes are new and empty. Data import is explicitly owner-managed.
        encryption = aoss.CfnSecurityPolicy(self, "SearchEncryption", name=f"{prefix}-enc", type="encryption",
            policy=json.dumps({"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{prefix}-search"]}], "AWSOwnedKey": True}))
        aoss.CfnSecurityPolicy(self, "SearchNetwork", name=f"{prefix}-net", type="network",
            policy=json.dumps([{"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{prefix}-search"]}], "AllowFromPublic": True}]))
        collection = aoss.CfnCollection(self, "Search", name=f"{prefix}-search", type="VECTORSEARCH", standby_replicas="DISABLED")
        collection.add_resource_dependency(encryption)
        collection.apply_removal_policy(RemovalPolicy.RETAIN)

        def role(name):
            r = iam.Role(self, name, assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"))
            r.add_to_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:InvokeModelWithBidirectionalStream"],
                resources=["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{self.account}:inference-profile/*"]))
            r.add_to_policy(iam.PolicyStatement(actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/*"]))
            return r

        movie_role, sports_role, voice_role = role("MovieRole"), role("SportsRole"), role("VoiceRole")
        memory_role = role("MemoryRole")
        # Read constants without executing the legacy setup script.
        constants = {n.targets[0].id: ast.literal_eval(n.value) for n in ast.parse(
            (ROOT / "backend/movie-ai-agent-v2/setup_ltm_strategy.py").read_text()).body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in {"EXTRACTION_PROMPT", "CONSOLIDATION_PROMPT"}}
        memory = agentcore.CfnMemory(self, "Memory", name=f"{runtime_prefix}_memory", event_expiry_duration=90,
            memory_execution_role_arn=memory_role.role_arn, tags=tags,
            memory_strategies=[])
        memory.add_override("Properties.MemoryStrategies", [
            {"SummaryMemoryStrategy": {"Name": "SessionSummarizer", "Namespaces": ["/summaries/{actorId}/{sessionId}"]}},
            {"CustomMemoryStrategy": {"Name": "TastePreferences", "Namespaces": ["/preferences/{actorId}"],
                "Configuration": {"UserPreferenceOverride": {
                    "Extraction": {"AppendToPrompt": constants["EXTRACTION_PROMPT"], "ModelId": models["memory"]},
                    "Consolidation": {"AppendToPrompt": constants["CONSOLIDATION_PROMPT"], "ModelId": models["memory"]}}}}}])
        memory.node.add_dependency(memory_role)
        movie_role.add_to_policy(iam.PolicyStatement(actions=["bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents", "bedrock-agentcore:GetEvent", "bedrock-agentcore:RetrieveMemoryRecords", "bedrock-agentcore:ListMemoryRecords"], resources=[memory.attr_memory_arn]))
        for name in ["profiles", "videos", "households", "user-interactions"]:
            tables[name].grant_read_data(movie_role)
        for name in ["events", "commentary"]:
            tables[name].grant_read_data(sports_role)

        kb_role = iam.Role(self, "KnowledgeRole", assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"))
        kb_role.add_to_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{models['knowledgeEmbedding']}"]))
        # Bedrock S3 data sources must be regional. Empty staging bucket: the
        # owner controls subsequent import; no source objects are copied here.
        kb_source = s3.Bucket(self, "KnowledgeSource", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True, removal_policy=RemovalPolicy.RETAIN)
        kb_source.grant_read(kb_role)
        CfnOutput(self, "KnowledgeSourceBucket", value=kb_source.bucket_name)
        # Identity-side grants only. No imported bucket or resource-policy mutation.
        # sceneBucket is optional: the deployer's own scene-analysis images.
        movie_role.add_to_policy(iam.PolicyStatement(actions=["s3:GetObject"],
            resources=[f'arn:aws:s3:::{b}/*' for b in (cfg["mediaBucket"], cfg["sceneBucket"]) if b]))
        movie_role.add_to_policy(iam.PolicyStatement(actions=["rekognition:RecognizeCelebrities"], resources=["*"]))
        for r in [movie_role, kb_role]:
            r.add_to_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection.attr_arn]))
        index_handler = lambda_.Function(self, "IndexHandler", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler", code=lambda_.Code.from_asset(str(ROOT / "infra/deployment/index_provider"), exclude=["__pycache__", "*.pyc"]),
            timeout=Duration.minutes(10),
            environment={"COLLECTION_ENDPOINT": collection.attr_collection_endpoint},
            log_group=logs.LogGroup(self, "IndexLogs", retention=logs.RetentionDays.ONE_MONTH))
        index_handler.add_to_role_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection.attr_arn]))
        index_provider = cr.Provider(self, "IndexProvider", on_event_handler=index_handler,
            log_group=logs.LogGroup(self, "IndexProviderLogs", retention=logs.RetentionDays.ONE_MONTH))
        search_access = aoss.CfnAccessPolicy(self, "SearchAccess", name=f"{prefix}-owner", type="data",
            policy=self.to_json_string([{"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{prefix}-search"], "Permission": ["aoss:DescribeCollectionItems"]},
                {"ResourceType": "index", "Resource": [f"index/{prefix}-search/*"], "Permission": ["aoss:CreateIndex", "aoss:UpdateIndex", "aoss:DeleteIndex", "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument"]}],
                # Data loaders (load_catalog.py) bulk-index the catalog as the
                # deploying principal: deploy.py passes it in, the config may add more.
                "Principal": [movie_role.role_arn, kb_role.role_arn, index_handler.role.role_arn,
                    f'arn:aws:iam::{self.account}:role/cdk-{cfg["qualifier"]}-cfn-exec-role-{self.account}-{self.region}',
                    *data_loaders(cfg)]}]))

        def index(name, properties):
            # Native CloudFormation Index currently excludes float/long types.
            def mapping(value):
                keys = {"SpaceType": "space_type"}
                return {keys.get(k, k.lower()): mapping(v) if isinstance(v, dict) else v for k, v in value.items()}
            resource = CustomResource(self, name.title().replace("-", "") + "Index",
                service_token=index_provider.service_token,
                properties={"IndexName": name, "Mappings": {k: mapping(v) for k, v in properties.items()}})
            resource.node.add_dependency(search_access)
            return resource

        vector = {"Type": "knn_vector", "Dimension": 1024,
                  "Method": {"Engine": "faiss", "Name": "hnsw", "SpaceType": "cosinesimil"}}
        # Every catalog field is mapped explicitly so nothing falls back to dynamic mapping.
        catalog_fields = {name: {"Type": "text"} for name in ["originalTitle", "plot", "title_text", "principalCastMembers", "principalCrewMembers", "genres", "image", "imdb_url"]}
        catalog_fields.update({name: {"Type": "keyword"} for name in ["title_id", "title_type", "mpaa_ratings", "awards", "keywords"]})
        catalog_fields.update({"embedding": vector, "premiere_year": {"Type": "integer"}, "imdb_rating": {"Type": "float"},
                               # YYYYMMDD, as stored in the source catalog; a real date enables recency range queries.
                               "premiere_date": {"Type": "date", "Format": "basic_date"}})
        index("catalog", catalog_fields)
        for name in ["chapters", "segments", "audio-segments"]:
            index(name, {"video_name": {"Type": "keyword"}, "chapter": {"Type": "integer"},
                         "start_timestamp_millis": {"Type": "long"}, "end_timestamp_millis": {"Type": "long"}})
        kb_index = index("scripts", {"vector": vector, "text": {"Type": "text"}, "metadata": {"Type": "text", "Index": False}})
        kb = CfnResource(self, "KnowledgeBase", type="AWS::Bedrock::KnowledgeBase", properties={
            "Name": f"{prefix}-scripts", "RoleArn": kb_role.role_arn, "Tags": tags,
            "KnowledgeBaseConfiguration": {"Type": "VECTOR", "VectorKnowledgeBaseConfiguration": {
                "EmbeddingModelArn": f"arn:aws:bedrock:{self.region}::foundation-model/{models['knowledgeEmbedding']}",
                "EmbeddingModelConfiguration": {"BedrockEmbeddingModelConfiguration": {"Dimensions": 1024}}}},
            "StorageConfiguration": {"Type": "OPENSEARCH_SERVERLESS", "OpensearchServerlessConfiguration": {
                "CollectionArn": collection.attr_arn, "VectorIndexName": "scripts", "FieldMapping": {"VectorField": "vector", "TextField": "text", "MetadataField": "metadata"}}}})
        kb.node.add_dependency(kb_index)
        kb.node.add_dependency(kb_role)
        CfnResource(self, "ScriptDataSource", type="AWS::Bedrock::DataSource", properties={
            "Name": f"{prefix}-scripts", "KnowledgeBaseId": kb.ref, "DataDeletionPolicy": "RETAIN",
            "DataSourceConfiguration": {"Type": "S3", "S3Configuration": {"BucketArn": kb_source.bucket_arn, "InclusionPrefixes": [cfg["scriptsPrefix"]]}}})
        movie_role.add_to_policy(iam.PolicyStatement(actions=["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"], resources=[f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/{kb.ref}"]))
        # RetrieveAndGenerate resolves the story model's inference profile itself (read-only).
        movie_role.add_to_policy(iam.PolicyStatement(actions=["bedrock:GetInferenceProfile"],
            resources=[f"arn:aws:bedrock:*:{self.account}:inference-profile/{models['scene']}"]))
        movie_role.add_to_policy(iam.PolicyStatement(actions=["bedrock:Rerank"], resources=["*"]))

        gateway_role = iam.Role(self, "GatewayRole", assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"))
        web_search = lambda_.Function(self, "WebGrounding", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler", code=lambda_.Code.from_asset(str(ROOT / "infra/deployment/web_grounding"), exclude=["__pycache__", "*.pyc"]),
            environment={"MODEL_ID": models["grounding"], "MODEL_REGION": cfg["groundingRegion"]},
            timeout=Duration.seconds(120), memory_size=256,
            log_group=logs.LogGroup(self, "WebGroundingLogs", retention=logs.RetentionDays.ONE_MONTH))
        web_search.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel"],
            resources=[f"arn:aws:bedrock:*::foundation-model/{models['groundingFoundation']}", f"arn:aws:bedrock:{cfg['groundingRegion']}:{self.account}:inference-profile/{models['grounding']}"]))
        web_search.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:InvokeTool"],
            resources=[f"arn:aws:bedrock::{self.account}:system-tool/amazon.nova_grounding"]))
        web_search.grant_invoke(gateway_role)
        gateway = agentcore.CfnGateway(self, "WebSearch", name=f"{prefix}-websearch", protocol_type="MCP", authorizer_type="AWS_IAM", role_arn=gateway_role.role_arn, tags=tags)
        gateway.node.add_dependency(gateway_role)
        CfnResource(self, "WebSearchTarget", type="AWS::BedrockAgentCore::GatewayTarget", properties={
            "GatewayIdentifier": gateway.attr_gateway_identifier, "Name": "web-search-tool",
            "CredentialProviderConfigurations": [{"CredentialProviderType": "GATEWAY_IAM_ROLE"}],
            "TargetConfiguration": {"Mcp": {"Lambda": {"LambdaArn": web_search.function_arn,
                "ToolSchema": {"InlinePayload": [{"Name": "WebSearch", "Description": "Search the web for current football knowledge with source citations.",
                    "InputSchema": {"Type": "object", "Properties": {"query": {"Type": "string"}, "maxResults": {"Type": "integer"}}, "Required": ["query"]}}]}}}}})
        sports_role.add_to_policy(iam.PolicyStatement(actions=["bedrock-agentcore:InvokeGateway"], resources=[gateway.attr_gateway_arn]))

        runtime_logs = lambda_.Function(self, "RuntimeLogSettings", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler", code=lambda_.Code.from_asset(str(ROOT / "infra/deployment/runtime_logs"), exclude=["__pycache__", "*.pyc"]),
            environment={"RUNTIME_PREFIX": runtime_prefix},
            timeout=Duration.seconds(60),
            log_group=logs.LogGroup(self, "RuntimeLogSettingsLogs", retention=logs.RetentionDays.ONE_MONTH))
        runtime_logs.add_to_role_policy(iam.PolicyStatement(actions=["logs:CreateLogGroup", "logs:PutRetentionPolicy", "logs:TagResource"],
            resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/{runtime_prefix}_*", f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/runtimes/{runtime_prefix}_*:*"]))
        log_provider = cr.Provider(self, "RuntimeLogProvider", on_event_handler=runtime_logs,
            log_group=logs.LogGroup(self, "RuntimeLogProviderLogs", retention=logs.RetentionDays.ONE_MONTH))

        def runtime(name, r, variables):
            repo = build.repositories[name]
            repo.grant_pull(r)
            digest = os.environ.get(f"HPM_{name.upper()}_IMAGE_DIGEST")
            image_uri = repo.repository_uri_for_digest(digest) if digest else repo.repository_uri_for_tag(build.image_tag)
            rt = agentcore.CfnRuntime(self, name.title() + "Runtime", agent_runtime_name=f"{runtime_prefix}_{name}",
                role_arn=r.role_arn, tags=tags,
                agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(container_uri=image_uri)),
                network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(network_mode="PUBLIC"), protocol_configuration="HTTP",
                authorizer_configuration=agentcore.CfnRuntime.AuthorizerConfigurationProperty(custom_jwt_authorizer=agentcore.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(discovery_url=f"https://cognito-idp.{self.region}.amazonaws.com/{self.pool.user_pool_id}/.well-known/openid-configuration", allowed_clients=[self.client.user_pool_client_id])),
                request_header_configuration=agentcore.CfnRuntime.RequestHeaderConfigurationProperty(request_header_allowlist=["Authorization"]),
                environment_variables={"AWS_REGION": self.region, "AWS_DEFAULT_REGION": self.region, **variables})
            # Supported by the live CloudFormation schema; CDK 2.270.0 has
            # not yet generated its typed property for this platform setting.
            rt.add_property_override("PlatformVersion", cfg["runtimePlatformVersion"])
            rt.node.add_dependency(r)
            CustomResource(self, name.title() + "RuntimeLogSettings", service_token=log_provider.service_token,
                properties={"RuntimeId": rt.attr_agent_runtime_id, "Account": self.account, "Tags": tags})
            CfnOutput(self, name.title() + "RuntimeArn", value=rt.attr_agent_runtime_arn)
            CfnOutput(self, name.title() + "RoleArn", value=r.role_arn)
            return rt

        from aws_cdk import Fn
        host = Fn.select(1, Fn.split("https://", collection.attr_collection_endpoint))
        movie = runtime("movie", movie_role, {"OPENSEARCH_HOST": host, "OPENSEARCH_INDEX": "catalog",
            "EMBEDDING_MODEL_ID": cfg["embeddingModel"], "EMBEDDING_REGION": cfg["embeddingRegion"],
            "DYNAMODB_TABLE": tables["profiles"].table_name, "VIDEO_MAPPING_TABLE": tables["videos"].table_name,
            "HOUSEHOLDS_TABLE": tables["households"].table_name,
            "INTERACTIONS_TABLE": tables["user-interactions"].table_name,
            # Stream created in _events_pipeline; the name is fixed, so no reference cycle.
            "PROFILE_EVENTS_STREAM": f"{prefix}-profile-events",
            "AGENTCORE_MEMORY_ID": memory.attr_memory_id, "ANALYSIS_OSS_HOST": host,
            "BEDROCK_KNOWLEDGE_BASE_ID": kb.ref, "BDA_VIDEO_CHAPTER_INDEX_NAME": "chapters",
            "BDA_VIDEO_CHAPTER_SEGMENT_INDEX_NAME": "segments", "BDA_VIDEO_CHAPTER_AUDIO_SEGMENT_INDEX_NAME": "audio-segments",
            "SCENE_IMAGE_S3_BUCKET": cfg["sceneBucket"], "SCENE_IMAGE_REGION": cfg.get("sceneRegion") or self.region,
            "MEDIA_REGION": self.region, "MOVIE_SCENE_UNDERSTANDING_MODEL_ID": models["scene"],
            "V2_MODEL_ID": models["movie"], "V2_SHARD_MODEL_ID": models["movie"], "V2_SHARD_CACHE_TTL": "1h",
            # Flush print() logs to CloudWatch immediately instead of minutes later.
            "PYTHONUNBUFFERED": "1"})
        runtime("sports", sports_role, {"EVENTS_TABLE": tables["events"].table_name, "COMMENTARY_TABLE": tables["commentary"].table_name,
            "WEBSEARCH_GATEWAY_URL": gateway.attr_gateway_url, "WEBSEARCH_REGION": self.region, "WEBSEARCH_TIMEOUT_SECONDS": "125",
            "MODEL_ID": models["sports"]})
        runtime("voice", voice_role, {"MOVIE_AGENT_ARN": movie.attr_agent_runtime_arn, "MOVIE_AGENT_REGION": self.region,
            "BEDROCK_REGION": self.region, "MODEL_ID": models["voice"]})

        api = ApiConstruct(self, "Api", user_pool=self.pool, table_name=tables["profiles"].table_name,
            collection_name=collection.name, collection_id=collection.attr_id, opensearch_host=host, opensearch_index="catalog",
            video_mapping_table_name=tables["videos"].table_name, video_bucket_arns=[f'arn:aws:s3:::{cfg["mediaBucket"]}'],
            events_table_name=tables["events"].table_name,
            access_policy_name=f"{prefix}-api", create_cloudwatch_role=False, api_name=f"{prefix}-api", manage_log_groups=True)
        # The content bucket (cfg mediaBucket) lives in the deployment region.
        for name in ["GetVideoUrlFn", "MatchStateFn"]:
            api.node.find_child(name).add_environment("MEDIA_REGION", self.region)
        api.node.find_child("GetVideoUrlFn").add_environment("HIGHLIGHT_BUCKET", cfg["mediaBucket"])
        api.node.find_child("GetVideoUrlFn").add_environment("HIGHLIGHT_PREFIX", HIGHLIGHT_PREFIX)
        # {title_id: clip base name}. Tears of Steel's cuts come from notebooks/tears-of-steel/;
        # until they are uploaded the Lambda finds no clip and the title shows its poster.
        api.node.find_child("GetVideoUrlFn").add_environment(
            "VOD_HIGHLIGHTS", json.dumps(cfg.get("vodHighlights", {"tt2285752": "tears-of-steel"})))
        # Highlight selection: list the title's clips, read each clip's
        # description metadata, embed it and compare with the profile's taste.
        video_url_fn = api.node.find_child("GetVideoUrlFn")
        for k, v in {"PROFILES_TABLE": tables["profiles"].table_name, "HOUSEHOLDS_TABLE": tables["households"].table_name,
                     "EMBEDDING_MODEL_ID": cfg["embeddingModel"], "EMBEDDING_REGION": cfg["embeddingRegion"]}.items():
            video_url_fn.add_environment(k, v)
        tables["profiles"].grant_read_data(video_url_fn)
        tables["households"].grant_read_data(video_url_fn)
        video_url_fn.add_to_role_policy(iam.PolicyStatement(actions=["s3:ListBucket"],
            resources=[f'arn:aws:s3:::{cfg["mediaBucket"]}'],
            conditions={"StringLike": {"s3:prefix": [HIGHLIGHT_PREFIX + "*"]}}))
        video_url_fn.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel"],
            resources=[f'arn:aws:bedrock:{cfg["embeddingRegion"]}::foundation-model/{cfg["embeddingModel"]}']))
        # My List accepts a household member's profile_id after a membership check.
        for name in ["GetListFn", "SaveListFn", "RemoveFromListFn"]:
            fn = api.node.find_child(name)
            fn.add_environment("HOUSEHOLDS_TABLE", tables["households"].table_name)
            tables["households"].grant_read_data(fn)
        # Catalog readers hydrate titles with presigned s3://<mediaBucket>/posters/* images.
        posters = iam.PolicyStatement(actions=["s3:GetObject"], resources=[f'arn:aws:s3:::{cfg["mediaBucket"]}/posters/*'])
        for name in ["GetTopPicksFn", "GetCarouselsFn", "GetListFn"]:
            api.node.find_child(name).add_to_role_policy(posters)
        # The synthetic match ships no player faces, and its key-moment stills are
        # served with the frontend (SOCCER_THUMB_BASE_URL), so MatchStateFn needs no S3.

        # GET /household + GET /home. The default household seed ships as an
        # asset; the Lambda copies it per member at the caller's first sign-in.
        seed = assets.Asset(self, "HouseholdSeed", path=str(ROOT / "infra/deployment/seed/default_household.json"))
        household = lambda_.Function(self, "HouseholdFn", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset(str(ROOT / "infra/lambdas/household"), exclude=["__pycache__", "*.pyc"]),
            layers=[api.shared_layer], timeout=Duration.seconds(30), memory_size=256,
            environment={"HOUSEHOLDS_TABLE": tables["households"].table_name, "PROFILES_TABLE": tables["profiles"].table_name,
                         "REC_CACHE_TABLE": tables["recommendation-cache"].table_name,
                         "INTERACTIONS_TABLE": tables["user-interactions"].table_name,
                         "SEED_BUCKET": seed.s3_bucket_name, "SEED_KEY": seed.s3_object_key,
                         # seed | personalize | connect: which Home rows GET /home serves (household_core.SOURCE_PREFIXES).
                         "HOME_RAILS_SOURCE": cfg.get("homeRailsSource", "seed"),
                         "OPENSEARCH_HOST": host, "OPENSEARCH_INDEX": "catalog"},
            log_group=logs.LogGroup(self, "HouseholdLogs", retention=logs.RetentionDays.ONE_MONTH))
        seed.grant_read(household)
        tables["households"].grant_read_write_data(household)
        tables["recommendation-cache"].grant_read_write_data(household)
        # profiles: read L3 + L2 for cold-start Home rows; write for provisioning.
        tables["profiles"].grant_read_write_data(household)
        tables["user-interactions"].grant_write_data(household)
        household.add_to_role_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection.attr_arn]))
        household.add_to_role_policy(posters)
        household.add_environment("MEDIA_REGION", self.region)
        aoss.CfnAccessPolicy(self, "HouseholdSearchAccess", name=f"{prefix}-home", type="data",
            policy=self.to_json_string([{"Rules": [
                {"ResourceType": "collection", "Resource": [f"collection/{prefix}-search"], "Permission": ["aoss:DescribeCollectionItems"]},
                {"ResourceType": "index", "Resource": [f"index/{prefix}-search/catalog"], "Permission": ["aoss:ReadDocument", "aoss:DescribeIndex"]}],
                "Principal": [household.role.role_arn]}]))
        routes = {}
        for path in ["household", "home"]:
            routes[path] = api.api.root.add_resource(path)
            routes[path].add_method("GET", apigw.LambdaIntegration(household),
                authorization_type=apigw.AuthorizationType.COGNITO, authorizer=api.authorizer)
        # POST /household/members: "Add profile". The new member's onboarding
        # then writes L3 through the Settings API.
        routes["household"].add_resource("members").add_method("POST", apigw.LambdaIntegration(household),
            authorization_type=apigw.AuthorizationType.COGNITO, authorizer=api.authorizer)
        # POST /household/members/{profile_id}/remove | /restore: soft removal
        # (data kept) and its undo, from the profile screen's Edit mode.
        member = routes["household"].get_resource("members").add_resource("{profile_id}")
        for action in ("remove", "restore"):
            member.add_resource(action).add_method("POST", apigw.LambdaIntegration(household),
                authorization_type=apigw.AuthorizationType.COGNITO, authorizer=api.authorizer)

        self._clips_feed(prefix, cfg, tables, api, host, collection)
        stream = self._events_pipeline(prefix, tables, api, host, collection, movie_role)
        self._profile_settings(prefix, tables, api, stream)
        self._media_session(prefix, api)

        warmer = lambda_.Function(self, "Warmer", runtime=lambda_.Runtime.PYTHON_3_12, handler="handler.handler",
            code=lambda_.Code.from_asset(str(ROOT / "backend/shard-warmer")), timeout=Duration.seconds(120), memory_size=512,
            log_group=logs.LogGroup(self, "WarmerLogs", retention=logs.RetentionDays.ONE_MONTH),
            environment={"SHARD_MODEL_ID": models["movie"], "SHARD_CACHE_TTL": "1h"})
        warmer.add_to_role_policy(iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{self.account}:inference-profile/*"]))
        events.Rule(self, "WarmSchedule", schedule=events.Schedule.rate(Duration.minutes(50)), enabled=cfg["enableWarmer"], targets=[targets.LambdaFunction(warmer)])
        for name, value in {"UserPoolId": self.pool.user_pool_id, "UserPoolClientId": self.client.user_pool_client_id,
            "ApiUrl": api.api.url, "SearchEndpoint": collection.attr_collection_endpoint, "KnowledgeBaseId": kb.ref,
            "KnowledgeRoleArn": kb_role.role_arn, "MemoryId": memory.attr_memory_id, "WebSearchUrl": gateway.attr_gateway_url}.items():
            CfnOutput(self, name, value=value)

    def _clips_feed(self, prefix, cfg, tables, api, host, collection):
        """POST /clips/session + /clips/refill (Cognito): the personalized clips
        feed over the catalog (microdramas + poster cards), with SESSION#clips
        in user-interactions."""
        fn = lambda_.Function(self, "ClipsFeedFn", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset(str(ROOT / "infra/lambdas/clips_feed"), exclude=["__pycache__", "*.pyc"]),
            layers=[api.shared_layer], timeout=Duration.seconds(20),
            # 1769 MB is one full vCPU: the ranking is pure-Python vector math,
            # which at 512 MB (~0.3 vCPU) took about a second per refill.
            memory_size=1769,
            environment={"HOUSEHOLDS_TABLE": tables["households"].table_name,
                         "PROFILES_TABLE": tables["profiles"].table_name,
                         "INTERACTIONS_TABLE": tables["user-interactions"].table_name,
                         "VIDEO_MAPPING_TABLE": tables["videos"].table_name,
                         "OPENSEARCH_HOST": host, "OPENSEARCH_INDEX": "catalog",
                         "MEDIA_REGION": self.region,
                         # Micro-dramas are uploaded by load_media.py under clips/microdramas/.
                         "CLIP_MEDIA_PREFIX": f's3://{cfg["mediaBucket"]}/clips/'},
            log_group=logs.LogGroup(self, "ClipsFeedLogs", retention=logs.RetentionDays.ONE_MONTH))
        tables["households"].grant(fn, "dynamodb:GetItem")
        tables["profiles"].grant(fn, "dynamodb:GetItem")
        tables["videos"].grant(fn, "dynamodb:GetItem")
        # SESSION#clips read/write and the member's TITLE# watch state (query).
        tables["user-interactions"].grant(fn, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query")
        # Presigned posters and microdrama videos (read-only).
        fn.add_to_role_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=[f'arn:aws:s3:::{cfg["mediaBucket"]}/*']))
        fn.add_to_role_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection.attr_arn]))
        aoss.CfnAccessPolicy(self, "ClipsFeedSearchAccess", name=f"{prefix}-clips", type="data",
            policy=self.to_json_string([{"Rules": [
                {"ResourceType": "collection", "Resource": [f"collection/{prefix}-search"], "Permission": ["aoss:DescribeCollectionItems"]},
                {"ResourceType": "index", "Resource": [f"index/{prefix}-search/catalog"], "Permission": ["aoss:ReadDocument", "aoss:DescribeIndex"]}],
                "Principal": [fn.role.role_arn]}]))
        clips = api.api.root.add_resource("clips")
        for path in ["session", "refill"]:
            clips.add_resource(path).add_method("POST", apigw.LambdaIntegration(fn),
                authorization_type=apigw.AuthorizationType.COGNITO, authorizer=api.authorizer)
        return fn

    def _events_pipeline(self, prefix, tables, api, host, collection, movie_role):
        """Behavioral pipeline (docs/unified-profile-design.md §7.2-7.4):
        POST /events -> Kinesis profile-events -> processor -> SESSION#behavior (L1),
        AFFINITIES (L2) and TITLE# watch state, with Firehose copying every
        event to the S3 event log."""
        code = lambda_.Code.from_asset(str(ROOT / "infra/lambdas/profile_events"), exclude=["__pycache__", "*.pyc"])
        stream = kinesis.Stream(self, "ProfileEvents", stream_name=f"{prefix}-profile-events",
            stream_mode=kinesis.StreamMode.ON_DEMAND, retention_period=Duration.days(7),
            encryption=kinesis.StreamEncryption.MANAGED)

        # Raw event log: events/dt=YYYY-MM-DD/, GZIP JSON lines, 90 days Standard,
        # then Glacier Instant Retrieval, deleted after 2 years.
        log_bucket = s3.Bucket(self, "ProfileEventLog", block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True, removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(730), transitions=[
                s3.Transition(storage_class=s3.StorageClass.GLACIER_INSTANT_RETRIEVAL, transition_after=Duration.days(90))])])
        firehose_role = iam.Role(self, "ProfileEventLogRole", assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"))
        stream.grant_read(firehose_role)
        # Firehose's documented S3 actions, on this stack's own log bucket only (no delete).
        firehose_role.add_to_policy(iam.PolicyStatement(actions=["s3:AbortMultipartUpload", "s3:GetBucketLocation",
            "s3:GetObject", "s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:PutObject"],
            resources=[log_bucket.bucket_arn, log_bucket.arn_for_objects("*")]))
        firehose_logs = logs.LogGroup(self, "ProfileEventLogDelivery", retention=logs.RetentionDays.ONE_MONTH)
        firehose_logs.grant_write(firehose_role)
        delivery = firehose.CfnDeliveryStream(self, "ProfileEventsToS3", delivery_stream_name=f"{prefix}-profile-events-to-s3",
            delivery_stream_type="KinesisStreamAsSource",
            kinesis_stream_source_configuration=firehose.CfnDeliveryStream.KinesisStreamSourceConfigurationProperty(
                kinesis_stream_arn=stream.stream_arn, role_arn=firehose_role.role_arn),
            extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                bucket_arn=log_bucket.bucket_arn, role_arn=firehose_role.role_arn, compression_format="GZIP",
                prefix="events/dt=!{timestamp:yyyy-MM-dd}/",
                error_output_prefix="errors/!{firehose:error-output-type}/dt=!{timestamp:yyyy-MM-dd}/",
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(interval_in_seconds=60, size_in_m_bs=64),
                cloud_watch_logging_options=firehose.CfnDeliveryStream.CloudWatchLoggingOptionsProperty(
                    enabled=True, log_group_name=firehose_logs.log_group_name, log_stream_name="delivery")))
        delivery.node.add_dependency(firehose_role)

        # POST /events (Cognito). Membership + consent come from the caller's household.
        ingest = lambda_.Function(self, "IngestEventsFn", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="ingest.lambda_handler", code=code, timeout=Duration.seconds(10), memory_size=256,
            environment={"PROFILE_EVENTS_STREAM": stream.stream_name,
                         "HOUSEHOLDS_TABLE": tables["households"].table_name,
                         "PROFILES_TABLE": tables["profiles"].table_name},
            log_group=logs.LogGroup(self, "IngestEventsLogs", retention=logs.RetentionDays.ONE_MONTH))
        stream.grant_write(ingest)
        for name in ["households", "profiles"]:
            tables[name].grant_read_data(ingest)
        api.api.root.add_resource("events").add_method("POST", apigw.LambdaIntegration(ingest),
            authorization_type=apigw.AuthorizationType.COGNITO, authorizer=api.authorizer)

        # Stream processor: per-profile ordering (parallelization 1), partial
        # batch failures, failed batches to a DLQ after 3 retries.
        dlq = sqs.Queue(self, "ProfileEventsDlq", queue_name=f"{prefix}-profile-events-dlq",
            retention_period=Duration.days(14), encryption=sqs.QueueEncryption.SQS_MANAGED, enforce_ssl=True)
        processor = lambda_.Function(self, "ProfileEventsProcessorFn", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="processor.lambda_handler", code=code, layers=[api.shared_layer],
            timeout=Duration.seconds(60), memory_size=512,
            environment={"INTERACTIONS_TABLE": tables["user-interactions"].table_name,
                         "PROFILES_TABLE": tables["profiles"].table_name,
                         "OPENSEARCH_HOST": host, "OPENSEARCH_INDEX": "catalog"},
            log_group=logs.LogGroup(self, "ProfileEventsProcessorLogs", retention=logs.RetentionDays.ONE_MONTH))
        tables["user-interactions"].grant_read_write_data(processor)
        # L2: read-modify-write of the AFFINITIES item only (code path); no delete, no batch.
        tables["profiles"].grant(processor, "dynamodb:GetItem", "dynamodb:PutItem")
        processor.add_to_role_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection.attr_arn]))
        aoss.CfnAccessPolicy(self, "ProcessorSearchAccess", name=f"{prefix}-events", type="data",
            policy=self.to_json_string([{"Rules": [
                {"ResourceType": "collection", "Resource": [f"collection/{prefix}-search"], "Permission": ["aoss:DescribeCollectionItems"]},
                {"ResourceType": "index", "Resource": [f"index/{prefix}-search/catalog"], "Permission": ["aoss:ReadDocument", "aoss:DescribeIndex"]}],
                "Principal": [processor.role.role_arn]}]))
        processor.add_event_source(sources.KinesisEventSource(stream,
            starting_position=lambda_.StartingPosition.LATEST, batch_size=200,
            max_batching_window=Duration.seconds(2), report_batch_item_failures=True,
            bisect_batch_on_error=True, retry_attempts=3, parallelization_factor=1,
            on_failure=sources.SqsDlq(dlq)))

        # The movie agent emits `impression` for the titles in each response.
        stream.grant_write(movie_role)
        CfnOutput(self, "ProfileEventsStream", value=stream.stream_name)
        CfnOutput(self, "ProfileEventLogBucket", value=log_bucket.bucket_name)
        CfnOutput(self, "ProfileEventsDlqUrl", value=dlq.queue_url)
        return stream

    def _profile_settings(self, prefix, tables, api, stream):
        """GET / PUT /profile/declared (Cognito): the Settings API, the only
        writer of a member's L3 after the household seed (design §12)."""
        fn = lambda_.Function(self, "ProfileSettingsFn", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset(str(ROOT / "infra/lambdas/profile_settings"), exclude=["__pycache__", "*.pyc"]),
            layers=[api.shared_layer], timeout=Duration.seconds(15), memory_size=256,
            environment={"HOUSEHOLDS_TABLE": tables["households"].table_name,
                         "PROFILES_TABLE": tables["profiles"].table_name,
                         "PROFILE_EVENTS_STREAM": stream.stream_name},
            log_group=logs.LogGroup(self, "ProfileSettingsLogs", retention=logs.RetentionDays.ONE_MONTH))
        tables["households"].grant(fn, "dynamodb:GetItem")
        # Single-item read and conditional put; no delete, query or batch.
        tables["profiles"].grant(fn, "dynamodb:GetItem", "dynamodb:PutItem")
        stream.grant_write(fn)
        # Genres are validated against the static genres.json in the asset: no catalog access.
        declared = api.api.root.add_resource("profile").add_resource("declared")
        for method in ["GET", "PUT"]:
            declared.add_method(method, apigw.LambdaIntegration(fn),
                authorization_type=apigw.AuthorizationType.COGNITO, authorizer=api.authorizer)
        # GET /profile/entities?q=: follow-name type-ahead over the static registry.
        declared.parent_resource.add_resource("entities").add_method("GET", apigw.LambdaIntegration(fn),
            authorization_type=apigw.AuthorizationType.COGNITO, authorizer=api.authorizer)
        return fn

    def _media_session(self, prefix, api):
        """GET /media/session (Cognito): signed cookies for the app's /media/*
        path. The key is owner-managed (media_signing_key.py), so the secret is
        referenced by name, not created here."""
        secret = f"{prefix}/media-signing-key"
        fn = lambda_.Function(self, "MediaSessionFn", runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset(str(ROOT / "infra/lambdas/media_session"), exclude=["__pycache__", "*.pyc"]),
            layers=[api.shared_layer], timeout=Duration.seconds(10), memory_size=256,
            environment={"MEDIA_KEY_SECRET": secret, "MEDIA_COOKIE_TTL_S": "14400"},
            log_group=logs.LogGroup(self, "MediaSessionLogs", retention=logs.RetentionDays.ONE_MONTH))
        fn.add_to_role_policy(iam.PolicyStatement(actions=["secretsmanager:GetSecretValue"],
            resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{secret}-*"]))
        api.api.root.add_resource("media").add_resource("session").add_method("GET", apigw.LambdaIntegration(fn),
            authorization_type=apigw.AuthorizationType.COGNITO, authorizer=api.authorizer)
        return fn


# CloudFront Function (viewer request, default behavior only): serve the SPA
# shell for app routes. A path whose last segment has no "." is a client route;
# anything with an extension is a real file and goes to S3 unchanged.
SPA_ROUTES_JS = """function handler(event) {
  var request = event.request;
  var last = request.uri.split('/').pop();
  if (last.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}"""


class FrontendStack(Stack):
    def __init__(self, scope, id, *, cfg, **kwargs):
        super().__init__(scope, id, **kwargs)
        bucket = s3.Bucket(self, "Site", encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True, removal_policy=RemovalPolicy.RETAIN)
        # Live-sports video (media/soccer/...), uploaded by load_media.py. Private; only this
        # distribution reads it (OAC), and only for viewers holding the signed
        # cookies GET /media/session issues.
        media = s3.Bucket(self, "Media", encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True, removal_policy=RemovalPolicy.RETAIN)
        key_id = ssm.StringParameter.value_for_string_parameter(self, f'/{cfg["prefix"]}/media-signing/key-pair-id')
        key_group = cf.KeyGroup(self, "MediaKeyGroup", key_group_name=f'{cfg["prefix"]}-media',
            items=[cf.PublicKey.from_public_key_id(self, "MediaPublicKey", key_id)])
        # SPA routing: app routes (/home, /title/tt123 ...) have no file
        # extension, so they are rewritten to /index.html at the viewer edge.
        # Only the default behavior runs this, and there are no distribution
        # error pages, so a refused /media/* request (no signed cookies) and a
        # missing asset return their real 403/404 instead of index.html.
        spa_routes = cf.Function(self, "SpaRoutes", runtime=cf.FunctionRuntime.JS_2_0,
            comment="Rewrite extensionless app routes to /index.html",
            code=cf.FunctionCode.from_inline(SPA_ROUTES_JS))
        distribution = cf.Distribution(self, "SiteDistribution", default_root_object="index.html",
            default_behavior=cf.BehaviorOptions(origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS, cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
                function_associations=[cf.FunctionAssociation(function=spa_routes, event_type=cf.FunctionEventType.VIEWER_REQUEST)]),
            additional_behaviors={"/media/*": cf.BehaviorOptions(origin=origins.S3BucketOrigin.with_origin_access_control(media),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.HTTPS_ONLY, cache_policy=cf.CachePolicy.CACHING_OPTIMIZED,
                trusted_key_groups=[key_group])})
        deploy.BucketDeployment(self, "Publish", sources=[deploy.Source.asset(cfg["frontendDir"])],
            destination_bucket=bucket, distribution=distribution, distribution_paths=["/*"], memory_limit=1024,
            log_group=logs.LogGroup(self, "PublishLogs", retention=logs.RetentionDays.ONE_MONTH))
        CfnOutput(self, "SiteUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "SiteBucket", value=bucket.bucket_name)
        CfnOutput(self, "MediaBucket", value=media.bucket_name)
