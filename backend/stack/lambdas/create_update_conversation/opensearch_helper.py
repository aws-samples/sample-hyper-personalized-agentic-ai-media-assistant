import json
import os
import random

import boto3
import numpy as np
from aws_lambda_powertools import Logger
from exceptions import EnvVarNotDefinedError
from models import QueryClassification, Title
from opensearchpy import OpenSearch, RequestsHttpConnection
from requests_aws4auth import AWS4Auth
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import pdist, squareform
from utils import get_entity_type_themes, get_era_themes
from typing import Any, Dict, Generator, List, Optional

# Setup AWS auth
session = boto3.Session()
credentials = session.get_credentials()

REGION = os.getenv("AWS_REGION")

logger = Logger()

OPENSEARCH_CLUSTER_URL = os.getenv("OPENSEARCH_CLUSTER_URL").removeprefix("https://")
if OPENSEARCH_CLUSTER_URL is None:
    logger.error("OPENSEARCH_CLUSTER_URL environment variable is not defined")
    raise EnvVarNotDefinedError("OPENSEARCH_CLUSTER_URL")

OPENSEARCH_CLUSTER_PORT = os.getenv("OPENSEARCH_CLUSTER_PORT")
if OPENSEARCH_CLUSTER_PORT is None:
    OPENSEARCH_CLUSTER_PORT = 443
    logger.info(
        f"OPENSEARCH_CLUSTER_PORT environment variable is not defined; using default {OPENSEARCH_CLUSTER_PORT}"
    )

else:
    OPENSEARCH_CLUSTER_PORT = int(OPENSEARCH_CLUSTER_PORT)

OPENSEARCH_RESULTS_COUNT = os.getenv("OPENSEARCH_RESULTS_COUNT")
if OPENSEARCH_RESULTS_COUNT is None:
    OPENSEARCH_RESULTS_COUNT = 30
    logger.info(
        f"OPENSEARCH_RESULTS_COUNT environment variable is not defined; using default {OPENSEARCH_RESULTS_COUNT}"
    )
else:
    OPENSEARCH_RESULTS_COUNT = int(OPENSEARCH_RESULTS_COUNT)

TITLE_INDEX = os.getenv("TITLE_INDEX")
if TITLE_INDEX is None:
    logger.error("TITLE_INDEX environment variable is not defined")
    raise EnvVarNotDefinedError("TITLE_INDEX")

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")
if EMBEDDING_MODEL is None:
    logger.error("EMBEDDING_MODEL environment variable is not defined")
    raise EnvVarNotDefinedError("EMBEDDING_MODEL")

RERANK_MODEL = os.getenv("RERANK_MODEL")
if RERANK_MODEL is None:
    logger.error("RERANK_MODEL environment variable is not defined")
    raise EnvVarNotDefinedError("RERANK_MODEL")

auth = AWS4Auth(region=REGION, service="aoss", refreshable_credentials=credentials)

# Connect to OpenSearch Serverless
os_client = OpenSearch(
    hosts=[{"host": OPENSEARCH_CLUSTER_URL, "port": OPENSEARCH_CLUSTER_PORT}],
    http_auth=auth,
    use_ssl=True,
    verify_certs=True,
    connection_class=RequestsHttpConnection,
)

bedrock_runtime = boto3.client(
    service_name="bedrock-runtime",
    region_name=REGION,
)

bedrock_rerank_runtime = boto3.client("bedrock-agent-runtime", region_name="us-west-2")


def find_recent_popular_titles(
    entity_type: str = None, exclude_title_ids: list = None
) -> list[Title]:
    """
    Returns a list of recent popular titles.

    Args:
        entity_type: The type of entity to filter by
        exclude_title_ids: List of title IDs to exclude

    Returns:
        List of recent popular titles
    """
    titles = _get_recent_popular_titles(entity_type, exclude_title_ids)

    logger.info(f"Found {len(titles)} unique recent popular titles")
    return titles


def find_titles_and_tappable_options_by_query(
    query: str,
    classification: QueryClassification,
    entity_type: str = None,
    exclude_title_ids: list = None,
    exclude_themes: list = None,
    title_limit: int = 3,
    turn_count: int = 0,
) -> tuple[list[Title], list]:
    query_embedding = _get_embedding(query)
    if classification.intent == "direct_search":
        titles = _get_titles_spearfishing(
            query_embedding,
            exclude_title_ids,
        )
    elif classification.intent == "theme_search":
        titles = _get_titles_from_query_v2(
            query_embedding,
            entity_type,
            exclude_title_ids,
            classification.animation_bias,
            classification.is_time_period,
            classification.kid_content,
            classification.is_award_winning,
        )
    if len(titles) < 2:
        reranked_titles = titles
    else:
        reranked_titles = _rerank_titles(titles, query)
    logger.info(f"### Query {query}")
    logger.info(f"### Unranked Titles: {[t.title for t in titles]}")
    logger.info(f"### Reranked Titles: {[t.title for t in reranked_titles]}")
    return reranked_titles[:title_limit], []


def _get_recent_popular_titles(
    entity_type: str = None, exclude_title_ids: list = None
) -> list[Title]:
    bool_query = {
        "must": [{"range": {"premiere_date": {"gte": "now-365d"}}}],
    }

    if exclude_title_ids:
        bool_query["must_not"] = [
            {"match": {"title_id": id}} for id in exclude_title_ids
        ]

    if entity_type:
        bool_query["must"].append({"match": {"title_type": entity_type}})

    search_body = {
        "query": {"bool": bool_query},
        "size": OPENSEARCH_RESULTS_COUNT,
        "sort": [
            {"imdb_rating": {"order": "desc"}},
            {"premiere_date": {"order": "desc"}},
        ],
        "_source": [
            "title_id",
            "originalTitle",
            "mpaa_ratings",
            "keywords",
            "premiere_date",
            "title_type",
            "principalCastMembers",
            "principalCrewMembers",
            "plot",
            "genres",
            "image",
            "awards",
            "imdb_rating"
        ],
    }
    response = os_client.search(index=TITLE_INDEX, body=search_body)

    titles = []
    for hit in response["hits"]["hits"]:
        source_obj = hit["_source"]
        t = Title(
            id=source_obj.get("title_id"),
            title=source_obj.get("originalTitle"),
            keywords=source_obj.get("keywords", []),
            entity_type=source_obj.get("title_type"),
            cast=source_obj.get("principalCastMembers", []),
            crew=source_obj.get("principalCrewMembers", []),
            is_kids_content=is_kids_content(source_obj.get("mpaa_ratings")),
            premiere_date=source_obj.get("premiere_date"),
            synopsis=source_obj.get("plot"),
            genres=source_obj.get("genres", []),
            imdb_rating=source_obj.get("imdb_rating", 0),
            image=source_obj.get("image", ""),
            awards=source_obj.get("awards", []))

        titles.append(t)
    return titles


def _get_most_dissimilar(embeddings: list[np.ndarray], n_groups: int) -> list[int]:
    """
    Returns indices of the most dissimilar embeddings by clustering.

    Args:
        embeddings: List of embeddings
        n_groups: Number of dissimilar embeddings to return

    Returns:
        List of indices of most dissimilar embeddings
    """
    if len(embeddings) <= n_groups:
        return list(range(len(embeddings)))

    # Compute pairwise cosine distances
    distances = pdist(embeddings, metric="cosine")

    # Hierarchical clustering
    linkage_matrix = linkage(distances, method="ward")
    clusters = fcluster(linkage_matrix, n_groups, criterion="maxclust")

    # Get one representative from each cluster (closest to centroid)
    representatives = []
    distance_matrix = squareform(distances)

    for cluster_id in range(1, n_groups + 1):
        cluster_indices = np.where(clusters == cluster_id)[0]
        if len(cluster_indices) == 1:
            representatives.append(cluster_indices[0])
        else:
            # Find embedding closest to cluster centroid
            cluster_distances = distance_matrix[
                np.ix_(cluster_indices, cluster_indices)
            ]
            centroid_idx = cluster_indices[np.argmin(cluster_distances.sum(axis=1))]
            representatives.append(centroid_idx)

    return representatives


def _get_most_dissimilar_greedy(
    embeddings: list[np.ndarray], n_groups: int
) -> list[int]:
    """
    Returns indices of the most dissimilar embeddings using greedy selection.

    Args:
        embeddings: List of embeddings
        n_groups: Number of dissimilar embeddings to return

    Returns:
        List of indices of most dissimilar embeddings
    """
    if len(embeddings) <= n_groups:
        return list(range(len(embeddings)))

    distance_matrix = squareform(pdist(embeddings, metric="cosine"))

    # Start with the two most distant points
    max_dist_idx = np.unravel_index(np.argmax(distance_matrix), distance_matrix.shape)
    selected = [max_dist_idx[0], max_dist_idx[1]]

    # Greedily add points that maximize minimum distance to selected points
    for _ in range(n_groups - 2):
        remaining = [i for i in range(len(embeddings)) if i not in selected]
        best_idx = max(
            remaining, key=lambda i: min(distance_matrix[i, j] for j in selected)
        )
        selected.append(best_idx)

    return selected

def _invoke_model(input_body: Dict[str, Any] = {}) -> Dict[str, Any]:
        """
        Note: Recommend input sanitization forthe methods when deploy in production, such as Bedrock Guardrails.
        """
        body = json.dumps(input_body)

        try:
            response = bedrock_runtime.invoke_model(
                body=body,
                modelId=EMBEDDING_MODEL,
                accept=accept,
                contentType=content_type,
            )

            response_metadata = response.get("ResponseMetadata", {})
            response_body = json.loads(response.get("body").read())
            return response_body

        except Exception as e:
            raise e

def _embedding_func(
        text: str, input_type: str = "GENERIC_RETRIEVAL") -> List[float]:
        """Call out to Bedrock embedding endpoint with a single text."""
        # replace newlines, which can negatively affect performance.
        text = text.replace(os.linesep, " ")

        # Create embedding
        request_body = {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": input_type,
                "embeddingDimension": 1024,
                "text": {"truncationMode": "END", "value": text},
            },
        }

        response = _invoke_model(input_body=request_body)
        embedding = response["embeddings"][0]["embedding"]
        return embedding

def _get_embedding(self, text: str, input_type="TEXT_RETRIEVAL") -> List[float]:
        """Compute query embeddings using a Bedrock model.

        Args:
            text: The text to embed.

        Returns:
            Embeddings for the text.

        """
        embedding = _embedding_func(text, input_type=input_type)
        return embedding

def is_kids_content(mpaa_ratings):
    if "ty-y" in mpaa_ratings or  "tv-y7" in mpaa_ratings:
        return True
    return False

def _get_titles_from_query_v2(
    query_embedding: list,
    entity_type: str = None,
    exclude_title_ids: list = None,
    animation_bias: bool = False,
    is_time_based_query: bool = False,
    is_kids_friendly: bool = False,
    is_award_winning: bool = False,
) -> list[Title]:

    bool_query = {
        "should": [
            {
                "script_score": {
                    "query": {"match_all": {}},
                    "script": {
                        "lang": "knn",
                        "source": "knn_score",
                        "params": {
                            "field": "embedding",
                            "query_value": query_embedding,
                            "space_type": "cosinesimil",
                        },
                    },
                    "boost": 2.5,
                }
            },
            # {"range": {"imdb_avg_rating": {"gte": 6.9, "boost": 0.2}}},
        ],
    }
    functions = [
        {
            "field_value_factor": {
                "field": "imdb_rating",
                "factor": 0.1,
                "modifier": "None",
                "missing": 0,
            }
        },
    ]
    if not is_time_based_query:
        bool_query["should"].extend(
            [
                {"range": {"premiere_date": {"gte": "now-8y", "boost": 0.1}}},
            ]
        )
        functions.append(
            {
                "field_value_factor": {
                    "field": "premiere_year",
                    "factor": 0.001,
                    "modifier": "sqrt",
                    "missing": 0,
                }
            },
        )

    if animation_bias:
        bool_query["should"].append(
            {
                "match": {
                    "keywords": {
                        "query": "animation",
                        "fuzziness": "AUTO",
                        "boost": 1.0,
                    }
                }
            }
        )

    if exclude_title_ids:
        bool_query["must_not"] = [
            {"match": {"title_id": id}} for id in exclude_title_ids
        ]

    if is_award_winning:
        if "must" not in bool_query:
            bool_query["must"] = [{"exists": {"field": "awards"}}]
        else:
            bool_query["must"].append({"exists": {"field": "awards"}})

    if is_kids_friendly:
        if "must" not in bool_query:
            bool_query["must"] = [{ "terms": { "mpaa_ratings": ["ty-y", "tv-y7"] }}]
        else:
            bool_query["must"].append({ "terms": { "mpaa_ratings": ["ty-y", "tv-y7"] }})

    if entity_type:
        if entity_type.lower() == "movie":
            if "must" not in bool_query:
                bool_query["must"] = [{"match": {"title_type": entity_type}}]
            else:
                bool_query["must"].append({"match": {"title_type": entity_type}})
        elif "tv" in entity_type.lower():
            if "must" not in bool_query:
                bool_query["must"] = [ { "query_string": { "fields": ["title_type"], "query": "tv*" } }]
            else:
                bool_query["must"].append({ "query_string": { "fields": ["title_type"], "query": "tv*" } })

    search_body = {
        "query": {
            "function_score": {
                "query": {"bool": bool_query},
                "functions": functions,
                "boost_mode": "sum",
                "score_mode": "sum",
            }
        },
        "size": OPENSEARCH_RESULTS_COUNT,
        "_source": [
            "title_id",
            "originalTitle",
            "mpaa_ratings",
            "keywords",
            "premiere_date",
            "title_type",
            "principalCastMembers",
            "principalCrewMembers",
            "plot",
            "genres",
            "image",
            "awards",
            "imdb_rating"
        ],
    }

    logger.info(search_body)

    response = os_client.search(index=TITLE_INDEX, body=search_body)

    titles = []
    seen_franchises = set()
    animated_content_count = 0
    for hit in response["hits"]["hits"]:
        source_obj = hit["_source"]
        
        t = Title(
            id=source_obj.get("title_id"),
            title=source_obj.get("originalTitle"),
            keywords=source_obj.get("keywords", []),
            entity_type=source_obj.get("title_type"),
            cast=source_obj.get("principalCastMembers", []),
            crew=source_obj.get("principalCrewMembers", []),
            is_kids_content=is_kids_content(source_obj.get("mpaa_ratings")),
            premiere_date=source_obj.get("premiere_date"),
            synopsis=source_obj.get("plot"),
            genres=source_obj.get("genres", []),
            imdb_rating=source_obj.get("imdb_rating", 0),
            image=source_obj.get("image", ""),
            awards=source_obj.get("awards", [])
        )

        ## This makes sure that we only have 1 animated content at most in our returned results
        if not animation_bias and "animation" in ",".join(t.keywords):
            animated_content_count += 1
            if animated_content_count > 1:
                continue

        titles.append(t)
    logger.info(f"Found {len(titles)} titles")
    logger.info(f"Titles: {[t.model_dump() for t in titles]}")
    return titles


def _rerank_titles(titles: list[Title], query: str) -> list[Title]:
    rerank_body = {
        "queries": [
            {"textQuery": {"text": query}, "type": "TEXT"},
        ],
        "rerankingConfiguration": {
            "bedrockRerankingConfiguration": {
                "modelConfiguration": {
                    "modelArn": RERANK_MODEL,
                },
                "numberOfResults": len(titles),
            },
            "type": "BEDROCK_RERANKING_MODEL",
        },
        "sources": [
            {
                "inlineDocumentSource": {
                    "textDocument": {
                        "text": t.model_dump_json(exclude={"themes", "id"})
                        # f"A {t.entity_type} titled '{t.title}' released on {t.premiere_date}. {' Featured cast includes ' + ', '.join(t.cast) if t.cast else ''}. Key themes: {', '.join(t.themes)}. Normalized popularity score (0-1): {t.popularity}. {' Part of ' + ', '.join(t.franchises) + ' franchise.' if t.franchises else ''}"
                    },
                    "type": "TEXT",
                },
                "type": "INLINE",
            }
            for t in titles
        ],
    }
    response = bedrock_rerank_runtime.rerank(**rerank_body)
    logger.info(response)
    if "results" in response:
        return [titles[r["index"]] for r in response["results"]]
    return titles


def _get_titles_spearfishing(
    query_embedding: list,
    exclude_title_ids: list = None,
) -> list[Title]:

    bool_query = {
        "should": [
            {
                "script_score": {
                    "query": {"match_all": {}},
                    "script": {
                        "lang": "knn",
                        "source": "knn_score",
                        "params": {
                            "field": "embedding",
                            "query_value": query_embedding,
                            "space_type": "cosinesimil",
                        },
                    },
                    "boost": 2.5,
                }
            },
            # {"range": {"imdb_rating": {"gte": 6.9, "boost": 0.2}}},
        ],
    }

    if exclude_title_ids:
        bool_query["must_not"] = [
            {"match": {"title_id": id}} for id in exclude_title_ids
        ]

    search_body = {
        "query": {"bool": bool_query},
        "size": OPENSEARCH_RESULTS_COUNT,
        "_source": [
            "title_id",
            "originalTitle",
            "mpaa_ratings",
            "keywords",
            "premiere_date",
            "title_type",
            "principalCastMembers",
            "principalCrewMembers",
            "plot",
            "genres",
            "image",
            "awards",
            "imdb_rating"
        ],
    }

    logger.info(search_body)

    response = os_client.search(index=TITLE_INDEX, body=search_body)

    titles = []
    for hit in response["hits"]["hits"]:
        source_obj = hit["_source"]

        t = Title(
            id=source_obj.get("title_id"),
            title=source_obj.get("originalTitle"),
            keywords=source_obj.get("keywords", []),
            entity_type=source_obj.get("title_type"),
            cast=source_obj.get("principalCastMembers", []),
            crew=source_obj.get("principalCrewMembers", []),
            is_kids_content=is_kids_content(source_obj.get("mpaa_ratings")),
            premiere_date=source_obj.get("premiere_date"),
            synopsis=source_obj.get("plot"),
            genres=source_obj.get("genres", []),
            imdb_rating=source_obj.get("imdb_rating", 0),
            image=source_obj.get("image", ""),
            awards=source_obj.get("awards", []))

        titles.append(t)
    logger.info(f"Found {len(titles)} titles")
    logger.info(f"Titles: {[t.model_dump() for t in titles]}")
    return titles
