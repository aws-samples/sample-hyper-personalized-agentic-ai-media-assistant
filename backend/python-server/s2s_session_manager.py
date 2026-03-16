import asyncio
import json
import warnings
import uuid
from s2s_events import S2sEvent
import time

# aws_sdk_bedrock_runtime library is experimental
from aws_sdk_bedrock_runtime.client import BedrockRuntimeClient, InvokeModelWithBidirectionalStreamOperationInput
from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamInputChunk, BidirectionalInputPayloadPart
from aws_sdk_bedrock_runtime.config import Config
from smithy_aws_core.auth.sigv4 import SigV4AuthScheme


import os
from datetime import datetime
import boto3
import logging
import copy
import tempfile

if os.environ.get("LOCALHOST", "false").lower() == "true":
    from smithy_aws_core.identity.environment import EnvironmentCredentialsResolver
    is_local = True
else:
    from smithy_aws_core.identity.container import ContainerCredentialsResolver
    from smithy_http.aio.aiohttp import AIOHTTPClient, AIOHTTPClientConfig
    is_local = False
import datetime
import time
import inspect
from aws_sdk_bedrock_runtime.models import InvokeModelWithBidirectionalStreamInputChunk, BidirectionalInputPayloadPart
from movie_scene_assistant import MovieSceneAssistant
from movie_actor_assistant import MovieActorAssistant
from movie_recommendation_assistant import MovieRecommendationAssistant

# Suppress warnings
warnings.filterwarnings("ignore")
# Configure logging
logger = logging.getLogger(__name__)

# Debug mode flag
DEBUG = False

def debug_print(message):
    """Print only if debug mode is enabled"""
    if DEBUG:
        functionName = inspect.stack()[1].function
        if  functionName == 'time_it' or functionName == 'time_it_async':
            functionName = inspect.stack()[2].function
        print('{:%Y-%m-%d %H:%M:%S.%f}'.format(datetime.datetime.now())[:-3] + ' ' + functionName + ' ' + message)

def time_it(label, methodToRun):
    start_time = time.perf_counter()
    result = methodToRun()
    end_time = time.perf_counter()
    debug_print(f"Execution time for {label}: {end_time - start_time:.4f} seconds")
    return result

async def time_it_async(label, methodToRun):
    start_time = time.perf_counter()
    result = await methodToRun()
    end_time = time.perf_counter()
    debug_print(f"Execution time for {label}: {end_time - start_time:.4f} seconds")
    return result


class S2sSessionManager:
    """Manages bidirectional streaming with AWS Bedrock using asyncio"""
    
    def __init__(self, model_id='amazon.nova-2-sonic-v1:0', region='us-east-1', authenticated_user=None):
        """Initialize the stream manager."""
        self.model_id = model_id
        self.region = region
        self.credential_signal_file = tempfile.gettempdir()
        self.authenticated_user = authenticated_user
        self.audio_input_queue = asyncio.Queue()
        self.output_queue = asyncio.Queue()
        
        self.response_task = None
        self.stream = None
        self.is_active = False
        self.bedrock_client = None
        
        # Session information
        self.prompt_name = None  # Will be set from frontend
        self.content_name = None  # Will be set from frontend
        self.audio_content_name = None  # Will be set from frontend
        self.toolUseContent = ""
        self.toolUseId = ""
        self.toolName = ""
        self.additionalContext = [] # Instantiate additional context allowing other search functionality
        
        self.movie_scene_assistant = MovieSceneAssistant(authenticated_user=self.authenticated_user)
        self.movie_actor_assistant = MovieActorAssistant(movie_scene_assistant=self.movie_scene_assistant, 
                                                         additionalContext=self.additionalContext,
                                                         authenticated_user=self.authenticated_user)
        self.movie_recommendation_assistant = MovieRecommendationAssistant(region=region, authenticated_user=self.authenticated_user, additionalContext = self.additionalContext)

    def _check_credential_refresh(self):
        """Check if credentials have been refreshed and recreate client if needed."""
        try:
            if os.path.exists(self.credential_signal_file):
                signal_mtime = os.path.getmtime(self.credential_signal_file)
                
                if signal_mtime > self.last_credential_check:
                    # A real credential refresh from background daemon
                    logger.info("Credential refresh signal detected - recreating Bedrock client")
                    self.bedrock_client = None  # Force recreation
                    self.last_credential_check = signal_mtime
                    # Remove the signal file after processing
                    os.remove(self.credential_signal_file)
        except Exception as e:
            logger.error(f"Error checking credential refresh signal: {e}")

    def _initialize_client(self):
        """Initialize the Bedrock client."""        
        if self.bedrock_client is not None:
            return True

        logger.info(f"Initializing Bedrock client for region {self.region}")
        if is_local:
            logger.info(f"Initializing Bedrock client for for local development")
            config = Config(
            endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
            region=self.region,
            aws_credentials_identity_resolver=EnvironmentCredentialsResolver(),
            auth_schemes={"aws.auth#sigv4": SigV4AuthScheme(service="bedrock")})
            self.bedrock_client = BedrockRuntimeClient(config=config)
        else:
            logger.info(f"Initializing Bedrock client for for ECS development")
            client_config = AIOHTTPClientConfig()
            
            # Create an HTTP client with the configuration
            http_client = AIOHTTPClient(client_config=client_config)
            credentials_resolver = ContainerCredentialsResolver(http_client)
        
            config = Config(
                endpoint_uri=f"https://bedrock-runtime.{self.region}.amazonaws.com",
                region=self.region,
                aws_credentials_identity_resolver=credentials_resolver,
                auth_schemes={"aws.auth#sigv4": SigV4AuthScheme(service="bedrock")})
            self.bedrock_client = BedrockRuntimeClient(config=config)
            logger.info(f"Bedrock client initialized successfully")
        
    async def initialize_stream(self):
        """Initialize the bidirectional stream with Bedrock."""
        try:
            if not self.bedrock_client:
                self._initialize_client()

        except Exception as ex:
            self.is_active = False
            logger.info(f"Failed to initialize stream: {str(e)}", exc_info=True)
            raise

        try:
            # Initialize the stream
            self.stream = await self.bedrock_client.invoke_model_with_bidirectional_stream(
                InvokeModelWithBidirectionalStreamOperationInput(model_id=self.model_id)
            )
            self.is_active = True
            
            # Start listening for responses
            self.response_task = asyncio.create_task(self._process_responses())

            # Start processing audio input
            asyncio.create_task(self._process_audio_input())
            
            # Wait a bit to ensure everything is set up
            await asyncio.sleep(0.1)
            
            debug_print("Stream initialized successfully")
            return self
        except Exception as e:
            self.is_active = False
            print(f"Failed to initialize stream: {str(e)}")
            raise


    async def refresh_credentials_immediately(self):
        """Refresh credentials immediately by calling the container metadata endpoint"""
        try:
            logger.info("Refreshing credentials due to ExpiredToken...")
            # Get credentials from ECS container metadata endpoint
            uri = os.environ.get('AWS_CONTAINER_CREDENTIALS_RELATIVE_URI')
            if not uri:
                logger.info("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI not found in environment, try with STS")
                try:
                    sts_client = boto3.client('sts')

                    # Call get_session_token to generate temporary credentials
                    response = sts_client.get_session_token()

                    # Extract credentials
                    credentials = response['Credentials']
                    access_key = credentials['AccessKeyId']
                    secret_key = credentials['SecretAccessKey']
                    session_token = credentials['SessionToken']
                    
                    os.environ['AWS_ACCESS_KEY_ID'] = access_key
                    os.environ['AWS_SECRET_ACCESS_KEY'] = secret_key
                    os.environ['AWS_SESSION_TOKEN'] = session_token
                    return True
                except Exception as e:
                    logger.error(f"Error refreshing credentials: {str(e)}")
                    return False
                            
            import aiohttp
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"http://169.254.170.2{uri}", timeout=2) as response:
                        if not response.ok:
                            logger.error(f"Failed to fetch credentials: {response.status}")
                            return False
                        
                        creds = await response.json()
            except ImportError:
                # Fall back to requests if aiohttp is not available
                import requests
                response = requests.get(f"http://169.254.170.2{uri}", timeout=2)
                if not response.ok:
                    logger.error(f"Failed to fetch credentials: {response.status_code}")
                    return False
                    
                creds = response.json()
            os.environ['AWS_ACCESS_KEY_ID'] = creds['AccessKeyId']
            os.environ['AWS_SECRET_ACCESS_KEY'] = creds['SecretAccessKey']
            os.environ['AWS_SESSION_TOKEN'] = creds['Token']
            
            logger.info(f"Successfully refreshed credentials, new key ends with: ...{creds['AccessKeyId'][-4:]}")
            
            # Force client recreation on next use
            self.bedrock_client = None
            return True
        except Exception as e:
            logger.error(f"Error refreshing credentials: {str(e)}")
            return False
    
    async def send_raw_event(self, event_data):
        try:
            """Send a raw event to the Bedrock stream."""
            if not self.stream or not self.is_active:
                debug_print("Stream not initialized or closed")
                return
            
            event_json = json.dumps(event_data)
            event = InvokeModelWithBidirectionalStreamInputChunk(
                value=BidirectionalInputPayloadPart(bytes_=event_json.encode('utf-8'))
            )
            await self.stream.input_stream.send(event)
            
        except Exception as e:
            debug_print(f"Error sending event: {str(e)}")
    
    async def _process_audio_input(self):
        """Process audio input from the queue and send to Bedrock."""
        while self.is_active:
            try:
                # Get audio data from the queue
                data = await self.audio_input_queue.get()
                
                # Extract data from the queue item
                prompt_name = data.get('prompt_name')
                content_name = data.get('content_name')
                audio_bytes = data.get('audio_bytes')
                
                if not audio_bytes or not prompt_name or not content_name:
                    debug_print("Missing required audio data properties")
                    continue

                # Create the audio input event
                audio_event = S2sEvent.audio_input(prompt_name, content_name, audio_bytes.decode('utf-8') if isinstance(audio_bytes, bytes) else audio_bytes)
                
                # Send the event
                await self.send_raw_event(audio_event)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                debug_print(f"Error processing audio: {e}")
                if DEBUG:
                    import traceback
                    traceback.print_exc()
    
    def add_audio_chunk(self, prompt_name, content_name, audio_data):
        """Add an audio chunk to the queue."""
        # The audio_data is already a base64 string from the frontend
        self.audio_input_queue.put_nowait({
            'prompt_name': prompt_name,
            'content_name': content_name,
            'audio_bytes': audio_data
        })
    
    async def _process_responses(self):
        """Process incoming responses from Bedrock."""
        while self.is_active:
            try:            
                output = await self.stream.await_output()
                result = await output[1].receive()
                
                if result.value and result.value.bytes_:
                    response_data = result.value.bytes_.decode('utf-8')
                    
                    json_data = json.loads(response_data)
                    json_data["timestamp"] = int(time.time() * 1000)  # Milliseconds since epoch
                    # print(f"received event from Bedrock: {json_data}")
                    if 'event' in json_data:
                        # if "audioOutput" not in json_data['event']:
                        #     print(json_data)
                        if 'usageEvent' in json_data['event']:
                            continue
                        # Handle tool use detection
                        if 'toolUse' in json_data['event']:
                            # print(f"json event here from Sonic with toolUse is: {json_data}")
                            self.toolUseContent = json_data['event']['toolUse']
                            self.toolName = json_data['event']['toolUse']['toolName']
                            self.toolUseId = json_data['event']['toolUse']['toolUseId']
                            debug_print(f"Tool use detected: {self.toolName}, ID: {self.toolUseId}, "+ json.dumps(json_data['event']))
                        elif 'contentEnd' in json_data['event'] and json_data['event'].get('contentEnd', {}).get('type') == 'TOOL':
                            prompt_name = json_data['event']['contentEnd'].get("promptName")
                            debug_print("Processing tool use and sending result")
                            toolResult = await self.processToolUse(self.toolName, self.toolUseContent)
                            
                            # Send tool start event
                            toolContent = str(uuid.uuid4())
                            tool_start_event = S2sEvent.content_start_tool(prompt_name, toolContent, self.toolUseId)
                            await self.send_raw_event(tool_start_event)
                            
                            # Send tool result event
                            if isinstance(toolResult, dict):
                                if "result" in toolResult:
                                    copied_tool_result  = copy.deepcopy(toolResult)
                                    tool_results_raw = copied_tool_result["result"]
                                    if isinstance(tool_results_raw, list):
                                        for tool_result_raw in tool_results_raw:
                                            if "poster" in tool_result_raw:
                                                tool_result_raw.pop("poster")
                                            if "content_url" in tool_result_raw:
                                                tool_result_raw.pop("content_url")
                                    content_json_string = json.dumps(copied_tool_result)
                                else:
                                    content_json_string = json.dumps(toolResult)
                            else:
                                content_json_string = toolResult

                            tool_result_event = S2sEvent.text_input_tool(prompt_name, toolContent, content_json_string)
                            
                            await self.send_raw_event(tool_result_event)

                            # Send tool content end event
                            tool_content_end_event = S2sEvent.content_end(prompt_name, toolContent)
                            await self.send_raw_event(tool_content_end_event)
                            
                            poster_data = []
                            has_poster_data = False
                            for tr in toolResult["result"]:
                                if "poster" in tr:
                                    has_poster_data = True
                                    tool_result_data = { 
                                        "url" : tr["poster"], 
                                        "role" : "ASSISTANT", 
                                        "title" : tr["title"],
                                        "plot" : tr["plot"],
                                        "rating" : tr["rating"],
                                        "content_url" : tr["content_url"]}
                                    poster_data.append(tool_result_data)
                            if has_poster_data:
                                posters_data = {"event" : { "poster":  {"contentId" : toolContent, "content" : poster_data }}}
                                await self.output_queue.put(posters_data)            
                                
                    # Put the response in the output queue for forwarding to the frontend
                    await self.output_queue.put(json_data)
            except json.JSONDecodeError as ex:
                print(ex)
                await self.output_queue.put({"raw_data": response_data})
            except StopAsyncIteration as ex:
                # Stream has ended
                print(ex)
            except Exception as e:
                # Handle ValidationException properly
                if "ValidationException" in str(e):
                    error_message = str(e)
                    print(f"Validation error: {error_message}")
                else:
                    print(f"Error receiving response: {e}")
                break

        self.is_active = False

    def ingest_user_watch_log(self, data):
        response = self.movie_scene_assistant.insert_user_watch_log( username=data["username"], timemillis=data["timemillis"], video_name=data["video_name"])
        return response
        
    async def processToolUse(self, toolName, toolUseContent):
        global lambda_mcp_client
        print(f"Tool Use Content: {toolUseContent}")

        if toolUseContent.get("content"):
            # Parse the JSON string in the content field
            content_json = json.loads(toolUseContent.get("content"))
            question = content_json.get("question", "")

        if toolName.lower() == "getMovieRecommendation".lower():
            response = self.movie_recommendation_assistant.process_movie_recommendation(question=question, prompt_name=self.prompt_name)
            return response

        elif toolName.lower() == "explainTheMovieScene".lower():
            response = self.movie_scene_assistant.process_movie_scene_request(question)
            return response

        elif toolName.lower() == "getActorInformation".lower():
            result = self.movie_actor_assistant.process_actor_information(question=question)
            return result
        
        return {"result" : "I am sorry but I ma not able to help with the question at the moment."}
    
    async def close(self):
        """Close the stream properly."""
        if not self.is_active:
            return
            
        self.is_active = False
        
        if self.stream:
            await self.stream.input_stream.close()
        
        if self.response_task and not self.response_task.done():
            self.response_task.cancel()
            try:
                await self.response_task
            except asyncio.CancelledError:
                pass
