"""Movie scene analysis agent.

Provides scene understanding, story summarization, and general movie Q&A
based on video chapter data in OpenSearch and a Bedrock Knowledge Base.
"""

import os
import re
import time

import boto3
from strands import Agent
from strands.tools import tool

from aoss_helper import OpenSearchHelper

# Config from environment
SCENE_MODEL_ID = os.environ.get("MOVIE_SCENE_UNDERSTANDING_MODEL_ID", "global.amazon.nova-2-lite-v1:0")
# Cohere Rerank 3.5 in the runtime's own region unless configured otherwise.
RERANK_MODEL_ARN = os.environ.get(
    "MOVIE_SCENE_UNDERSTANDING_RERANKING_MODEL_ARN",
    f"arn:aws:bedrock:{os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-west-2'}"
    "::foundation-model/cohere.rerank-v3-5:0",
)
RERANK_REGION = RERANK_MODEL_ARN.split(":")[3]
ANALYSIS_OSS_HOST = os.environ.get("ANALYSIS_OSS_HOST", "")
KNOWLEDGE_BASE_ID = os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID", "")
VIDEO_CHAPTER_INDEX = os.environ.get("BDA_VIDEO_CHAPTER_INDEX_NAME", "chapters")
VIDEO_CHAPTER_SEGMENT_INDEX = os.environ.get("BDA_VIDEO_CHAPTER_SEGMENT_INDEX_NAME", "segments")
VIDEO_CHAPTER_AUDIO_SEGMENT_INDEX = os.environ.get("BDA_VIDEO_CHAPTER_AUDIO_SEGMENT_INDEX_NAME", "audio-segments")
SCENE_IMAGE_S3_BUCKET = os.environ.get("SCENE_IMAGE_S3_BUCKET", "")

if not os.environ.get("AWS_DEFAULT_REGION"):
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

STORY_SYSTEM_PROMPT = """You are a professional story teller.
You job is to curate a compelling story based on the given movie scenes in <scene> and <dialog> XML tags.
The scene contains scene level description, and dialog contains the conversation in the scene.

Here are the guidelines:

- You are to first summarize your story based on the given scenes, chapters and dialog information, then return the summarized story in story telling style.
- Be as creative in the story telling as possible. If the characters or the celebrities are available in the scene, make sure to use the character name directly.
- When describing the story, focus on the characters more so than the surrounding.
- Do not mention anything about chapter, or scene explicitly in your summary.
- Your summary should not be more than a few sentences long. Balance between story telling and being concise.

The scenes given in the context are organized in the right sequence, please follow the exact order of the scenes when providing the summary.

Put your thoughts in the <thinking> XML tag. Then provide your story in the <story> XML tag."""

GENERAL_PROMPT_TEMPLATE = """You are a movie assistant. You task is to provide an answer to the user question about a movie given the context of scenes or plots for the movie.
Your answer must be derived from the given context only. Do not make up any answers on your own.
Summarize the answer that aligns with the user question. Make it sound interesting that captures the attention of the user.
Only provide the summary, nothing else, do not provide any other explainations.

Here's the user's question: $query$

Here's the given context about the scene or plot in a movie the user is interested:

$search_results$
"""

# --- Strands tool functions for the actor agent ---

_rekognition_client = None


def _get_rekognition_client():
    global _rekognition_client
    if _rekognition_client is None:
        _rekognition_client = boto3.client("rekognition", region_name=os.environ.get("SCENE_IMAGE_REGION", os.environ["AWS_DEFAULT_REGION"]))
    return _rekognition_client


@tool
def get_title_cast(title_id: str) -> str:
    """Look up the cast (principalCastMembers) for a movie title from the catalog.

    Args:
        title_id: The catalog title ID (e.g. "tt1001001").

    Returns:
        A string listing the cast members with their roles, or "No cast found".
    """
    from config import OPENSEARCH_INDEX
    from retrieval import get_client as get_opensearch_client
    try:
        client = get_opensearch_client()
        resp = client.search(index=OPENSEARCH_INDEX, body={
            "_source": ["principalCastMembers"],
            "query": {"term": {"title_id": title_id}},
            "size": 1,
        })
        hits = resp["hits"]["hits"]
        if not hits:
            return "No cast found for this title."
        cast = hits[0]["_source"].get("principalCastMembers", [])
        if isinstance(cast, list):
            return "Cast members:\n" + "\n".join(f"- {m}" for m in cast)
        return f"Cast members: {cast}"
    except Exception as e:
        return f"Error looking up cast: {e}"


@tool
def detect_celebrities_from_image(s3_key: str) -> str:
    """Detect celebrities in a scene image using Amazon Rekognition.

    Args:
        s3_key: The S3 object key for the scene screenshot image.

    Returns:
        A string listing detected celebrities with confidence scores and gender.
    """
    try:
        client = _get_rekognition_client()
        resp = client.recognize_celebrities(
            Image={"S3Object": {"Bucket": SCENE_IMAGE_S3_BUCKET, "Name": s3_key}},
        )
        celebrities = resp.get("CelebrityFaces", [])
        if not celebrities:
            return "No celebrities detected in this image."
        lines = []
        for celeb in celebrities:
            name = celeb.get("Name", "Unknown")
            confidence = round(celeb.get("MatchConfidence", 0), 1)
            gender = celeb.get("KnownGender", {}).get("Type", "unknown")
            bbox = celeb.get("Face", {}).get("BoundingBox", {})
            if confidence > 99: #only include celebrity with 99% certainties.
                lines.append(f"- {name} (confidence: {confidence}%, gender: {gender}, bbox: {bbox})")
        return f"Detected {len(celebrities)} celebrities:\n" + "\n".join(lines)
    except Exception as e:
        return f"Celebrity detection failed: {e}"


@tool
def get_scene_celebrities_from_index(video_name: str, timestamp_millis: int) -> str:
    """Get pre-calculated celebrity information for a scene from the video chapter segment index.

    This is a fallback when image-based detection is not available.

    Args:
        video_name: The video file name.
        timestamp_millis: The playback position in milliseconds.

    Returns:
        A string listing celebrities found in the scene segments.
    """
    try:
        oss = OpenSearchHelper(host=ANALYSIS_OSS_HOST.removeprefix("https://"))
        start_search = timestamp_millis - 5000
        body = {
            "size": 10,
            "query": {"function_score": {"query": {"bool": {"filter": [
                {"range": {"end_timestamp_millis": {"gte": start_search}}},
                {"range": {"start_timestamp_millis": {"lte": timestamp_millis}}},
                {"term": {"video_name": video_name}},
            ]}}}},
            "_source": ["celebrities"],
        }
        hits = oss.search(body, VIDEO_CHAPTER_SEGMENT_INDEX)["hits"]["hits"]
        all_celebs = []
        for hit in hits:
            all_celebs.extend(hit["_source"].get("celebrities", []))
        seen = set()
        unique = [c for c in all_celebs if c not in seen and not seen.add(c)]
        if not unique:
            return "No celebrity information found for this scene in the index."
        return "Celebrities from scene index:\n" + "\n".join(f"- {c}" for c in unique)
    except Exception as e:
        return f"Error querying scene index: {e}"


ACTOR_AGENT_SYSTEM_PROMPT = """You are a movie scene actor identification assistant. Your job is to identify the actors and characters in a movie scene.

You have access to three tools:
1. get_title_cast - Look up the full cast list for a movie by title_id. Cast members are formatted as "Name (as Character)".
2. detect_celebrities_from_image - Use Amazon Rekognition to detect celebrities in a scene screenshot.
3. get_scene_celebrities_from_index - Get pre-calculated celebrity data from the video analysis index (fallback).

## Strategy
First, you must analyze the question and context carefully about what the user is asking. The question could be directly referencing the characters in the scene, or referring to the real actor names.
If it's about the characters in the scene, you should first look into the given context and derive any supporting information from it. 

1. If you need to find out the actors from the image, you should use detect_celebrities_from_image tool. This should be your first and default attempt if the answer is not already in the context. If you it did not detect any celebrity, then try other approaches which is described in the numbered list below.
2. If you need to find out the full cast of the show, you should use get_title_cast tool with the given title_id to get the information. Do not guess the casts from the title_id, you must use the tool to get the list of casts.
3. If you need to relate the character in the movie with the actor, you can cross-reference the detected celebrities with the cast list to identify both the actor name AND their character role.
4. If image detection is not available or returns no results, use get_scene_celebrities_from_index as a fallback.

You do not have to always use the strategy above. The answer to user's question could already be found in the given scene or dialog. If that's the case, you can simply use those knowledge to answer the question directly.
You also do not need to justify whether the actor / characters appears in the scene or not. If you find the information you should response with what you know.

## Response Format
Think step by step, put your analysis in <thinking> XML tag.
Your final answer must be very concise to the user question. Do not give information that's not relevant to the question.
Put your final answer in <answer> XML tag.

## Critical Rules
- NEVER expose internal identifiers like title_id (e.g. "tt1001001"), index names, S3 keys, or any technical details to the user.
- NEVER preface your answer with phrases like "Based on the available cast information" or "According to the data". Just answer directly.
- NEVER say you cannot identify something if you already have information from the scene context or conversation history. Be confident — if the scene description mentions a character, you know who they are.
- NEVER add disclaimers, caveats, or qualifications like "the actor is not in the cast list", "suggesting he may be a secondary character", "without more information", or "I cannot definitively identify". If you know something about the character from the scene, just state it.
- If a character is mentioned in the scene context but not found in the cast list, still answer confidently using what you know from the scene. The scene context is authoritative.
- Only answer what the user asked. Do not volunteer extra analysis about cast lists, visibility, or presence.
"""


class AnalysisSession:
    """Lightweight session memory for the analysis agent."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._turns: list[dict] = []

    def add_turn(self, query: str, response: str) -> None:
        self._turns.append({"query": query, "response": response})

    def get_history_text(self) -> str:
        if not self._turns:
            return ""
        lines = []
        for i, t in enumerate(self._turns, 1):
            lines.append(f"Turn {i}: User: \"{t['query']}\" → Assistant: {t['response']}")
        return "\n".join(lines)

    def get_recent_history_text(self, n: int = 2) -> str:
        """Return only the last n turns for lightweight context."""
        if not self._turns:
            return ""
        recent = self._turns[-n:]
        lines = []
        for t in recent:
            lines.append(f"User: \"{t['query']}\" → Assistant: {t['response'][:200]}...")
        return "\n".join(lines)


class MovieSceneAssistant:
    """Handles movie scene analysis: classification, scene detail, and overall story."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        from strands.models import BedrockModel
        scene_model = BedrockModel(model_id=SCENE_MODEL_ID, region_name=os.environ["AWS_DEFAULT_REGION"], max_tokens=16384)
        story_model = BedrockModel(model_id=SCENE_MODEL_ID, region_name=os.environ["AWS_DEFAULT_REGION"], max_tokens=16384)
        # actor_model = BedrockModel(model_id="global.anthropic.claude-sonnet-4-6", region_name="us-east-1", max_tokens=4096)
        # actor_model = BedrockModel(model_id=SCENE_MODEL_ID, region_name="us-east-1", max_tokens=4096)
        actor_model_id = os.environ.get("V2_MODEL_ID", "global.anthropic.claude-haiku-4-5-20251001-v1:0")
        # Haiku 5.x thinks by default; keep it off like the pipeline stages (llm.py).
        actor_extra = {"additional_request_fields": {"thinking": {"type": "disabled"}}} if "claude-haiku-5" in actor_model_id else {}
        actor_model = BedrockModel(model_id=actor_model_id, region_name=os.environ["AWS_DEFAULT_REGION"], max_tokens=4096, **actor_extra)
        self.intent_agent = Agent(model=scene_model)
        self.story_agent = Agent(model=story_model, system_prompt=STORY_SYSTEM_PROMPT)
        self.actor_agent = Agent(
            model=actor_model,
            tools=[get_title_cast, detect_celebrities_from_image, get_scene_celebrities_from_index],
            system_prompt=ACTOR_AGENT_SYSTEM_PROMPT,
        )
        self.rerank_client = boto3.client("bedrock-agent-runtime", region_name=RERANK_REGION)
        self.kb_client = boto3.client("bedrock-agent-runtime", region_name=os.environ["AWS_DEFAULT_REGION"])
        self.oss = OpenSearchHelper(host=ANALYSIS_OSS_HOST.removeprefix("https://"))

    # --- Classification ---

    def classify_intent(self, question: str, history: str = "") -> str:
        context_block = ""
        if history:
            context_block = f"## Conversation History\n{history}\n\n"
        resp = self.intent_agent(
            f"""{context_block}given the {question}, classify the intent. Return one of:

## Classfication
- movie_scene_detail - question about what's happening in the current scene
- movie_scene_actors - question about who the actors, actresses, or characters are in the scene, or follow-up questions about characters already discussed
- movie_overall_story - question beyond the current scene, requires cross-scene context
- movie_recommendation - user wants to find, discover, or get recommendations for OTHER movies (e.g. "find other movies with this actor", "what else has she been in?", "recommend something similar", "show me movies like this")

IMPORTANT: If the conversation history shows the user has been asking about actors/characters and the current question is a follow-up about another character (e.g. "how about Gloria?", "and the other one?", "what about him?"), classify as movie_scene_actors — NOT movie_recommendation.
Only classify as movie_recommendation if the user explicitly wants to FIND or DISCOVER different movies/shows.

For example:
question: What's going on in this scene? → movie_scene_detail
question: Who are the actors in this scene? → movie_scene_actors
question: Who is that character? → movie_scene_actors
question: What other celebrities are in this movie? → movie_scene_actors
question: How about Gloria? (after asking about actors) → movie_scene_actors
question: And the other guy? (after asking about actors) → movie_scene_actors
question: Why is she angry at him? → movie_overall_story
question: What other movies has this actor been in? → movie_recommendation
question: Find me similar movies → movie_recommendation
question: Can you recommend movies like this one? → movie_recommendation

Put your result in <classification> XML tag. The values must be the classification text. No other text is allowed."""
        )
        text = resp.message["content"][0]["text"]
        match = re.findall(r"<classification>(.*?)</classification>", text, re.DOTALL)
        if match:
            return match[0].strip().lower()
        text_lower = text.lower()
        if "movie_recommendation" in text_lower:
            return "movie_recommendation"
        if "movie_scene_actors" in text_lower:
            return "movie_scene_actors"
        if "movie_overall_story" in text_lower:
            return "movie_overall_story"
        return "movie_scene_detail"

    # --- OpenSearch queries ---

    def _current_chapter(self, millis: int, video_name: str) -> list:
        body = {
            "size": 10,
            "query": {"function_score": {"query": {"bool": {"filter": [
                {"range": {"start_timestamp_millis": {"lte": millis}}},
                {"range": {"end_timestamp_millis": {"gt": millis}}},
                {"term": {"video_name": video_name}},
            ]}}}},
            "_source": ["chapter", "video_name", "summary", "start_timestamp_millis", "end_timestamp_millis"],
        }
        return self.oss.search(body, VIDEO_CHAPTER_INDEX)["hits"]["hits"]

    def _previous_chapter(self, video_name: str, chapter: int) -> list:
        body = {
            "size": 10,
            "query": {"function_score": {"query": {"bool": {"filter": [
                {"term": {"video_name": video_name}},
                {"term": {"chapter": chapter}},
            ]}}}},
            "_source": ["chapter", "video_name", "summary", "start_timestamp_millis", "end_timestamp_millis"],
        }
        return self.oss.search(body, VIDEO_CHAPTER_INDEX)["hits"]["hits"]

    def _audio_segments(self, video_name: str, chapter: int, time_millis: int) -> list:
        body = {
            "size": 100,
            "query": {"function_score": {"query": {"bool": {"filter": [
                {"term": {"video_name": video_name}},
                {"term": {"chapter": chapter}},
                {"range": {"end_timestamp_millis": {"lt": time_millis}}},
            ]}}}},
            "_source": ["chapter", "chapter_segment", "video_name", "text", "start_timestamp_millis", "end_timestamp_millis"],
        }
        return self.oss.search(body, VIDEO_CHAPTER_AUDIO_SEGMENT_INDEX)["hits"]["hits"]

    # --- Knowledge Base & Reranking ---

    def _rerank(self, query: str, docs: list[str]) -> list[str]:
        sources = [{"inlineDocumentSource": {"textDocument": {"text": d}, "type": "TEXT"}, "type": "INLINE"} for d in docs]
        resp = self.rerank_client.rerank(
            queries=[{"textQuery": {"text": query}, "type": "TEXT"}],
            rerankingConfiguration={
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": RERANK_MODEL_ARN},
                    # Rerank refuses a count above the number of sources.
                    "numberOfResults": min(3, len(docs)),
                },
                "type": "BEDROCK_RERANKING_MODEL",
            },
            sources=sources,
        )
        return [docs[r["index"]] for r in resp["results"]]

    def _retrieve_kb(self, query: str, video_name: str) -> list[str] | None:
        resp = self.kb_client.retrieve(
            knowledgeBaseId=KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {
                "filter": {"equals": {"key": "video_name", "value": video_name}},
                "numberOfResults": 5,
                "overrideSearchType": "HYBRID",
            }},
        )
        results = [x["content"]["text"] for x in resp["retrievalResults"]]
        if not results:
            return None
        return self._rerank(query, results) if len(results) > 1 else results

    # --- Chapter summary building ---

    def _build_chapter_summary(self, chapter_hit: dict, video_name: str) -> list[str]:
        summaries = []
        total_duration = 0
        hit = chapter_hit
        while True:
            src = hit["_source"]
            duration = src["end_timestamp_millis"] - src["start_timestamp_millis"]
            total_duration += duration
            summaries.insert(0, src["summary"])
            if total_duration < 10_000 and src["chapter"] > 1:
                prev = self._previous_chapter(video_name, src["chapter"] - 1)
                if prev:
                    hit = prev[0]
                else:
                    break
            else:
                break
        return summaries

    # --- Main processing ---

    def _process_scene_detail(self, millis: int, video_name: str, history: str = "") -> str:
        """Get scene detail for the current timestamp."""
        chapters = self._current_chapter(millis, video_name)
        if not chapters:
            return "I couldn't find scene information for this timestamp."
        chapter_hit = chapters[0]
        src = chapter_hit["_source"]
        current_chapter = src["chapter"]
        progress = millis - src["start_timestamp_millis"]
        duration = src["end_timestamp_millis"] - src["start_timestamp_millis"]
        progress_pct = progress / duration if duration > 0 else 1.0

        dialog = ""
        if progress_pct < 0.3 and current_chapter > 1:
            prev = self._previous_chapter(video_name, current_chapter - 1)
            chapter_summary = self._build_chapter_summary(prev[0], video_name) if prev else self._build_chapter_summary(chapter_hit, video_name)
            audio = self._audio_segments(video_name, current_chapter, millis)
            if audio:
                sorted_audio = sorted(audio, key=lambda x: x["_source"]["chapter_segment"])
                dialog = " ".join(x["_source"]["text"] for x in sorted_audio)
        else:
            chapter_summary = self._build_chapter_summary(chapter_hit, video_name)

        combined = "\n".join(chapter_summary)
        kb_results = self._retrieve_kb(combined, video_name)
        script = kb_results[0] if kb_results else combined

        prompt = f"<scene>\n{script}\n</scene>\n\n<dialog>\n{dialog}\n</dialog>"
        if history:
            prompt = f"## Previous Conversation\n{history}\n\n{prompt}"
        resp = self.story_agent(prompt)
        text = resp.message["content"][0]["text"]
        match = re.findall(r"<story.*?>(.*?)</story>", text, re.DOTALL)
        return match[0].strip() if match else text

    def _process_scene_actors(self, question: str, video_name: str, timestamp_millis: int,
                              context_question: str = "", title_id: str = "",
                              scene_image_s3_key: str = "") -> str:
        """Identify actors/characters using the actor agent with tools.

        The actor agent has access to:
        - get_title_cast: look up cast by title_id
        - detect_celebrities_from_image: Rekognition celebrity detection
        - get_scene_celebrities_from_index: fallback pre-calculated data
        """
        # Build the prompt with all available context
        prompt_parts = [f"## User Question\n{question}"]
        if context_question:
            prompt_parts.insert(0, context_question)
        prompt_parts.append(f"\n## Available Data")
        if title_id:
            prompt_parts.append(f"- title_id: {title_id}")
        if scene_image_s3_key:
            prompt_parts.append(f"- scene_image_s3_key: {scene_image_s3_key}")
        prompt_parts.append(f"- video_name: {video_name}")
        prompt_parts.append(f"- timestamp_millis: {timestamp_millis}")

        actor_input = "\n".join(prompt_parts)
        print(f"[actor-agent] Input: {actor_input[:200]}...")

        resp = self.actor_agent(actor_input)
        text = resp.message["content"][0]["text"]
        print(f"[actor-agent] Full response:\n{text}")

        # Parse <answer> tag — return only the answer, not the thinking process
        answer_match = re.findall(r"<answer>(.*?)</answer>", text, re.DOTALL)
        if answer_match:
            return answer_match[0].strip()
        return text

    def _process_overall_story(self, question: str, video_name: str, history: str = "") -> str:
        """Answer a general question using the Knowledge Base."""
        search_query = question
        if history:
            rewrite_resp = self.intent_agent(
                f"""Rewrite the user's question into a single, self-contained search query
that can be used for semantic search against a movie knowledge base.
Resolve any pronouns or references using the conversation history.

## Conversation History
{history}

## User Question
{question}

Respond with ONLY the rewritten query inside <query> tags. Nothing else.
<query>rewritten query here</query>"""
            )
            rewrite_text = rewrite_resp.message["content"][0]["text"]
            match = re.findall(r"<query>(.*?)</query>", rewrite_text, re.DOTALL)
            if match:
                search_query = match[0].strip()
                print(f"[analysis] Rewritten query: {search_query}")

        resp = self.kb_client.retrieve_and_generate(
            input={"text": search_query},
            retrieveAndGenerateConfiguration={
                "knowledgeBaseConfiguration": {
                    "knowledgeBaseId": KNOWLEDGE_BASE_ID,
                    "modelArn": SCENE_MODEL_ID,
                    "retrievalConfiguration": {"vectorSearchConfiguration": {
                        "filter": {"equals": {"key": "video_name", "value": video_name.strip()}},
                    }},
                    "generationConfiguration": {
                        "inferenceConfig": {"textInferenceConfig": {"maxTokens": 1024, "temperature": 0.7, "topP": 0.99}},
                        "promptTemplate": {"textPromptTemplate": GENERAL_PROMPT_TEMPLATE},
                    },
                },
                "type": "KNOWLEDGE_BASE",
            },
        )
        return resp["output"]["text"]

    # --- Public entry point ---

    def _rewrite_for_recommendation(self, question: str, history: str) -> str:
        """Rewrite a question into a self-contained recommendation query."""
        resp = self.intent_agent(
            f"""The user is watching a movie and has been asking about scenes. Now they want
movie recommendations. Rewrite their question into a clear, self-contained
recommendation query that resolves all pronouns and references.

## Conversation History
{history}

## User Question
{question}

Respond with ONLY the rewritten query inside <query> tags.
<query>rewritten recommendation query</query>"""
        )
        text = resp.message["content"][0]["text"]
        match = re.findall(r"<query>(.*?)</query>", text, re.DOTALL)
        if match:
            rewritten = match[0].strip()
            print(f"[analysis→rec] Rewritten query: {rewritten}")
            return rewritten
        return question

    def process_request(self, question: str, timestamp_millis: int, video_name: str,
                        session: AnalysisSession = None, title_id: str = "", scene_image_s3_key: str = "") -> dict:
        """Process a movie scene analysis request."""
        history = session.get_history_text() if session else ""
        recent_history = session.get_recent_history_text(2) if session else ""
        context_question = question
        if history:
            context_question = f"## Conversation History\n{history}\n\n## Current Question\n{question}"

        intent = self.classify_intent(question, history=recent_history)
        print(f"[analysis] intent={intent} video={video_name} ts={timestamp_millis}")

        if intent == "movie_recommendation":
            rewritten = self._rewrite_for_recommendation(question, history) if history else question
            if session:
                session.add_turn(question, f"[Searching for recommendations: {rewritten}]")
            return {
                "result": None,
                "crossover": "recommendation",
                "recommendation_query": rewritten,
            }
        # Scene answers need the deployer's own analysis data (chapter/segment
        # indexes and the scripts knowledge base); none ships with the demo, so a
        # missing index or empty knowledge base degrades to a polite answer.
        try:
            if intent == "movie_scene_detail":
                result = self._process_scene_detail(timestamp_millis, video_name, history=history)
            elif intent == "movie_scene_actors":
                result = self._process_scene_actors(
                    question, video_name, timestamp_millis,
                    context_question=context_question,
                    title_id=title_id, scene_image_s3_key=scene_image_s3_key,
                )
            elif intent == "movie_overall_story":
                result = self._process_overall_story(question, video_name, history=history)
            else:
                result = "I'm not able to help with that question about the scene at the moment."
        except Exception as e:  # noqa: BLE001 - any missing-data failure gets the same answer
            print(f"[analysis] {intent} unavailable for {video_name!r}: {type(e).__name__}: {e}")
            result = "Scene details aren't available for this title yet."

        if session:
            session.add_turn(question, result)

        return {"result": result}


def run_analysis_stream(user_query: str, user_id: str, timestamp_millis: int, video_name: str,
                        session: AnalysisSession = None, title_id: str = "", scene_image_s3_key: str = ""):
    """Generator that runs movie scene analysis, yielding status/response events."""
    yield {"type": "status", "message": "Analyzing movie scene...", "timestamp": time.time()}

    assistant = MovieSceneAssistant(user_id)

    yield {"type": "status", "message": "Processing your question...", "timestamp": time.time()}

    result = assistant.process_request(
        user_query, timestamp_millis, video_name,
        session=session, title_id=title_id, scene_image_s3_key=scene_image_s3_key,
    )

    if result.get("crossover") == "recommendation":
        rewritten_query = result["recommendation_query"]
        yield {"type": "status", "message": "Searching for recommendations...", "timestamp": time.time()}
        yield {
            "type": "crossover",
            "target": "recommendation",
            "query": rewritten_query,
        }
    else:
        yield {
            "type": "response",
            "response": result.get("result", ""),
            "request_type": "analysis",
        }
