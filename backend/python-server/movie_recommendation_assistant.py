import os
import boto3
import requests
import json
import uuid
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

use_agentcore_gateway = os.environ.get("USE_AGENTCORE_GATEWAY", "true")
gateway_url = os.environ.get("AGENTCORE_GATEWAY_URL", "https://ibc-demo-gateway-2a9cd-vctauxr3ib.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
RESOURCE_SERVER_ID = os.environ.get("AGENTCORE_COGNITO_RESOURCE_SERVER_ID", "ibc-demo-agentcore-gateway-id")
scopeString = f"{RESOURCE_SERVER_ID}/gateway:read {RESOURCE_SERVER_ID}/gateway:write"
get_title_recommendation_tool_name = os.environ.get("AGENTCORE_GATEWAY_TOOL_TITLE_RECOMMENDATION", "convo-search-mcp-tool___get_title_recommendation")
convo_search_function_name_qualifier = os.environ.get("CONVO_SEARCH_LAMBDA_FUNCTION_QUALIFIER", "")
convo_search_function_name = os.environ.get("CONVO_SEARCH_LAMBDA_FUNCTION_NAME", 'NovaSonicSolutionBackendS-ConvoSearchAPIConversati-pWRLtULwgEWP')

class MovieRecommendationAssistant:
    def __init__(self, region, authenticated_user, additionalContext):
        self.lambda_client = boto3.client('lambda')
        self.authenticated_user = authenticated_user
        self.additionalContext = additionalContext
        self.is_new_chat = True
        self.region = region
        if use_agentcore_gateway.lower() == "true":
            secrets = self.get_secrets()
            self.pool_id = secrets["pool_id"]
            self.client_id = secrets["client_id"]
            self.client_secret = secrets["client_secret"]
            token_response = self.get_token(self.pool_id, self.client_id, self.client_secret,scopeString)
            bearer_access_token=token_response["access_token"]
            def create_streamable_http_transport():
                return streamablehttp_client(gateway_url,headers={"Authorization": f"Bearer {bearer_access_token}"})

            self.lambda_mcp_client = MCPClient(create_streamable_http_transport)

    def get_token(self, user_pool_id: str, client_id: str, client_secret: str, scope_string: str) -> dict:
        try:
            user_pool_id_without_underscore = user_pool_id.replace("_", "")
            url = f"https://{user_pool_id_without_underscore}.auth.{self.region}.amazoncognito.com/oauth2/token"
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            data = {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": scope_string,

            }
            response = requests.post(url, headers=headers, data=data, timeout=10.0)
            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as err:
            return {"error": str(err)}
        
    def get_secrets(self) -> dict:
        secrets_client = boto3.client('secretsmanager')
        response = secrets_client.get_secret_value(SecretId='mcp_server/cognito/credentials')
        secret_value = response['SecretString']
        parsed_secret = json.loads(secret_value)
        return parsed_secret
    
    def process_title_recommendation_mcp(self, question, prompt_name):
        with self.lambda_mcp_client:
            response = self.lambda_mcp_client.call_tool_sync(
                tool_use_id=str(uuid.uuid4())[:5], # You can replace this with unique identifier. 
                name=get_title_recommendation_tool_name, # This is the tool name based on AWS Lambda target types. This will change based on the target name
                arguments={"user_query": question, "conversation_id" : prompt_name, "user" : self.authenticated_user}
            )
            response_text = response["content"][0]["text"]
            recommendation_event = json.loads(response_text)
            return { "result" : recommendation_event}
    
    def process_movie_recommendation(self, question, prompt_name):
        if use_agentcore_gateway.lower() == "true":
                try:
                    return self.process_title_recommendation_mcp(question=question, prompt_name=prompt_name)
                except Exception as e:
                    print(f"exception is thrown. Refreshing the bearer access token")
                    token_response = self.get_token(self.pool_id, self.client_id, self.client_secret,scopeString)
                    bearer_access_token=token_response["access_token"]
                    def create_streamable_http_transport():
                        return streamablehttp_client(gateway_url,headers={"Authorization": f"Bearer {bearer_access_token}"})
                    self.lambda_mcp_client = MCPClient(create_streamable_http_transport)
                    return self.process_title_recommendation_mcp(question=question, prompt_name=prompt_name)
        else:
            # Define your function name and payload
            payload = {
                "user_query" : question, 
                "conversation_id" : prompt_name, 
                "user" : self.authenticated_user,
                "is_new_chat" : self.is_new_chat
            }
            
            if len(convo_search_function_name_qualifier) > 0:
                response = self.lambda_client.invoke(
                FunctionName=convo_search_function_name,
                InvocationType='RequestResponse',  # or use 'Event' for async
                Payload=json.dumps(payload),
                Qualifier=convo_search_function_name_qualifier
            )
            else:
                # Invoke the Lambda function synchronously
                response = self.lambda_client.invoke(
                    FunctionName=convo_search_function_name,
                    InvocationType='RequestResponse',  # or use 'Event' for async
                    Payload=json.dumps(payload)
                )

            if self.is_new_chat:
                self.is_new_chat = False
            # The result is in response['Payload']
            recommendation_event = response['Payload'].read()
            recommendation_event_dict = json.loads(recommendation_event)
            recommendation_event_dict_as_str = []
            recommendation_event_dict_as_str.append("ASSISTANT: Recommended Titles: \n")
            for recommended_title in recommendation_event_dict:
                title = recommended_title["title"]
                plot = recommended_title["plot"]
                actors = ", ".join(recommended_title["actors"])
                recommended_title_str = f"Title: {title}, Plot: {plot}, Actors: {actors}"
                recommendation_event_dict_as_str.append(f"{recommended_title_str}\n")
            self.additionalContext.append("\n".join(recommendation_event_dict_as_str))
            return { "result" : recommendation_event_dict}

    