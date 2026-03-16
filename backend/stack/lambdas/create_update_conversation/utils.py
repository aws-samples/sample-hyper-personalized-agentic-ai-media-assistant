import hashlib
import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import boto3
import requests
from aws_lambda_powertools import Logger, Tracer
from boto3.dynamodb.conditions import Attr, Key

# Signing a request using only botocore
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session
from exceptions import EnvVarNotDefinedError
from models import MessageChunk, MessageInput, MessageOutput, Title, WatchHistory
from requests_aws4auth import AWS4Auth

logger = Logger()

dynamodb_client = boto3.client("dynamodb")
dynamodb_resource = boto3.resource("dynamodb")

TABLE_NAME = os.getenv("TABLE_NAME")
if TABLE_NAME is None:
    logger.error("TABLE_NAME environment variable is not defined")
    raise EnvVarNotDefinedError("TABLE_NAME")

APPSYNC_ENDPOINT = os.getenv("APPSYNC_ENDPOINT")
if APPSYNC_ENDPOINT is None:
    logger.error("APPSYNC_ENDPOINT environment variable is not defined")
    raise EnvVarNotDefinedError("APPSYNC_ENDPOINT")

REGION = os.getenv("AWS_REGION")

dynamodb_table = dynamodb_resource.Table(TABLE_NAME)

credentials = Session().get_credentials()
auth = AWS4Auth(region=REGION, service="appsync", refreshable_credentials=credentials)


def get_conversation(user: str, conversation_id: str, limit: int = None) -> dict:
    """Gets all messages for the specified conversation ID and user"""
    attributes_map = {
        f"#{i}": attr
        for i, attr in enumerate(
            [
                "content",
                "id",
                "tappableOptions",
                "suggestedContent",
                "role",
                "latestContentDate",
                "earliestContentDate",
                "llmTappableOptions",
            ]
        )
    }

    query_params = {
        "KeyConditionExpression": Key("pk").eq(f"CONVERSATION#{conversation_id}")
        & Key("sk").begins_with("MESSAGE#"),
        "ProjectionExpression": ", ".join(attributes_map.keys()),
        "ExpressionAttributeNames": attributes_map,
        "Select": "SPECIFIC_ATTRIBUTES",
        "FilterExpression": Attr("user").eq(user),
    }

    if limit:
        query_params["Limit"] = limit

    response = dynamodb_table.query(**query_params)
    conversation_history = response.get("Items", [])

    while "LastEvaluatedKey" in response and not limit:
        query_params["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        response = dynamodb_table.query(**query_params)
        conversation_history.extend(response.get("Items", []))

    return conversation_history


def update_conversation_from_user(user: str, message: MessageInput) -> dict:
    """Updates an existing conversation for the specified user

    Args:
        user: The user ID
        input: The MessageInput containing the conversation content

    Returns:
        dict: Contains the conversation metadata under the 'conversation' key
    """
    created_at = int(datetime.now(UTC).timestamp() * 1000)
    conversation_id = message.conversation_id
    message_item = {
        "pk": f"CONVERSATION#{conversation_id}",
        "sk": f"MESSAGE#{created_at}",
        "createdAt": created_at,
        "content": message.content,
        "user": user,
        "role": "user",
        "conversationId": conversation_id,
        "contentType": message.content_type,
    }
    dynamodb_table.put_item(Item=message_item)


def parse_xml(xml_string: str, parsed_values: list[str]) -> dict[str, any]:
    """Parse XML-like string response into a dictionary."""
    logger.info("Parsing XML response")
    logger.info(f"XML response: {xml_string}")
    result = {}
    for value in parsed_values:
        pattern = rf"<{value}>(.*?)</{value}>"
        match = re.search(pattern, xml_string, re.DOTALL)
        if match:
            result[value] = match.group(1).strip()
    return result


def get_content_date(titles: list[Title], get_latest: bool = True) -> str:
    """Get the latest or oldest premiere date from a list of titles
    Args:
        titles: List of Title objects
        get_latest: If True returns latest date, if False returns oldest date
    """
    if not titles:
        return None

    # Filter out None/empty dates and find min/max
    valid_dates = [t.premiere_date for t in titles if t.premiere_date]
    return (
        max(valid_dates)
        if get_latest and valid_dates
        else min(valid_dates) if valid_dates else None
    )


def update_conversation_from_assistant(user: str, message: MessageOutput) -> dict:
    """Updates an existing conversation for the specified user

    Args:
        user: The user ID
        input: The MessageInput containing the conversation content

    Returns:
        dict: Contains the conversation metadata under the 'conversation' key
    """
    created_at = int(datetime.now(UTC).timestamp() * 1000)

    tappable_options = message.tappable_options if message.tappable_options else []
    content_options = message.suggested_content if message.suggested_content else []

    message_item = {
        "pk": f"CONVERSATION#{message.conversation_id}",
        "sk": f"MESSAGE#{created_at}",
        "createdAt": created_at,
        # "content": message.content,
        # "rawContent": message.raw_content,
        "user": user,
        "role": "assistant",
        "conversationId": message.conversation_id,
        # "timeToFirstToken": Decimal(str(message.time_to_first_token)),
        # "tappableOptions": tappable_options,
        "suggestedContent": [c.id for c in content_options],
        "latestContentDate": get_content_date(message.suggested_content, True),
        "earliestContentDate": get_content_date(message.suggested_content, False),
        # "llmTappableOptions": message.llm_tappable_options,
    }
    dynamodb_table.put_item(Item=message_item)
    return {"conversation": message_item}


def get_watch_history_by_profile_id(profile_id: str) -> WatchHistory:
    """Gets the watch history for the specified profile ID

    Args:
        profile_id: The profile ID

    Returns:
        list: A list of watch history items
    """

    profile_hash = hashlib.md5(profile_id.encode(), usedforsecurity=False).hexdigest()
    projected_attributes = ["titleNames", "titleThemes", "titleIds"]
    query_params = {
        "Key": {"pk": f"PROFILE#{profile_hash}", "sk": "META"},
        "ProjectionExpression": ", ".join(projected_attributes),
    }

    response = dynamodb_table.get_item(**query_params)
    print(f"response from WATCH_HISTORY dynamoDB query: {response}")
    if "Item" not in response:
        print("no result found in WATCH_HISTORY")
        return WatchHistory(title_id=[], title_themes=[], title_name=[])
    return WatchHistory(
        title_ids=response["Item"].get("titleIds", []),
        title_themes=response["Item"].get("titleThemes", []),
        title_names=response["Item"].get("titleNames", []),
    )


def create_conversation(user: str, message: MessageInput):
    """Creates a new conversation for the specified user

    Args:
        user: The user ID
        input: The MessageInput containing the conversation content

    Returns:
        dict: Contains the conversation metadata under the 'conversation' key
    """
    created_at = int(datetime.now(UTC).timestamp() * 1000)
    conversation_id = message.conversation_id

    meta_item = {
        "pk": f"CONVERSATION#{conversation_id}",
        "sk": f"META#{user}",
        "id": conversation_id,
        "createdAt": created_at,
        "initialQuery": message.content,
        "user": user,
    }

    message_item = {
        "pk": f"CONVERSATION#{conversation_id}",
        "sk": f"MESSAGE#{created_at}",
        "createdAt": created_at,
        "content": message.content,
        "user": user,
        "conversationId": conversation_id,
        "role": "user",
        "contentType": message.content_type,
    }

    dynamodb_table.put_item(Item=meta_item)
    dynamodb_table.put_item(Item=message_item)


def send_appsync_event(chunk: MessageChunk):
    """Signs an AWS request using SigV4 authentication and executes it

    Args:
        data: Request body data
        params: Optional URL parameters
        headers: Optional request headers

    Returns:
        requests.Response: The response from the signed HTTP request
    """
    url = APPSYNC_ENDPOINT
    headers = {
        "content-type": "application/json",
    }
    data = {
        "channel": f"conversation/{chunk.conversation_id}",
        "events": [chunk.model_dump_json(by_alias=True)],
    }
    response = requests.post(url, json=data, headers=headers, auth=auth, timeout=10.0)
    logger.debug(response)


def get_previous_tappable_options(conversation_history: list) -> list[str]:
    """Get the previous tappable options from the conversation history."""
    tappable_options = set()
    for message in conversation_history:
        if "tappableOptions" in message and message["tappableOptions"]:
            tappable_options.update(message["tappableOptions"])
    return list(tappable_options)


def get_previous_suggested_content(conversation_history: list) -> list[str]:
    """Get the previous recommended title ids from the conversation history."""
    recommended_titles = set()
    for message in conversation_history:
        if "suggestedContent" in message and message["suggestedContent"]:
            recommended_titles.update(message["suggestedContent"])
    return list(recommended_titles)


def get_previous_queries(conversation_history: list) -> list[str]:
    """Get the previous tappable options from the conversation history."""
    previous_queries = []
    for message in conversation_history:
        if "role" in message and message["role"] == "user":
            previous_queries.append(message["content"])
    return previous_queries


def get_conversation_content(conversation_history: list) -> list[dict]:
    conversation_content = []
    print(f"conversation history : {conversation_history}")
    for message in conversation_history:
        conversation_content.append(
            {"role": message["role"], "content": message["content"] if "content" in message else ""}
        )
    return conversation_content


def get_conversation_content_with_tappables(conversation_history: list) -> list[dict]:
    conversation_content = []
    for message in conversation_history:
        msg = {"role": message["role"], "content": message["content"] if "content" in message else ""}
        if "llmTappableOptions" in message:
            msg["tappableOptions"] = message["llmTappableOptions"]
        conversation_content.append(msg)
    return conversation_content


def get_turn_count(conversation_history: list) -> int:
    """Get the turn count from the conversation history."""
    return len(get_previous_queries(conversation_history))


def get_date_range_from_conversation(conversation_history: list) -> tuple[str, str]:
    # Returns a tuple of (earliest_date, latest_date) strings from the conversation history
    # earliest_date: ISO formatted date string of earliest content mentioned by assistant
    # latest_date: ISO formatted date string of latest content mentioned by assistant
    # Returns (None, None) if no assistant messages found
    assistant_messages = [
        msg for msg in conversation_history if msg.get("role") == "assistant"
    ]
    if assistant_messages:
        latest_assistant_msg = assistant_messages[-1]
        return (
            latest_assistant_msg.get("earliestContentDate"),
            latest_assistant_msg.get("latestContentDate"),
        )
    return None, None


def get_era_themes() -> list[str]:
    return [
        "Contemporary",
        "Modern",
        "Classics",
        "Current",
        "Present-day",
        "Recent",
        "New",
        "Latest",
        "Traditional",
        "Vintage",
        "Retro",
        "Old-school",
        "Historical",
        "Period",
        "Timeless",
    ]


def get_entity_type_themes() -> list[str]:
    return [
        "Movies",
        "TV Shows",
        "Films",
        "Television",
        "Series",
        "Shows",
        "Television Series",
        "TV Programs",
    ]


def emit_output_event(chunk: MessageChunk):
    send_appsync_event(chunk)


def emit_content(conversation_id: str, recommended_titles: list[Title], order: int):
    # Send content IDs, themes and final message in sequence
    messages = [
        MessageChunk(
            conversation_id=conversation_id,
            content=f"<content_ids>{[t.id for t in recommended_titles]}</content_ids>",
            is_final=False,
            order=order,
        ),
        MessageChunk(
            conversation_id=conversation_id, content="", is_final=True, order=order + 1
        ),
    ]

    for msg in messages:
        emit_output_event(msg)
