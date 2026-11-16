"""OpenSearch Serverless helper for movie scene analysis.

Uses AWSV4SignerAuth with 'aoss' service for OpenSearch Serverless.
Points at the analysis collection (ANALYSIS_OSS_HOST) — separate from the
recommendation index used by retrieval.py.
"""

import os
import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth


class OpenSearchHelper:
    def __init__(self, host: str = None):
        self.region_name = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        credentials = boto3.Session().get_credentials()
        self.awsauth = AWSV4SignerAuth(credentials, self.region_name, "aoss")
        self.oss_client = None
        if host:
            self.oss_client = OpenSearch(
                hosts=[{"host": host, "port": 443}],
                http_auth=self.awsauth,
                use_ssl=True,
                verify_certs=True,
                connection_class=RequestsHttpConnection,
                timeout=300,
            )

    def search(self, query: dict, index: str) -> dict:
        return self.oss_client.search(body=query, index=index)
