import time 
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth, RequestError
import json 
import boto3

def _ensure_index_ready(region, host, index_name, timeout=60):
        """
        Block until index is ready, without using sleep.
        Uses OpenSearch client's built-in retry and timeout mechanisms.
        """
        credentials = boto3.Session().get_credentials()
        awsauth = AWSV4SignerAuth(credentials, region, 'aoss')

        oss_client = OpenSearch(
            hosts=[{'host': host, 'port': 443}],
            http_auth=awsauth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=300
        )
        try:
            # This will block and retry internally until the index is available or timeout
            # The OpenSearch client handles the waiting logic
            oss_client.indices.get(
                index=index_name,
                params={
                    "timeout": f"{timeout}s",
                    "master_timeout": f"{timeout}s"
                }
            )
            print(f"Index {index_name} is ready")
            return True
        except Exception as e:
            raise Exception(f"Index {index_name} is not ready after {timeout}s: {str(e)}")

def create_vector_index(kb_index_name: str, 
                        title_catalog_index_name: str, 
                        bda_video_level_detail_index_name: str,
                        bda_video_chapter_index_name: str,
                        bda_video_chapter_audio_segment_index_name: str,
                        bda_video_chapter_segment_index_name: str,
                        host: str, 
                        region: str):
        """
        Create OpenSearch Serverless vector index. If existent, ignore
        Args:
            index_name: name of the vector index
        """
        credentials = boto3.Session().get_credentials()
        awsauth = AWSV4SignerAuth(credentials, region, 'aoss')

        oss_client = OpenSearch(
            hosts=[{'host': host, 'port': 443}],
            http_auth=awsauth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            timeout=300
        )
        
        kb_index = {
            "settings": {
                "index.knn": "true",
                "number_of_shards": 1,
                "knn.algo_param.ef_search": 512,
                "number_of_replicas": 0,
            },
            "mappings": {
                "properties": {
                    "vector": {
                        "type": "knn_vector",
                        "dimension": 1024,
                        "method": {
                            "name": "hnsw",
                            "engine": "faiss",
                            "space_type": "l2"
                        },
                    },
                    "text": {
                        "type": "text"
                    },
                    "text-metadata": {
                        "type": "text"}
                }
            }
        }

        title_catalog_index =  {
            "mappings": {
                "properties": {
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": 1024,
                        "method": {
                            "engine": "nmslib",
                            "space_type": "cosinesimil",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16}
                        }
                    },
                    "title_id": {
                        "type": "text",
                        "store": True
                    },
                    "originalTitle": {
                        "type": "text",
                        "store": True
                    },
                    "principalCastMembers": {
                        "type": "text",
                        "store" : True
                    },
                    "principalCrewMembers": {
                        "type": "text",
                        "store" : True
                    },
                    "mpaa_ratings": {
                        "type": "keyword",
                        "store": True
                    },
                    "genres": {
                        "type": "text",
                        "store": True
                    },
                    "imdb_url": {
                        "type": "text",
                        "store": True
                    },
                    "image": {
                        "type": "text",
                        "store": True
                    },
                    "keywords": {
                        "type": "keyword",
                        "store": True
                    },
                    "plot": {
                        "type": "text",
                        "store": True
                    },
                    "title_type": {
                        "type": "keyword",
                        "store": True
                    },
                    "imdb_rating" : {
                        "type" : "float",
                        "store" : True
                    },
                    "premiere_date": {
                        "type": "date",
                        "format" : "basic_date"
                    },
                    "premiere_year": {
                        "type": "integer",
                        "store" : True
                    },
                    "title_text": {
                        "type": "text",
                        "store" : True
                    },
                    "awards" : {
                        "type" : "keyword",
                        "store" : True
                    }
                }
            },
            "settings": {
                "index": {
                    "number_of_shards": 2,
                    "knn.algo_param": {"ef_search": 512},
                    "knn": True
                }
            }
        }

        bda_video_index =  {
            "mappings": {
                "properties": {
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": 1024,
                        "method": {
                            "engine": "nmslib",
                            "space_type": "cosinesimil",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16}
                        }
                    },
                    "summary": {
                        "type": "text",
                        "store": True
                    },
                    "video_name": {
                        "type": "keyword",
                        "store": True
                    },
                    "start_timestamp_millis": {
                        "type": "float",
                        "store" : True
                    },
                    "end_timestamp_millis": {
                        "type": "float",
                        "store" : True
                    },
                    "src_media_s3_location": {
                        "type": "text",
                        "store": True
                    }
                }
            },
            "settings": {
                "index": {
                    "number_of_shards": 2,
                    "knn.algo_param": {"ef_search": 512},
                    "knn": True
                }
            }
        }

        bda_video_chapter_index =  {
            "mappings": {
                "properties": {
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": 1024,
                        "method": {
                            "engine": "nmslib",
                            "space_type": "cosinesimil",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16}
                        }
                    },
                    "summary": {
                        "type": "text",
                        "store": True
                    },
                    "video_name": {
                        "type": "keyword",
                        "store": True
                    },
                    "start_timestamp_millis": {
                        "type": "float",
                        "store" : True
                    },
                    "end_timestamp_millis": {
                        "type": "float",
                        "store" : True
                    },
                    "src_media_s3_location": {
                        "type": "text",
                        "store": True
                    },
                    "chapter": {
                        "type": "integer",
                        "store": True
                    },
                    "chapter_transcription": {
                        "type": "text",
                        "store": True
                    },   
                }
            },
            "settings": {
                "index": {
                    "number_of_shards": 2,
                    "knn.algo_param": {"ef_search": 512},
                    "knn": True
                }
            }
        }

        bda_video_chapter_audio_segment_index =  {
            "mappings": {
                "properties": {
                    "video_name": {
                        "type": "keyword",
                        "store": True
                    },
                    "start_timestamp_millis": {
                        "type": "float",
                        "store" : True
                    },
                    "end_timestamp_millis": {
                        "type": "float",
                        "store" : True
                    },
                    "src_media_s3_location": {
                        "type": "text",
                        "store": True
                    },
                    "chapter": {
                        "type": "integer",
                        "store": True
                    },
                    "chapter_segment": {
                        "type": "integer",
                        "store": True
                    },
                    "text": {
                        "type": "text",
                        "store": True
                    }
                }
            },
            "settings": {
                "index": {
                    "number_of_shards": 2,
                    "knn.algo_param": {"ef_search": 512},
                    "knn": True
                }
            }
        }

        bda_video_chapter_segment_index =  {
            "mappings": {
                "properties": {
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": 1024,
                        "method": {
                            "engine": "nmslib",
                            "space_type": "cosinesimil",
                            "name": "hnsw",
                            "parameters": {"ef_construction": 512, "m": 16}
                        }
                    },
                    "video_name": {
                        "type": "keyword",
                        "store": True
                    },
                    "start_timestamp_millis": {
                        "type": "float",
                        "store" : True
                    },
                    "end_timestamp_millis": {
                        "type": "float",
                        "store" : True
                    },
                    "src_media_s3_location": {
                        "type": "text",
                        "store": True
                    },
                    "chapter": {
                        "type": "integer",
                        "store": True
                    },
                    "chapter_segment": {
                        "type": "integer",
                        "store": True
                    },
                    "celebrities": {
                        "type": "text",
                        "store": True
                    }
                }
            },
            "settings": {
                "index": {
                    "number_of_shards": 2,
                    "knn.algo_param": {"ef_search": 512},
                    "knn": True
                }
            }
        }

        # Create index
        try:
            print('\nCreating index for KB')
            response = oss_client.indices.create(index=kb_index_name, body=json.dumps(kb_index))
        except RequestError as e:
            # you can delete the index if its already exists
            oss_client.indices.delete(index=kb_index_name)
            print(
                f'Error while trying to create the index, with error {e.error}\nyou may unmark the delete above to '
                f'delete, and recreate the index')
            response = oss_client.indices.create(index=kb_index_name, body=json.dumps(kb_index))
        print(f'index: {kb_index_name} created successfully')            

        try:
            print('\nCreating index for Title Catalog')
            response = oss_client.indices.create(index=title_catalog_index_name, body=json.dumps(title_catalog_index))
        except RequestError as e:
            # you can delete the index if its already exists
            oss_client.indices.delete(index=title_catalog_index_name)
            print(
                f'Error while trying to create the index, with error {e.error}\nyou may unmark the delete above to '
                f'delete, and recreate the index')
            response = oss_client.indices.create(index=title_catalog_index_name, body=json.dumps(title_catalog_index))
        print(f'index: {title_catalog_index_name} created successfully')

        try:
            print('\nCreating index for bda_video_index')
            response = oss_client.indices.create(index=bda_video_level_detail_index_name, body=json.dumps(bda_video_index))
        except RequestError as e:
            # you can delete the index if its already exists
            oss_client.indices.delete(index=bda_video_level_detail_index_name)
            print(
                f'Error while trying to create the index, with error {e.error}\nyou may unmark the delete above to '
                f'delete, and recreate the index')
            response = oss_client.indices.create(index=bda_video_level_detail_index_name, body=json.dumps(bda_video_index))
        print(f'index: {bda_video_level_detail_index_name} created successfully')

        try:
            print('\nCreating index for bda_video_chapter_index')
            response = oss_client.indices.create(index=bda_video_chapter_index_name, body=json.dumps(bda_video_chapter_index))
        except RequestError as e:
            # you can delete the index if its already exists
            oss_client.indices.delete(index=bda_video_chapter_index_name)
            print(
                f'Error while trying to create the index, with error {e.error}\nyou may unmark the delete above to '
                f'delete, and recreate the index')
            response = oss_client.indices.create(index=bda_video_chapter_index_name, body=json.dumps(bda_video_chapter_index))
        print(f'index: {bda_video_chapter_index_name} created successfully')

        try:
            print('\nCreating index for bda_video_chapter_audio_segment_index')
            response = oss_client.indices.create(index=bda_video_chapter_audio_segment_index_name, body=json.dumps(bda_video_chapter_audio_segment_index))
        except RequestError as e:
            # you can delete the index if its already exists
            oss_client.indices.delete(index=bda_video_chapter_audio_segment_index_name)
            print(
                f'Error while trying to create the index, with error {e.error}\nyou may unmark the delete above to '
                f'delete, and recreate the index')
            response = oss_client.indices.create(index=bda_video_chapter_audio_segment_index_name, body=json.dumps(bda_video_chapter_audio_segment_index))
        print(f'index: {bda_video_chapter_audio_segment_index_name} created successfully')

        try:
            print('\nCreating index for bda_video_chapter_segment_index')
            response = oss_client.indices.create(index=bda_video_chapter_segment_index_name, body=json.dumps(bda_video_chapter_segment_index))
        except RequestError as e:
            # you can delete the index if its already exists
            oss_client.indices.delete(index=bda_video_chapter_segment_index_name)
            print(
                f'Error while trying to create the index, with error {e.error}\nyou may unmark the delete above to '
                f'delete, and recreate the index')
            response = oss_client.indices.create(index=bda_video_chapter_segment_index_name, body=json.dumps(bda_video_chapter_segment_index))
        print(f'index: {bda_video_chapter_segment_index_name} created successfully')

        return response
            
def lambda_handler(event, context):
    kb_index_name = event["ResourceProperties"]["kb_index_name"]
    title_catalog_index_name = event["ResourceProperties"]["title_catalog_index_name"]
    bda_video_level_detail_index_name = event["ResourceProperties"]["bda_video_level_detail_index_name"] 
    bda_video_chapter_index_name = event["ResourceProperties"]["bda_video_chapter_index_name"]
    bda_video_chapter_audio_segment_index_name = event["ResourceProperties"]["bda_video_chapter_audio_segment_index_name"]
    bda_video_chapter_segment_index_name = event["ResourceProperties"]["bda_video_chapter_segment_index_name"]
    host = event["ResourceProperties"]["opensearch_host"]
    region = event["ResourceProperties"]["region"]
    hostname = host.removeprefix("https://")
    response = create_vector_index(kb_index_name=kb_index_name, 
                title_catalog_index_name=title_catalog_index_name, 
                bda_video_level_detail_index_name=bda_video_level_detail_index_name,
                bda_video_chapter_index_name=bda_video_chapter_index_name,
                bda_video_chapter_audio_segment_index_name=bda_video_chapter_audio_segment_index_name,
                bda_video_chapter_segment_index_name=bda_video_chapter_segment_index_name,
                host=hostname, 
                region=region)
    _ensure_index_ready(region, host, kb_index_name)
    return {
        'statusCode': 200,
        'body': json.dumps('Opensearch Serverless Index updated successfully')
    }


         

