import hashlib
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator
import json
import os
from datetime import UTC, datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key
import argparse

class EnvVarNotDefinedError(Exception):
    """Exception raised when required environment variable is not defined"""

    def __init__(self, var_name):
        self.var_name = var_name
        self.message = f"Environment variable '{var_name}' is not defined"
        super().__init__(self.message)


class InvalidProfileError(Exception):
    """Exception raised when a profile is invalid"""

    def __init__(self, message):
        self.message = message
        super().__init__(self.message)


class Profile(BaseModel):
    id: Optional[str] = Field(default_factory=lambda: str(uuid4()), min_length=1)
    title: str = Field(..., min_length=1)
    aggregated_history: Optional[list]

    @model_validator(mode="before")
    def set_defaults(cls, values):
        if not values.get("id"):
            values["id"] = hashlib.md5(
                values["title"].encode(), usedforsecurity=False
            ).hexdigest()
        return values


dynamodb_resource = boto3.resource("dynamodb")

TABLE_NAME = os.getenv("CONVO_SEARCH_PERSONAS_DYNAMODB_TABLE", "user_conversation_history")
if TABLE_NAME is None:
    print("TABLE_NAME environment variable is not defined")
    raise EnvVarNotDefinedError("TABLE_NAME")

dynamodb_table = dynamodb_resource.Table(TABLE_NAME)

def main():
    # Your program logic here
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona-json-file", help="Persona JSON input file", type=str, required=True)
    args = parser.parse_args()

    persona_json_file = args.persona_json_file
    with open(persona_json_file, "r") as f:
        data = f.read()
    
    persona_dict = json.loads(data)
    created_at = int(datetime.now(UTC).timestamp() * 1000)
    title_ids = persona_dict["title_ids"] if "title_ids" in persona_dict else []
    title_themes = persona_dict["title_themes"] if "title_themes" in persona_dict else []
    
    profile_hash = hashlib.md5(persona_dict["title"].encode(), usedforsecurity=False).hexdigest()
    
    rail_obj = {
        "pk": f"PROFILE#{profile_hash}",
        "sk": f"META",
        "title": persona_dict["title"],
        "createdAt": created_at,
        "id": profile_hash,
        "titleIds": title_ids,
        "titleThemes": title_themes,
    }
    print(f"Creating profile: {rail_obj}")
    dynamodb_table.put_item(Item=rail_obj)
    print(f"profile: {persona_dict["title"]} created successfully")
    return rail_obj

if __name__ == "__main__":
    main()