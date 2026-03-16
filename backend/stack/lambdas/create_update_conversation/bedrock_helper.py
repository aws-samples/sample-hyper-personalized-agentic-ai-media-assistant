import os
import time

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from exceptions import EnvVarNotDefinedError
from models import MessageChunk, QueryClassification, QueryRewrite, Title
from utils import emit_content, emit_output_event, parse_xml

config = Config(read_timeout=45000)

bedrock_client = boto3.client("bedrock-runtime", config=config)

logger = Logger()

INTENT_CLASSIFICATION_PROMPT = os.getenv("INTENT_CLASSIFICATION_PROMPT")
if INTENT_CLASSIFICATION_PROMPT is None:
    logger.error("INTENT_CLASSIFICATION_PROMPT environment variable is not defined")
    raise EnvVarNotDefinedError("INTENT_CLASSIFICATION_PROMPT")

QUERY_REWRITE_PROMPT = os.getenv("QUERY_REWRITE_PROMPT")
if QUERY_REWRITE_PROMPT is None:
    logger.error("QUERY_REWRITE_PROMPT environment variable is not defined")
    raise EnvVarNotDefinedError("QUERY_REWRITE_PROMPT")

FINAL_RESPONSE_PROMPT = os.getenv("FINAL_RESPONSE_PROMPT")
if FINAL_RESPONSE_PROMPT is None:
    logger.error("FINAL_RESPONSE_PROMPT environment variable is not defined")
    raise EnvVarNotDefinedError("FINAL_RESPONSE_PROMPT")

RERANK_PROMPT = os.getenv("RERANK_PROMPT")
if RERANK_PROMPT is None:
    logger.error("RERANK_PROMPT environment variable is not defined")
    raise EnvVarNotDefinedError("RERANK_PROMPT")

QUOTE_FINDER_PROMPT = os.getenv("QUOTE_FINDER_PROMPT")
if QUOTE_FINDER_PROMPT is None:
    logger.error("QUOTE_FINDER_PROMPT environment variable is not defined")
    raise EnvVarNotDefinedError("QUOTE_FINDER_PROMPT")

GENERAL_RESPONSE_PROMPT = os.getenv("GENERAL_RESPONSE_PROMPT")
if GENERAL_RESPONSE_PROMPT is None:
    logger.error("GENERAL_RESPONSE_PROMPT environment variable is not defined")
    raise EnvVarNotDefinedError("GENERAL_RESPONSE_PROMPT")


def classify_user_intent(
    query: str, previous_queries: list[str], add_llm_metrics: callable
) -> QueryClassification:
    start_time = time.perf_counter()
    prompt_var = {
        "query": {
            "text": f"Current User Query: {query} \n\n Previous Inputs: {previous_queries}",
        }
    }
    logger.info(f"Classifying user intent with prompt: {prompt_var}")
    response = bedrock_client.converse(
        modelId=INTENT_CLASSIFICATION_PROMPT,
        promptVariables=prompt_var,
    )
    response_message = response["output"]["message"]["content"][0]["text"]
    logger.info(f"Total usage: { response['usage']}")
    input_tokens, output_tokens = (
        response["usage"]["inputTokens"],
        response["usage"]["outputTokens"],
    )
    xml_response_message = parse_xml(
        response_message,
        [
            "intent",
            "animation_bias",
            "is_award_winning",
            "kid_content",
            "is_time_period",
        ],
    )
    add_llm_metrics(
        "Classify", time.perf_counter() - start_time, input_tokens, output_tokens
    )
    logger.info(f"Classified intent: {xml_response_message}")
    return QueryClassification(**xml_response_message)


def quote_to_title(query: str, add_llm_metrics: callable) -> str | None:
    start_time = time.perf_counter()
    prompt_var = {"query": {"text": query}}

    logger.info(f"Finding quote with prompt: {prompt_var}")
    response = bedrock_client.converse(
        modelId=QUOTE_FINDER_PROMPT,
        promptVariables=prompt_var,
        performanceConfig={"latency": "optimized"},
    )

    response_message = response["output"]["message"]["content"][0]["text"]
    input_tokens = response["usage"]["inputTokens"]
    output_tokens = response["usage"]["outputTokens"]

    xml_response_message = parse_xml(response_message, ["title"])

    add_llm_metrics(
        "QuoteFinder", time.perf_counter() - start_time, input_tokens, output_tokens
    )

    logger.info(f"Quote finder intent: {xml_response_message}")
    return xml_response_message["title"]


def rewrite_user_query(
    watch_history_themes: list,
    conversation_history: list,
    dates: tuple[str, str],
    add_llm_metrics: callable,
) -> QueryRewrite:
    start_time = time.perf_counter()
    prompt_var = {
        "query": {
            "text": f"{conversation_history } \n\n watch_history: {watch_history_themes} \n\n previousLatestDate: {dates[1]} previousEarliestDate: {dates[0]}",
        }
    }
    logger.info(f"Rewriting user query with prompt: {prompt_var}")
    response = bedrock_client.converse(
        modelId=QUERY_REWRITE_PROMPT,
        promptVariables=prompt_var,
    )

    response_message = response["output"]["message"]["content"][0]["text"]
    logger.info(f"Total usage: { response['usage']}")
    input_tokens, output_tokens = (
        response["usage"]["inputTokens"],
        response["usage"]["outputTokens"],
    )
    xml_response_message = parse_xml(
        response_message, ["query", "media_type", "user_profile"]
    )
    add_llm_metrics(
        "Rewrite", time.perf_counter() - start_time, input_tokens, output_tokens
    )
    return QueryRewrite(**xml_response_message)


def respond_to_question(
    conversation_id: str,
    conversation_history: list,
    recommended_titles: list[Title],
    user_profile: str,
    add_llm_metrics: callable,
) -> tuple[str, float]:
    # Get streaming response from model

    recommended_content = [f"{t.title}" for t in recommended_titles]

    logger.info(conversation_history)
    user_prompt = f"retrievedTitles: {recommended_content},\n\n userProfile: {user_profile},\n\n conversationHistory: {conversation_history}, \n\n"

    logger.info(f"Final response prompt: {user_prompt}")
    response = bedrock_client.converse_stream(
        modelId=FINAL_RESPONSE_PROMPT,
        promptVariables={"query": {"text": user_prompt}},
        performanceConfig={"latency": "optimized"},
    )
    
    # Process stream response
    final_response = ""
    order = 0
    first_token = None

    if stream := response.get("stream"):
        for event in stream:
            if "contentBlockDelta" in event:
                if not first_token:
                    first_token = time.perf_counter()
                message = event["contentBlockDelta"]["delta"]["text"]
                final_response += message
                # emit_output_event(
                #     MessageChunk(
                #         conversation_id=conversation_id,
                #         content=message,
                #         is_final=False,
                #         order=order,
                #     )
                # )
                order += 1
            if "metadata" in event:
                usage = event["metadata"]["usage"]
                logger.info(f"Total usage: {usage}")
                input_tokens, output_tokens = (
                    usage["inputTokens"],
                    usage["outputTokens"],
                )

    ## Emit everything else :)
    # emit_content(conversation_id, recommended_titles, order)
    print("Final LLM run successfully")

    if input_tokens and output_tokens:
        add_llm_metrics("Final", input_tokens=input_tokens, output_tokens=output_tokens)

    return final_response, first_token


def respond_to_general_question(
    conversation_id: str,
    conversation_history: list,
    recommended_titles: list[Title],
    user_profile: str,
    add_llm_metrics: callable,
) -> tuple[str, float]:
    # Get streaming response from model

    recommended_content = [f"{t.title}" for t in recommended_titles]

    logger.info(conversation_history)
    user_prompt = f"retrievedTitles: {recommended_content}, conversationHistory: {conversation_history}, \n\n"

    logger.info(f"Final response prompt: {user_prompt}")
    response = bedrock_client.converse_stream(
        modelId=GENERAL_RESPONSE_PROMPT,
        promptVariables={"query": {"text": user_prompt}},
        performanceConfig={"latency": "optimized"},
    )

    # Process stream response
    final_response = ""
    order = 0
    first_token = None

    if stream := response.get("stream"):
        for event in stream:
            if "contentBlockDelta" in event:
                if not first_token:
                    first_token = time.perf_counter()
                message = event["contentBlockDelta"]["delta"]["text"]
                final_response += message
                # emit_output_event(
                #     MessageChunk(
                #         conversation_id=conversation_id,
                #         content=message,
                #         is_final=False,
                #         order=order,
                #     )
                # )
                order += 1
            if "metadata" in event:
                usage = event["metadata"]["usage"]
                logger.info(f"Total usage: {usage}")
                input_tokens, output_tokens = (
                    usage["inputTokens"],
                    usage["outputTokens"],
                )

    ## Emit everything else :)
    # emit_content(conversation_id, recommended_titles, order)

    if input_tokens and output_tokens:
        add_llm_metrics("Final", input_tokens=input_tokens, output_tokens=output_tokens)

    return final_response, first_token
