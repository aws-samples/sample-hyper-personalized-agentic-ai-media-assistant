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

from agentic_conversation_handler import handle_conversation
from aws_lambda_powertools import Logger, Metrics, Tracer
from aws_lambda_powertools.logging import correlation_paths
from aws_lambda_powertools.utilities.data_classes import SQSEvent, event_source
from aws_lambda_powertools.utilities.data_masking import DataMasking
from aws_lambda_powertools.utilities.parser import envelopes, parse
from aws_lambda_powertools.utilities.typing import LambdaContext
from models import MessageInput
import uuid
import boto3
from urllib.parse import urlparse
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError
import os

tracer = Tracer()
logger = Logger()
data_masker = DataMasking()
metrics = Metrics()
media_content_bucket_name = os.environ.get("MEDIA_CONTENT_BUCKET_NAME", "")

def generate_presigned_url(bucket_name, object_name, expiration=3600):
    """
    Generate a presigned URL to share an S3 object.

    :param bucket_name: Name of the S3 bucket.
    :param object_name: Name of the object in the bucket.
    :param expiration: Time in seconds for the presigned URL to remain valid.
    :return: Presigned URL as a string. None if error occurs.
    """
    s3_client = boto3.client('s3')
    try:
        response = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': object_name},
            ExpiresIn=expiration
        )
        return response
    except ClientError as e:
        print(f"Error generating presigned URL: {e}")
        return None

def get_title_video_mapping_s3(title_id):
    """
    Retrieves the latest watch_log entry for a given username from the user_watch_log table.
    
    Args:
        username (str): The username to query for
        
    Returns:
        dict: The latest watch log entry, or None if no entries found
    """
    # Initialize DynamoDB resource
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table('title_video_mapping')
    
    try:
        # Query the table for the username, ordered by timestamp descending
        response = table.query(
            KeyConditionExpression=Key('title_id').eq(title_id),
            Limit=1  
        )
        
        # Check if any items were returned
        if response['Items']:
            title_video_mapping = response['Items'][0]
            
            return title_video_mapping["video_s3_url"]
        else:
            return None
            
    except Exception as e:
        print(f"Error retrieving watch log for title_id: {title_id}: {str(e)}")
        return None


@tracer.capture_lambda_handler
@metrics.log_metrics
def lambda_handler(event, context: LambdaContext) -> dict:
    """Lambda handler that creates or updates a conversation for a user"""

    # the logic below for assigning a user profile is intended for demo purposes.
    # The following authenticated users are assumed to have user affnity as defined as followed:
    # demo-user-action - user affinity towards action genre
    # demo-user-comedy - user affinity towards comedy genre
    # demo-user-drama - user affinity towards drama genre. 
    # The logic for determining user affinity is beyond the scope of the demo. 
    
    profile="COLD_START"
    if event["user"] == "demo-user-action":
        profile="ACTION"
    elif event["user"] == "demo-user-comedy":
        profile="COMEDY"
    elif event["user"] == "demo-user-drama":
        profile="DRAMA"
    
    payload = MessageInput(
      conversationId=event["conversation_id"] if "conversation_id" in event else str(uuid.uuid4()),
      content=event["user_query"],
      isNewChat=event["is_new_chat"] if "is_new_chat" in event else True,
      contentType="WRITTEN",
      profile=profile,
      user=event["user"])

    response = handle_conversation(metrics, payload.user, payload)
    response_message = response["response_message"]
    output_message = json.loads(response_message)
    suggested_contents = output_message["suggested_content"]
    final_response_contents = []
    for suggested_content in suggested_contents:
        final_response_content = {}
        final_response_content["title"] = suggested_content["title"]
        if suggested_content["image"].startswith("https://"):
            final_response_content["poster"] = suggested_content["image"]
        else:
            final_response_content["poster"] = generate_presigned_url(media_content_bucket_name, f"posters/{os.path.basename(suggested_content["image"])}")
        final_response_content["plot"] = suggested_content["synopsis"]
        final_response_content["actors"] = suggested_content["cast"]
        final_response_content["rating"] = (float(suggested_content["imdb_rating"]) / 2) if "imdb_rating" in suggested_content  else 0.0
        final_response_content["content_url"] = ""
        if suggested_content["id"] in ["tt2285752"]:
            s3_video_url = get_title_video_mapping_s3(suggested_content["id"])
            parsed = urlparse(s3_video_url)
            bucket_name = parsed.netloc
            s3_key = parsed.path.lstrip('/')
            video_presigned_url = generate_presigned_url(bucket_name, s3_key)
            final_response_content["content_url"] = video_presigned_url
        final_response_contents.append(final_response_content)
        print(final_response_contents)

    return final_response_contents