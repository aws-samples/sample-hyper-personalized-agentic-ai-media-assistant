"""Resolve content URLs for movie playback.

Looks up the S3 video URL from DynamoDB and generates a presigned URL.
"""

import os

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError


def get_title_video_mapping_s3(title_id: str) -> str | None:
    """Look up the S3 video URL for a title_id from DynamoDB.

    Args:
        title_id: The catalog title ID (e.g. "md0000001").

    Returns:
        The S3 URL string, or None if not found.
    """
    dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    table = dynamodb.Table(os.environ.get("VIDEO_MAPPING_TABLE", "title_video_mapping"))
    try:
        response = table.query(
            KeyConditionExpression=Key("title_id").eq(title_id),
            Limit=1,
        )
        if response["Items"]:
            return response["Items"][0].get("video_s3_url")
        return None
    except Exception as e:
        print(f"Error retrieving video mapping for {title_id}: {e}")
        return None


def generate_presigned_url(s3_url: str, expiration: int = 3600) -> str | None:
    """Generate a presigned URL from an S3 URL.

    Args:
        s3_url: S3 URL in format s3://bucket/key or https://bucket.s3...
        expiration: URL expiration in seconds (default 1 hour).

    Returns:
        Presigned HTTPS URL, or None on failure.
    """
    try:
        # Parse s3://bucket/key format
        if s3_url.startswith("s3://"):
            parts = s3_url[5:].split("/", 1)
            bucket = parts[0]
            key = parts[1] if len(parts) > 1 else ""
        else:
            return s3_url  # Already an HTTPS URL, return as-is

        s3_client = boto3.client("s3", region_name=os.environ.get("MEDIA_REGION", os.environ.get("AWS_REGION", "us-east-1")))
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expiration,
        )
    except Exception as e:
        print(f"Error generating presigned URL for {s3_url}: {e}")
        return None


def get_content_url(title_id: str) -> str | None:
    """Get a playable presigned URL for a title_id.

    Returns None if the title has no video mapping. The mapping table is the
    playback allow-list: it holds only titles whose media the deployer uploaded
    (the micro-dramas, plus Tears of Steel when added).
    """
    s3_url = get_title_video_mapping_s3(title_id)
    if not s3_url:
        return None
    return generate_presigned_url(s3_url)
