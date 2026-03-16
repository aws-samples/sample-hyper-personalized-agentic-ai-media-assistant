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

from typing import Optional

from pydantic import BaseModel, Field


class MessageInput(BaseModel):
    conversation_id: str = Field(..., alias="conversationId")
    content: str
    is_new_chat: bool = Field(..., alias="isNewChat")
    content_type: str = Field(..., alias="contentType", pattern="^(TAPPABLE|WRITTEN)$")
    profile: Optional[str] = None
    user: str


class SQSPayload(MessageInput):
    user: str


class Asset(BaseModel):
    id: str


class Rail(BaseModel):
    assets: list[Asset]


class Title(BaseModel):
    title: str
    id: str
    imdb_rating: float = 0
    premiere_date: str = None
    synopsis: Optional[str] = None
    cast: Optional[list[str]] = None
    crew: Optional[list[str]] = None
    entity_type: Optional[str] = None
    is_kids_content: bool = False
    genres: Optional[list[str]] = None
    keywords: Optional[list[str]] = None
    image: Optional[str] = None
    awards: Optional[list[str]] = None
    

class MessageOutput(BaseModel):
    conversation_id: str
    content: Optional[str] = None
    raw_content: Optional[str] = None
    suggested_content: Optional[list[Title]] = None
    tappable_options: Optional[list[str]] = None
    time_to_first_token: Optional[float] = None
    llm_tappable_options: Optional[list[str]] = None

class MessageChunk(BaseModel):
    conversation_id: str = Field(..., serialization_alias="conversationId")
    content: str
    is_final: bool = Field(False, serialization_alias="isFinal")
    order: int


class WatchHistory(BaseModel):
    title_ids: Optional[list[str]] = []
    title_themes: Optional[list[str]] = []
    title_names: Optional[list[str]] = []


class QueryClassification(BaseModel):
    intent: str
    animation_bias: bool = False
    is_award_winning: bool = False
    is_time_period: bool = False
    kid_content: bool = False


class QueryRewrite(BaseModel):
    query: str
    media_type: str
    user_profile: str = None
    did_request_media_type: str = "No"
