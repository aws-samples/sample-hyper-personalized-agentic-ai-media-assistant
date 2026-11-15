"""Gateway-compatible web search using configured Nova grounding inference."""
import os
import boto3
from botocore.config import Config


def handler(event, context):
    query = str(event.get("query", "")).strip()
    if not query or len(query) > 1000:
        raise ValueError("Provide a query of 1–1000 characters")
    limit = max(1, min(int(event.get("maxResults", 5)), 10))
    response = boto3.client("bedrock-runtime", region_name=os.environ["MODEL_REGION"], config=Config(read_timeout=110)).converse(
        modelId=os.environ["MODEL_ID"],
        system=[{"text": "Search for the requested football information. Keep the answer concise and include source citations."}],
        messages=[{"role": "user", "content": [{"text": query}]}],
        toolConfig={"tools": [{"systemTool": {"name": "nova_grounding"}}]},
        inferenceConfig={"maxTokens": 1500})
    blocks = response["output"]["message"]["content"]
    texts, urls = [], []
    for block in blocks:
        if block.get("text"):
            texts.append(block["text"])
        citation_content = block.get("citationsContent", {})
        if isinstance(citation_content, dict):
            texts.extend(x["text"] for x in citation_content.get("content", []) if "text" in x)
            citations = citation_content.get("citations", [])
        else:
            citations = citation_content
        for citation in citations:
            url = citation.get("location", {}).get("web", {}).get("url")
            if url and url not in urls:
                urls.append(url)
    text = " ".join(texts)
    # Return only sourced results; never invent URLs for ungrounded responses.
    return {"results": [{"text": text, "url": url} for url in urls[:limit]]}
