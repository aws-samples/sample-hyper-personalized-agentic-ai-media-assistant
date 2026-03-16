"""
Create AgentCore Gateway with Lambda Target

This script sets up an Amazon Bedrock AgentCore Gateway with Cognito authentication
for a movie recommendation assistant. It performs the following:

1. Creates an IAM role for the gateway to assume
2. Sets up Cognito User Pool with OAuth2 M2M client for authentication
3. Creates an AgentCore Gateway with custom JWT authorization
4. Attaches a Lambda function as an MCP target to the gateway
5. Stores credentials in Secrets Manager and gateway URL in Parameter Store

Usage:
    python create_gateway.py --lambda-arn arn:aws:lambda:region:account:function:name
"""

from acg_utils import create_agentcore_gateway_role, get_or_create_user_pool, get_or_create_resource_server, get_or_create_m2m_client
from boto3.session import Session
import boto3
import uuid
import json
import argparse
import time

# Cognito configuration constants
USER_POOL_NAME = "movie-assistant-agentcore-gateway-pool"
RESOURCE_SERVER_ID = "movie-assistant-agentcore-gateway-id"
RESOURCE_SERVER_NAME = "movie-assistant-agentcore-gateway-name"
CLIENT_NAME = "movie-assistant-agentcore-gateway-client"
SCOPES = [
    {"ScopeName": "gateway:read", "ScopeDescription": "Read access"},
    {"ScopeName": "gateway:write", "ScopeDescription": "Write access"}
]


def main(lambda_arn: str):
    """
    Main function to create and configure the AgentCore Gateway.
    
    Args:
        lambda_arn: ARN of the Lambda function to use as the gateway target
    """
    boto_session = Session()
    region = boto_session.region_name
    scope_string = f"{RESOURCE_SERVER_ID}/gateway:read {RESOURCE_SERVER_ID}/gateway:write"

    # Step 1: Create IAM role for the gateway
    # This role allows the gateway to invoke Lambda functions
    agentcore_gateway_iam_role = create_agentcore_gateway_role("movie-assistant-lambdagateway")
    print("Agentcore gateway role ARN: ", agentcore_gateway_iam_role['Role']['Arn'])

    # Step 2: Setup Cognito for M2M authentication
    # Creates user pool, resource server with scopes, and OAuth2 client
    cognito = boto3.client("cognito-idp", region_name=region)
    print("Creating or retrieving Cognito resources...")
    
    user_pool_id = get_or_create_user_pool(cognito, USER_POOL_NAME, region)
    print(f"User Pool ID: {user_pool_id}")

    get_or_create_resource_server(cognito, user_pool_id, RESOURCE_SERVER_ID, RESOURCE_SERVER_NAME, SCOPES)
    print("Resource server ensured.")

    client_id, client_secret = get_or_create_m2m_client(cognito, user_pool_id, CLIENT_NAME, RESOURCE_SERVER_ID)
    print(f"Client ID: {client_id}")

    # OpenID Connect discovery URL for JWT validation
    cognito_discovery_url = f'https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration'
    

    # Step 3: Create AgentCore Gateway with custom JWT authorizer
    # The gateway uses Cognito for authentication via JWT tokens
    gateway_client = boto3.client('bedrock-agentcore-control', region_name=region)
    gateway_name = f'movie-assistant-gateway-{str(uuid.uuid4())[:5]}'
    
    auth_config = {
        "customJWTAuthorizer": {
            "allowedClients": [client_id],  # Only allow tokens from our Cognito client
            "discoveryUrl": cognito_discovery_url
        }
    }
    
    create_response = gateway_client.create_gateway(
        name=gateway_name,
        roleArn=agentcore_gateway_iam_role['Role']['Arn'],
        protocolType='MCP',  # Model Context Protocol
        authorizerType='CUSTOM_JWT',
        authorizerConfiguration=auth_config,
        description='AgentCore Gateway with AWS Lambda target type'
    )
    
    gateway_id = create_response["gatewayId"]
    gateway_url = create_response["gatewayUrl"]
    
    # Wait for gateway to transition from CREATING to READY status
    # Gateway must be ready before we can attach targets
    print("Waiting for gateway to be ready...")
    while True:
        gateway_status = gateway_client.get_gateway(gatewayIdentifier=gateway_id)
        status = gateway_status.get("status")
        print(f"Gateway status: {status}")
        if status == "READY":
            print("Gateway is ready!")
            break
        elif status == "FAILED":
            raise Exception(f"Gateway creation failed: {gateway_status}")
        time.sleep(5)

    # Step 4: Create Lambda target with MCP tool schema
    # Defines the tool interface that the gateway exposes
    lambda_target_config = {
        "mcp": {
            "lambda": {
                "lambdaArn": lambda_arn,
                "toolSchema": {
                    "inlinePayload": [
                        {
                            "name": "get_title_recommendation",
                            "description": "tool that retrieves recommendation for a user based on the user_query",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "user_query": {
                                        "type": "string"
                                    },
                                },
                                "required": ["user_query"]
                            }
                        }
                    ]
                }
            }
        }
    }

    # Use gateway's IAM role for Lambda invocation credentials
    credential_config = [
        {"credentialProviderType": "GATEWAY_IAM_ROLE"}
    ]
    
    target_name = f'movie-assistant-target-{str(uuid.uuid4())[:5]}'
    gateway_client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=target_name,
        description='Lambda Target for AgentCore Gateway',
        targetConfiguration=lambda_target_config,
        credentialProviderConfigurations=credential_config
    )

    # Step 5: Store credentials and configuration for other services to use
    cognito_config = {
        'pool_id': user_pool_id,
        'client_id': client_id,
        'client_secret': client_secret,
        'discovery_url': cognito_discovery_url,
    }

    ssm_client = boto3.client('ssm', region_name=region)
    secrets_client = boto3.client('secretsmanager', region_name=region)

    # Store Cognito credentials in Secrets Manager (sensitive data)
    try:
        secrets_client.create_secret(
            Name='mcp_server/cognito/credentials',
            Description='Cognito credentials for MCP server',
            SecretString=json.dumps(cognito_config)
        )
        print("✓ Cognito credentials stored in Secrets Manager")
    except secrets_client.exceptions.ResourceExistsException:
        secrets_client.update_secret(
            SecretId='mcp_server/cognito/credentials',
            SecretString=json.dumps(cognito_config)
        )
        print("✓ Cognito credentials updated in Secrets Manager")

    # Store gateway URL in Parameter Store (non-sensitive configuration)
    ssm_client.put_parameter(
        Name='/mcp_server/gateway/gateway_url',
        Value=gateway_url,
        Type='String',
        Description='AgentCore Gateway URL for Lambda MCP server',
        Overwrite=True
    )

    print("======AGENTCORE GATEWAY CREATION DETAIL======\n")
    print(f"✓ User Pool ID: {user_pool_id}")
    print(f"✓ Cognito Discovery URL: {cognito_discovery_url}")
    print(f"✓ Cognito Resource Server ID: {RESOURCE_SERVER_ID}")
    print(f"✓ AgentCore Gateway Name: {gateway_name}")
    print(f"✓ AgentCore Gateway URL: {gateway_url}")
    print(f"✓ AgentCore Gateway Target Name: {target_name}")
    print(f"✓ AgentCore Gateway Target Tool Name: {target_name}__get_title_recommendation")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Create AgentCore Gateway with Lambda target')
    parser.add_argument('--lambda-arn', required=True, help='ARN of the Lambda function to use as target')
    args = parser.parse_args()
    main(args.lambda_arn)
