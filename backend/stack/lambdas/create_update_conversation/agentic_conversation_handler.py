"""
Copyright 2025 Amazon.com, Inc. and its affiliates. All Rights Reserved.

Licensed under the Amazon Software License (the "License").
You may not use this file except in compliance with the License.
A copy of the License is located at

  http://aws.amazon.com/asl/

or in the "license" file accompanying this file. This file is distributed
on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
express or implied. See the License for the specific language governing
permissions and limitations under the License.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from bedrock_helper import (
    classify_user_intent,
    quote_to_title,
    respond_to_general_question,
    respond_to_question,
    rewrite_user_query,
)
from botocore.exceptions import ClientError
from models import MessageChunk, MessageInput, MessageOutput, WatchHistory
from opensearch_helper import (
    find_recent_popular_titles,
    find_titles_and_tappable_options_by_query,
)
from utils import (
    create_conversation,
    emit_output_event,
    get_conversation,
    get_conversation_content,
    get_conversation_content_with_tappables,
    get_date_range_from_conversation,
    get_previous_queries,
    get_previous_suggested_content,
    get_previous_tappable_options,
    get_turn_count,
    get_watch_history_by_profile_id,
    parse_xml,
    update_conversation_from_assistant,
    update_conversation_from_user,
)

logger = Logger()

_metrics = Metrics()


def handle_conversation(metrics: Metrics, user: str, message: MessageInput) -> dict:
    """
    Handle the conversation by printing the conversation ID and the last message.
    Note: Recommend input sanitization forthe methods when deploy in production, such as Bedrock Guardrails.
    """
    global _metrics
    _metrics = metrics
    ## Create new conversation
    logger.append_keys(conversation_id=message.conversation_id)
    response_message = create_response(user, message, message.is_new_chat)
    return {"conversationId": message.conversation_id, "response_message" : response_message.json()}


def create_response(user: str, message: MessageInput, new_chat: bool):
    """Create a response for the conversation
    Note: Recommend input sanitization forthe methods when deploy in production, such as Bedrock Guardrails.
    """

    message_response = stream_response(
        message.conversation_id,
        (
            message.content.lower()
            if message.content_type == "TAPPABLE"
            else message.content
        ),
        user,
        message.profile,
    )
    if new_chat:
        create_conversation(user, message)
    else:
        update_conversation_from_user(user, message)
    update_conversation_from_assistant(user, message_response)
    if message_response.time_to_first_token:
        _metrics.add_metric(
            name="timeToFirstToken",
            unit=MetricUnit.Seconds,
            value=message_response.time_to_first_token,
        )
    return message_response


# @metrics.log_metrics TODO determine if we need this
def stream_response(
    conversation_id: str,
    input_text: str,
    user_id: str,
    profile: str,
) -> MessageOutput:
    try:

        start_time = time.perf_counter()
        completion = ""
        recommended_titles = []
        tappable_options = []
        time_first_token = None

        # Get metadata and conversation history
        print("Before retrieve metadata")
    
        conversation_history, watch_history = retrieve_metadata(
            profile or user_id, user_id, conversation_id
        )
        print("After retrieve metadata")
        previous_queries = get_previous_queries(conversation_history)
        print("After retrieve previous_queries")
        previous_tappable_options = get_previous_tappable_options(conversation_history)
        print("After previous_tappable_options")
        previous_suggested_content_ids = get_previous_suggested_content(
            conversation_history
        )
        print("After previous_suggested_content_ids")
        conversation_content = get_conversation_content(conversation_history)
        print("After get_conversation_content")
        conversation_content_w_tappables = get_conversation_content_with_tappables(
            conversation_history
        )
        print("After get_conversation_content_with_tappables")
        turn_count = get_turn_count(conversation_history)
        print("After get_turn_count")
        earliest_date, latest_date = get_date_range_from_conversation(
            conversation_history
        )
        print("After get_date_range_from_conversation")

        # Classify user intent
        classification = classify_user_intent(
            input_text, previous_queries, _add_llm_metrics
        )

        if classification.intent == "quote_search":
            quoted_movie = quote_to_title(input_text, _add_llm_metrics)
            if quoted_movie:
                classification.intent = "direct_search"
                input_text = quoted_movie
            else:
                classification.intent = "general_question"

        if classification.intent == "direct_search":
            current_query_with_convo = [
                {"role": "user", "content": input_text}
            ]   
        else: 
            current_query_with_convo = conversation_content + [
                {"role": "user", "content": input_text}
            ]
        rewritten_query = rewrite_user_query(
            watch_history.title_themes,
            current_query_with_convo,
            (earliest_date, latest_date),
            _add_llm_metrics,
        )

        # Process based on intent classification
        if classification.intent in ["theme_search", "direct_search"]:

            entity_type = {"Both": None, "TV_Show": "series", "Movie": "movie"}.get(
                rewritten_query.media_type
            )
            recommended_titles, tappable_options = (
                find_titles_and_tappable_options_by_query(
                    rewritten_query.query,
                    classification,
                    exclude_title_ids=previous_suggested_content_ids
                    + watch_history.title_ids,
                    exclude_themes=previous_tappable_options,
                    entity_type=entity_type,
                    turn_count=turn_count,
                )
            )
            # completion, time_first_token = respond_to_question(
            #     conversation_id,
            #     conversation_content_w_tappables
            #     + [{"role": "user", "content": input_text}],
            #     recommended_titles,
            #     rewritten_query.user_profile,
            #     _add_llm_metrics,
            # )
            
        elif classification.intent == "general_question":
            recommended_titles = find_recent_popular_titles(
                exclude_title_ids=previous_suggested_content_ids
                + watch_history.title_ids,
            )
            # completion, time_first_token = respond_to_general_question(
            #     conversation_id,
            #     conversation_content_w_tappables
            #     + [{"role": "user", "content": input_text}],
            #     recommended_titles,
            #     rewritten_query.user_profile,
            #     _add_llm_metrics,
            # )

        else:
            completion, time_first_token = respond_to_general_question(
                input_text, conversation_id, conversation_history, [], []
            )
    except Exception as e:
        logger.error(f"Couldn't invoke agent. {e}")
        emit_output_event(
            MessageChunk(
                conversation_id=conversation_id,
                content="Sorry, I'm having trouble right now. Please try again later.",
                is_final=True,
                order=0,
            )
        )
        raise

    # parsed_response = parse_xml(completion, ["response_text", "themes"])

    # llm_themes = [
    #     theme.strip()
    #     for theme in parsed_response.get("themes", "[]").strip("[]").split(",")
    #     if theme.strip()
    # ]

    return MessageOutput(
        conversation_id=conversation_id,
        # content=parsed_response.get("response_text"),
        # raw_content=completion,
        suggested_content=recommended_titles,
        # tappable_options=tappable_options,
        # time_to_first_token=time_first_token - start_time,
        # llm_tappable_options=llm_themes,
    )


def retrieve_metadata(
    profile: str, user_id: str, conversation_id: str
) -> tuple[list, WatchHistory]:
    start_time = time.perf_counter()
    with ThreadPoolExecutor(max_workers=3) as executor:
        logger.info(f"User ID: {user_id}")
        logger.info(f"Conversation ID: {conversation_id}")

        futures = {
            "watch_history": executor.submit(get_watch_history_by_profile_id, profile),
            "conversation_history": executor.submit(
                get_conversation, user_id, conversation_id
            ),
        }

        results = {name: future.result() for name, future in futures.items()}
    _metrics.add_metric(
        name="timeToRetrieveMetadata",
        unit=MetricUnit.Seconds,
        value=time.perf_counter() - start_time,
    )
    return (
        results["conversation_history"],
        results["watch_history"],
    )


def get_tappable_options(is_mock: bool = False) -> list[str]:
    if is_mock:
        return [
            "Recently Added on TMC ",
            "Cult Classic Movies",
            "Classic Award Winning Movies",
        ]
    return []


def _add_llm_metrics(
    name: str,
    total_time: float = None,
    input_tokens: int = None,
    output_tokens: int = None,
):
    if total_time:
        _metrics.add_metric(
            name=f"timeTo{name}Intent",
            unit=MetricUnit.Seconds,
            value=total_time,
        )
    if input_tokens:
        _metrics.add_metric(
            name=f"inputTokens{name}",
            unit=MetricUnit.Count,
            value=input_tokens,
        )
    if output_tokens:
        _metrics.add_metric(
            name=f"outputTokens{name}",
            unit=MetricUnit.Count,
            value=output_tokens,
        )
