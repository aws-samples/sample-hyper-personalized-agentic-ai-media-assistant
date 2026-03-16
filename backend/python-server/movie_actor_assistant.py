from strands import Agent
import re
import os

actor_detail_model_id = os.environ.get("MOVIE_ACTOR_ASSISTANT_MODEL_ID", "us.amazon.nova-micro-v1:0")

class MovieActorAssistant:
    def __init__(self, movie_scene_assistant, additionalContext, authenticated_user):
        self.actor_classifier_agent = Agent(model=actor_detail_model_id)
        self.actor_detail_agent = Agent(model=actor_detail_model_id)
        self.movie_scene_assistant = movie_scene_assistant
        self.additionalContext = additionalContext # Instantiate additional context allowing other search functionality
        self.authenticated_user = authenticated_user
        
    def process_actor_information(self, question):
        additionContext_str = "\n".join(self.additionalContext)

        actor_classifier_prompt = f"""You are given a question about actor, categorize the user question as followed: 

Categories: 
actor_scene - if the question is actor in a scene of a movie.
actor_movie - if the question is about a specific title, not a scene.

Here's the context of the conversation:

{additionContext_str}

User question: {question}


You answer must be one of the categories above and not make anything else up. Put your answer in the following XML format <category/>. 
For example:

<category>actor_movie</category>
or 
<category>actor_scene</category>

Do not provide any other detail. 
"""
        
        actor_title_prompt = f"""
You are given a question about actor and the conversation history between a user and the movie assistant. Your job is to determine the actor information based on the user question. 

Here's the conversation history:

{additionContext_str}

User question: {question}


You answer must only be derived from the conversation history, do not make anything up. Answer the question directly in plain English. Do not provide any other detail. 

"""

        response = self.actor_classifier_agent(actor_classifier_prompt)
        response_message = response.message["content"][0]["text"]
        pattern = r"<category>(.*?)</category>"
        matches = re.findall(pattern, response_message, re.DOTALL)
        category = matches[0]

        if category.lower() == "actor_movie":
            response = self.actor_detail_agent(actor_title_prompt)
            response_message = response.message["content"][0]["text"]
        else:
            response_message = self.movie_scene_assistant.process_scene_actor_detail()

        return { "result" : response_message}