"""Provision mappings only in the stack's new collection; retain on deletion."""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def request(method, path, body=None):
    payload = json.dumps(body).encode() if body is not None else b""
    url = os.environ["COLLECTION_ENDPOINT"] + path
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    signed = AWSRequest(method=method, url=url, data=payload, headers={"Content-Type": "application/json",
        "X-Amz-Content-SHA256": hashlib.sha256(payload).hexdigest()})
    SigV4Auth(credentials, "aoss", os.environ["AWS_REGION"]).add_auth(signed)
    with urllib.request.urlopen(urllib.request.Request(url, data=payload if body is not None else None,
            headers=dict(signed.headers), method=method), timeout=30) as response:
        return json.loads(response.read() or b"{}")


def handler(event, context):
    name = event["ResourceProperties"]["IndexName"]
    if name not in {"catalog", "chapters", "segments", "audio-segments", "scripts"}:
        raise ValueError("Unexpected index")
    result = {"PhysicalResourceId": os.environ["COLLECTION_ENDPOINT"] + "/" + name}
    if event["RequestType"] == "Delete":
        return result
    properties = event["ResourceProperties"]["Mappings"]
    deadline = time.monotonic() + 480
    while True:
        try:
            try:
                request("GET", "/" + name)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                request("PUT", "/" + name, {"settings": {"index.knn": True},
                    "mappings": {"properties": properties}})
            else:
                # Additive mapping updates fail safely on incompatible types.
                request("PUT", "/" + name + "/_mapping", {"properties": properties})
            request("GET", "/" + name + "/_mapping")
            # Allow the Bedrock validator to observe a newly created index.
            time.sleep(30)
            return result
        except urllib.error.HTTPError as error:
            print(f"Index {name}: HTTP {error.code}; waiting for index/access readiness", flush=True)
            if error.code not in {401, 403, 404, 429, 500, 502, 503} or time.monotonic() >= deadline:
                raise RuntimeError(f"Index {name}: HTTP {error.code}: {error.read().decode()}") from error
            time.sleep(15)
