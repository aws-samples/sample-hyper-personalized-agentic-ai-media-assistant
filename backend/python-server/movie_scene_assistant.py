from strands import Agent
import boto3
import re
import os
import time
from boto3.dynamodb.conditions import Key
from utils import OpenSearchHelper

scene_understanding_model_id = os.environ.get("MOVIE_SCENE_UNDERSTANDING_MODEL_ID", "us.amazon.nova-micro-v1:0")
rerank_model_arn = os.environ.get("MOVIE_SCENE_UNDERSTANDING_RERANKING_MODEL_ARN", "arn:aws:bedrock:us-west-2::foundation-model/cohere.rerank-v3-5:0")
oss_host = os.environ.get("OSS_HOST", "")
knowledge_base_id = os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID", "")
video_chapter_index = os.environ.get("BDA_VIDEO_CHAPTER_INDEX_NAME", "")
video_chapter_segment_index = os.environ.get("BDA_VIDEO_CHAPTER_SEGMENT_INDEX_NAME", "")
video_chapter_audio_segment_index_name = os.environ.get("BDA_VIDEO_CHAPTER_AUDIO_SEGMENT_INDEX_NAME", "")

class MovieSceneAssistant:
    def __init__(self, authenticated_user):
        self.authenticated_user = authenticated_user
        self.movie_scene_classfier_agent = Agent(model=scene_understanding_model_id)
        self.movie_general_question_agent = Agent(model=scene_understanding_model_id)
        self.bedrock_agent_runtime_rerank_client = boto3.client('bedrock-agent-runtime', region_name="us-west-2")
        self.bedrock_agent_runtime_client = boto3.client('bedrock-agent-runtime')
        self.oss_helper = OpenSearchHelper(host=oss_host.removeprefix("https://"))
        self.system_prompt = """You are a professional story teller. 
You job is to curate a compelling story based on the given movie scenes from the original script, and optionally dialogs that follow the scene. The scene and dialogs are in the <scene> and <dialog> XML tags.
Here are the guidelines:

- You are to first summarize your story based on the given scenes, chapters and dialog information, then return the summarized story in story telling style.
- Be as creative in the story telling as possible. If the characters or the celebrities are available in the scene, make sure to use the character name directly.
- Be very detail about describing the story, focused on the characters more so than the surrounding.
- Do not mention anything about chapter, or scene explicitly in your summary.

The scenes given in the context are organized in the right sequence, please follow the exact order of the scenes when providing the summary.

Put your thoughts in the <thinking> XML tag. Then provide your story in the <story> XML tag."""

        self.get_movie_detail_general_prompt_template = """You are a movie assistant. You task is to provide an answer to the user question about a movie given the context of scenes or plots for the movie. 
Your answer must be derived from the given context only. Do not make up any answers on your own.
Summarize the answer that aligns with the user question. Make it sound interesting that captures the attention of the user. 
Only provide the summary, nothing else, do not provide any other explainations.

Here's the user's question: $query$

Here's the given context about the scene or plot in a movie the user is interested:

$search_results$


"""
        self.agent = Agent(model=scene_understanding_model_id, system_prompt=self.system_prompt)
        
    def movieScene_classfier(self, question):
        response = self.movie_scene_classfier_agent(f"""given the {question}, you task is to classify the type of intent it belong. You must only return one of these intents:

    1. movie_scene_detail - question is about the specific scene in a movie.
    2. movie_actor - question is about actor / actresses in the scene.
    3. movie_overall_story - question is beyond the current scene, requires connections between scenes in the whole movie to reveal the detail
                                            
    For example, 

    question: What's going on in this scene? 
    answer: movie_scene_detail
                                            
    question: Who are the actors in scene? 
    answer: movie_actor

    question: Why is she angry about him? 
    answer: movie_overall_story
        
    Put your result in <classification> XML tag. Do not provide any other explanations. 
    """)
        response_message = response.message["content"][0]["text"]
        pattern = r"<classification>(.*?)</classification>"
        matches = re.findall(pattern, response_message, re.DOTALL)
        classification = matches[0]
        return classification
    
    def rerank_results(self, query, docs):
        sources = [
        {
            "inlineDocumentSource": {
                "textDocument": {"text": doc},
                "type": "TEXT"
            },
            "type": "INLINE"
        }
        for doc in docs
    ]
        response = self.bedrock_agent_runtime_rerank_client.rerank(
            queries=[
                {
                    'textQuery': {
                        'text': query
                    },
                    'type': 'TEXT'
                },
            ],
            rerankingConfiguration={
                'bedrockRerankingConfiguration': {
                    'modelConfiguration': {
                        'modelArn': rerank_model_arn
                    },
                    'numberOfResults': 3
                },
                'type': 'BEDROCK_RERANKING_MODEL'
            },
            sources=sources
        )
        reranked_docs = []
        for reranked_doc in response["results"]:
            reranked_docs.append(docs[reranked_doc['index']])
        return reranked_docs
    
    def retrieve_top_result(self, knowledge_base_id, query, video_name):
        response = self.bedrock_agent_runtime_client.retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={'text': query},
            retrievalConfiguration={'vectorSearchConfiguration': {
                'filter': {
                    'equals': {
                        'key': 'video_name',
                        'value': video_name
                    }
                },
                'numberOfResults': 5,
                "overrideSearchType": "HYBRID"
                }
            }
        )
        #TODO: check for no results.
        rertrieved_results = [ x["content"]["text"] for x in response["retrievalResults"] ]
        if len(rertrieved_results) == 0:
            return None
        if len(rertrieved_results) > 1:
            reranked_results = self.rerank_results(query, rertrieved_results)
        else:
            reranked_results = rertrieved_results #single result
        return reranked_results
    
    def current_chapter_query(self, millis, video_chapter_index, video_name):
        print(f"calling current_chapter_query")
        aoss_query = {
            "size" : 10,
            "query": {
                "function_score": {
                    "query": {
                        "bool": {
                        "filter": [
                            { "range": { "start_timestamp_millis": { "lte" : millis } } },
                            { "range": { "end_timestamp_millis": { "gt" : millis } } },
                            { "term" : { "video_name": video_name}}
                            ]
                        }
                    }
                }
            },
            "_source" : ["chapter", "video_name", "summary", "start_timestamp_millis", "end_timestamp_millis"]
        }
        response  = self.oss_helper.search(query=aoss_query, index=video_chapter_index)
        hits = response["hits"]["hits"]
        return hits
    
    def current_chapter_segment_query(self, millis, video_chapter_segment_index, video_name):
        start_millis_search_begin = millis - 5000 # add 5 seconds before
        aoss_query = {
            "size" : 10,
            "query": {
                "function_score": {
                    "query": {
                        "bool": {
                        "filter": [
                            { "range": { "end_timestamp_millis": { "gte" : start_millis_search_begin } } },
                            { "range": { "start_timestamp_millis": { "lte" : millis } } },
                            { "term" : { "video_name": video_name}}
                            ]
                        }
                    }
                }
            },
            "_source" : ["chapter", "video_name", "celebrities", "start_timestamp_millis", "end_timestamp_millis"]
        }
        response  = self.oss_helper.search(query=aoss_query, index=video_chapter_segment_index)
        hits = response["hits"]["hits"]
        return hits
    
    def previous_chapter_query(self, video_chapter_index, video_name, chapter):
        aoss_query = {
            "size" : 10,
            "query": {
                "function_score": {
                    "query": {
                        "bool": {
                        "filter": [
                            { "term" : { "video_name": video_name}},
                            { "term" : { "chapter": chapter}}
                            ]
                        }
                    }
                }
            },
            "_source" : ["chapter", "video_name", "summary", "start_timestamp_millis", "end_timestamp_millis"]
        }
        response  = self.oss_helper.search(query=aoss_query, index=video_chapter_index)
        hits = response["hits"]["hits"]
        return hits
    
    def current_chapter_audio_segment_query(self, video_chapter_audio_segment_index, video_name, chapter, time_millis):
        print(f"calling current_chapter_audio_segment_query")
        aoss_query = {
            "size" : 100,
            "query": {
                "function_score": {
                    "query": {
                        "bool": {
                        "filter": [
                            { "term" : { "video_name": video_name}},
                            { "term" : { "chapter": chapter}},
                            { "range" : { "end_timestamp_millis" : { "lt" : time_millis}}}
                            ]
                        }
                    }
                }
            },
            "_source" : ["chapter", "chapter_segment", "video_name", "text", "start_timestamp_millis", "end_timestamp_millis"]
        }
        response  = self.oss_helper.search(query=aoss_query, index=video_chapter_audio_segment_index)
        hits = response["hits"]["hits"]
        return hits


    def process_chapter_summary_response(self, chapter_query_response, video_name):
        print("inside process_chapter_summary_response")
        chapter_summary = []
        total_chapter_duration = 0
        while True:
            chapter_duration = chapter_query_response["_source"]["end_timestamp_millis"] - chapter_query_response["_source"]["start_timestamp_millis"]
            total_chapter_duration += chapter_duration
            chapter_summary = [chapter_query_response["_source"]["summary"]] +  chapter_summary
            if total_chapter_duration < 10*1000: # if the chapter is less than 30 seconds, get the last chapter
                current_chapter = chapter_query_response["_source"]["chapter"]
                previous_chapter = current_chapter - 1
                if previous_chapter > 0:
                    previous_chapter_query_response = self.previous_chapter_query(video_chapter_index, video_name, previous_chapter)[0]
                    chapter_query_response = previous_chapter_query_response
                else:
                    break
            else:
                break
        return chapter_summary
    
    def process_video_progress(self, current_progress_millis, video_name):
        print("inside process_video_progress")
        current_chapter_query_response = self.current_chapter_query(current_progress_millis, video_chapter_index, video_name=video_name)[0] 
        current_watch_time_progress = current_progress_millis - current_chapter_query_response["_source"]["start_timestamp_millis"]
        current_chapter_duration = current_chapter_query_response["_source"]["end_timestamp_millis"] - current_chapter_query_response["_source"]["start_timestamp_millis"]
        current_watch_time_progress_percentage = current_watch_time_progress / current_chapter_duration
        current_chapter = current_chapter_query_response["_source"]["chapter"]    
        print(f"current chapter is: {current_chapter}")
        chapter_segment_dialog = ""
        if current_watch_time_progress_percentage < .3 and current_chapter > 1: # if less than 30% and not chapter 1, then we will capture the previous chapter. 
            previous_chapter = current_chapter - 1
            previous_chapter_query_response = self.previous_chapter_query(video_chapter_index, video_name, previous_chapter)[0]
            chapter_summary = self.process_chapter_summary_response(previous_chapter_query_response, video_name)
            current_chapter_audio_segment_response = self.current_chapter_audio_segment_query(video_chapter_audio_segment_index_name, video_name, current_chapter, current_progress_millis)
            if len(current_chapter_audio_segment_response) > 0:
                sorted_chapter_segment_data = sorted(current_chapter_audio_segment_response, key=lambda x: x["_source"]["chapter_segment"])
                chapter_segment_dialog = " ".join([ x["_source"]["text"] for x in sorted_chapter_segment_data ])
        else:
            chapter_summary = self.process_chapter_summary_response(current_chapter_query_response, video_name)
            

        script_results = []
        combined_chapter_summary = "\n".join(chapter_summary)
        # for chapter in chapter_summary:
        script_kb = self.retrieve_top_result(knowledge_base_id=knowledge_base_id, query=combined_chapter_summary, video_name=video_name)
        if script_kb:
            script_result = script_kb[0]
        else:
            script_result = combined_chapter_summary
            
        print(script_result)

        prompt = f"""<scene>
    {script_result}
    </scene>

    <dialog>
    {chapter_segment_dialog}
    </dialog>

    """
        response = self.agent(prompt)
        pattern = r'<story.*?>(.*?)</story>'
        response_message = response.message["content"][0]["text"]
        matches = re.findall(pattern, response_message, re.DOTALL)
        if len(matches) > 0:
            return matches[0]
        else:
            return response_message
        
    
    def insert_user_watch_log(self, username: str, timemillis: int, video_name: str) -> dict:
        """
        Insert a record into DynamoDB table with video information.
        
        Args:
            username: Username string
            timemillis: User provided milliseconds
            video_name: Name of the video
            region: AWS region (default: us-east-1)
        
        Returns:
            dict: Response from DynamoDB put_item operation
        """
        # Initialize DynamoDB resource
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table("user_watch_log")
        
        # Get current timestamp in milliseconds
        current_timestamp = int(time.time() * 1000)
        
        # Prepare the item to insert
        item = {
            'username': username,
            'current_time': current_timestamp,
            'timemillis': timemillis,
            'video_name': video_name
        }
        
        try:
            # Insert the item into DynamoDB
            response = table.put_item(Item=item)
            
            print(f"Successfully inserted record for user: {username}")
            return response
            
        except Exception as e:
            print(f"Error inserting record: {str(e)}")
            raise

    
    def get_latest_watch_log(self):
        """
        Retrieves the latest watch_log entry for a given username from the user_watch_log table.
        
        Returns:
            dict: The latest watch log entry, or None if no entries found
        """
        # Initialize DynamoDB resource
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table('user_watch_log')
        
        try:
            # Query the table for the username, ordered by timestamp descending
            response = table.query(
                KeyConditionExpression=Key('username').eq(self.authenticated_user),
                ScanIndexForward=False,  # Sort in descending order (latest first)
                Limit=1  # Only get the most recent entry
            )
            
            # Check if any items were returned
            if response['Items']:
                latest_log = response['Items'][0]
                
                return latest_log
            else:
                return None
                
        except Exception as e:
            print(f"Error retrieving watch log for user {self.authenticated_user}: {str(e)}")
            return None
        

    def get_movie_detail_general(self, question, video_name):
        response = self.bedrock_agent_runtime_client.retrieve_and_generate(
        input={"text": question},
        retrieveAndGenerateConfiguration={
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": knowledge_base_id,
                    "modelArn": scene_understanding_model_id,
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {
                            "numberOfResults": 5,
                            "overrideSearchType": "HYBRID"
                        }
                    },
                    "generationConfiguration" : {
                        'inferenceConfig': {
                            'textInferenceConfig': {
                                'maxTokens': 1024,
                                'temperature': 0.7,
                                'topP': .99
                            }
                        },
                        'promptTemplate': {
                            'textPromptTemplate': self.get_movie_detail_general_prompt_template
                        }
                    },
                    'retrievalConfiguration': {
                        'vectorSearchConfiguration': {
                            'filter': {
                                'equals': {
                                    "key" : "video_name",
                                    "value" : video_name.strip()
                                },
                            }
                        }
                    }
                },
                "type": "KNOWLEDGE_BASE"
            }
        )
        print(response["output"]["text"])
        summarized_response_message = response["output"]["text"]
        return summarized_response_message
    
    def get_movie_detail(self, question=None):
        latest_viewing_log = self.get_latest_watch_log()
        video_name = latest_viewing_log["video_name"]
        current_progress_millis = latest_viewing_log["timemillis"]
        response = self.process_video_progress(video_name=video_name, current_progress_millis=current_progress_millis)
        return response
    
    def process_scene_actor_detail(self):
        print("inside process_scene_actor_detail")
        latest_viewing_log = self.get_latest_watch_log()
        video_name = latest_viewing_log["video_name"]
        current_progress_millis = latest_viewing_log["timemillis"]
        response = self.current_chapter_segment_query(current_progress_millis, video_chapter_segment_index, video_name)
        if len(response) == 0:
            return "I am sorry, I am not able to find the actor information about this scene"
        else:
            all_celebrities = []
            for doc in response:
                celebrities = doc["_source"]["celebrities"]
                all_celebrities.extend(celebrities)
            return ", ".join(all_celebrities)
    
    def process_movie_scene_request(self, question):
        classification = self.movieScene_classfier(question)

        if classification.lower() == "movie_scene_detail".lower():
            response = self.get_movie_detail(question=question)
        elif classification.lower() == "movie_actor":
            response = self.process_scene_actor_detail()
        elif classification.lower() == "movie_overall_story":
            latest_viewing_log = self.get_latest_watch_log()
            video_name = latest_viewing_log["video_name"]
            response = self.get_movie_detail_general(video_name=video_name, question=question)
        else:
            response = "I am sorry but I ma not able to help with the question related to the scene at the moment."
        return { "result" : response}

    


