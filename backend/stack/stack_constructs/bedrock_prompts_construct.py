"""
Copyright 2024 Amazon.com, Inc. and its affiliates. All Rights Reserved.

Licensed under the Amazon Software License (the "License").
You may not use this file except in compliance with the License.
A copy of the License is located at

  http://aws.amazon.com/asl/

or in the "license" file accompanying this file. This file is distributed
on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
express or implied. See the License for the specific language governing
permissions and limitations under the License.
"""

import hashlib
from typing import Optional

import aws_cdk as cdk
from aws_cdk import aws_bedrock
from constructs import Construct


class BedrockPromptsConstruct(Construct):
    """Deploys the BedrockPrompts construct"""
    
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id)
        
        self.classification_prompt = self._create_classification_prompt("./stack/stack_constructs/prompts/classification_prompt.txt")
        self.rewrite_prompt = self._create_rewrite_prompt("./stack/stack_constructs/prompts/rewrite_prompt.txt")
        self.final_prompt = self._create_final_prompt("./stack/stack_constructs/prompts/final_prompt.txt")
        self.general_q_prompt = self._create_general_question_prompt("./stack/stack_constructs/prompts/general_q_prompt.txt")
        self.rerank_prompt = self._create_rerank_prompt("./stack/stack_constructs/prompts/rerank_prompt.txt")
        self.quote_finder_prompt = self._create_quote_finder_prompt("./stack/stack_constructs/prompts/quote_finder_prompt.txt")
        self.scene_finder_prompt = self._create_scene_finder_prompt("./stack/stack_constructs/prompts/scene_finder_prompt.txt")

    def _create_classification_prompt(self, path_to_prompt: str) -> aws_bedrock.CfnPromptVersion:
        prompt_name = f"{cdk.Stack.of(self).stack_name}-ClassificationPrompt"
        prompt_description = "A prompt to classify the intent of a query"
        
        with open(path_to_prompt, 'r', encoding='utf-8') as f:
            system_prompt = f.read().strip()
        
        prompt = aws_bedrock.CfnPrompt(
            self, prompt_name,
            name=prompt_name,
            default_variant="default",
            description=prompt_description,
            variants=[{
                "name": "default",
                "templateConfiguration": {
                    "chat": {
                        "inputVariables": [{"name": "query"}],
                        "messages": [{
                            "role": "user",
                            "content": [{"text": "{{query}}"}]
                        }],
                        "system": [
                            {"text": system_prompt},
                            {"cachePoint": {"type": "default"}}
                        ]
                    }
                },
                "modelId": f"us.amazon.nova-micro-v1:0",
                "templateType": "CHAT",
                "inferenceConfiguration": {
                    "text": {
                        "maxTokens": 512,
                        "temperature": 0.1
                    }
                }
            }]
        )
        
        return aws_bedrock.CfnPromptVersion(
            self, f"{prompt_name}Version",
            prompt_arn=prompt.attr_arn,
            description=hashlib.sha256(system_prompt.encode()).hexdigest()
        )

    def _create_rewrite_prompt(self, path_to_prompt: str) -> aws_bedrock.CfnPromptVersion:
        prompt_name = f"{cdk.Stack.of(self).stack_name}-RewritePrompt"
        prompt_description = "A prompt to rewrite the query of the user"
        
        with open(path_to_prompt, 'r', encoding='utf-8') as f:
            system_prompt = f.read().strip()
        
        prompt = aws_bedrock.CfnPrompt(
            self, prompt_name,
            name=prompt_name,
            default_variant="default",
            description=prompt_description,
            variants=[{
                "name": "default",
                "templateConfiguration": {
                    "chat": {
                        "inputVariables": [{"name": "query"}],
                        "messages": [{
                            "role": "user",
                            "content": [{"text": "{{query}}"}]
                        }],
                        "system": [
                            {"text": system_prompt},
                            {"cachePoint": {"type": "default"}}
                        ]
                    }
                },
                "modelId": f"us.amazon.nova-lite-v1:0",
                "templateType": "CHAT",
                "inferenceConfiguration": {
                    "text": {
                        "maxTokens": 512,
                        "temperature": 0.9,
                        "topP": 0.9
                    }
                }
            }]
        )
        
        return aws_bedrock.CfnPromptVersion(
            self, f"{prompt_name}Version",
            prompt_arn=prompt.attr_arn,
            description=hashlib.sha256(system_prompt.encode()).hexdigest()
        )

    def _create_final_prompt(self, path_to_prompt: str) -> aws_bedrock.CfnPromptVersion:
        prompt_name = f"{cdk.Stack.of(self).stack_name}-FinalPrompt"
        prompt_description = "A final prompt to stitch it all together"
        
        with open(path_to_prompt, 'r', encoding='utf-8') as f:
            system_prompt = f.read().strip()
        
        prompt = aws_bedrock.CfnPrompt(
            self, prompt_name,
            name=prompt_name,
            default_variant="default",
            description=prompt_description,
            variants=[{
                "name": "default",
                "templateConfiguration": {
                    "chat": {
                        "inputVariables": [{"name": "query"}],
                        "messages": [{
                            "role": "user",
                            "content": [{"text": "{{query}}"}]
                        }],
                        "system": [
                            {"text": system_prompt},
                            {"cachePoint": {"type": "default"}}
                        ]
                    }
                },
                "modelId": f"us.amazon.nova-pro-v1:0",
                "templateType": "CHAT",
                "inferenceConfiguration": {
                    "text": {
                        "maxTokens": 512,
                        "temperature": 0.7,
                        "topP": 0.9
                    }
                }
            }]
        )
        
        return aws_bedrock.CfnPromptVersion(
            self, f"{prompt_name}Version",
            prompt_arn=prompt.attr_arn,
            description=hashlib.sha256(system_prompt.encode()).hexdigest()
        )

    def _create_rerank_prompt(self, path_to_prompt: str) -> aws_bedrock.CfnPromptVersion:
        prompt_name = f"{cdk.Stack.of(self).stack_name}-RerankPrompt"
        prompt_description = "Rerank prompt to order results"
        
        with open(path_to_prompt, 'r', encoding='utf-8') as f:
            system_prompt = f.read().strip()
        
        prompt = aws_bedrock.CfnPrompt(
            self, "RerankPrompt",
            name=prompt_name,
            default_variant="default",
            description=prompt_description,
            variants=[{
                "name": "default",
                "templateConfiguration": {
                    "chat": {
                        "inputVariables": [{"name": "query"}],
                        "messages": [{
                            "role": "user",
                            "content": [{"text": "{{query}}"}]
                        }],
                        "system": [
                            {"text": system_prompt},
                            {"cachePoint": {"type": "default"}}
                        ]
                    }
                },
                "modelId": f"us.amazon.nova-micro-v1:0",
                "templateType": "CHAT",
                "inferenceConfiguration": {
                    "text": {
                        "maxTokens": 512,
                        "temperature": 0.0,
                        "topP": 0.0
                    }
                }
            }]
        )
        
        return aws_bedrock.CfnPromptVersion(
            self, f"{prompt_name}Version",
            prompt_arn=prompt.attr_arn,
            description=hashlib.sha256(system_prompt.encode()).hexdigest()
        )

    def _create_quote_finder_prompt(self, path_to_prompt: str) -> aws_bedrock.CfnPromptVersion:
        prompt_name = f"{cdk.Stack.of(self).stack_name}-QuoteFinderPrompt"
        prompt_description = "Quote to title finder; using inherent LLM knowledge"
        
        with open(path_to_prompt, 'r', encoding='utf-8') as f:
            system_prompt = f.read().strip()
        
        prompt = aws_bedrock.CfnPrompt(
            self, prompt_name,
            name=prompt_name,
            default_variant="default",
            description=prompt_description,
            variants=[{
                "name": "default",
                "templateConfiguration": {
                    "chat": {
                        "inputVariables": [{"name": "query"}],
                        "messages": [{
                            "role": "user",
                            "content": [{"text": "{{query}}"}]
                        }],
                        "system": [
                            {"text": system_prompt},
                            {"cachePoint": {"type": "default"}}
                        ]
                    }
                },
                "modelId": f"us.amazon.nova-pro-v1:0",
                "templateType": "CHAT",
                "inferenceConfiguration": {
                    "text": {
                        "maxTokens": 512,
                        "temperature": 0.0,
                        "topP": 0.0
                    }
                }
            }]
        )
        
        return aws_bedrock.CfnPromptVersion(
            self, f"{prompt_name}Version",
            prompt_arn=prompt.attr_arn,
            description=hashlib.sha256(system_prompt.encode()).hexdigest()
        )

    def _create_scene_finder_prompt(self, path_to_prompt: str) -> aws_bedrock.CfnPromptVersion:
        prompt_name = f"{cdk.Stack.of(self).stack_name}-SceneFinderPrompt"
        prompt_description = "Scene to title finder; using inherent LLM knowledge"
        
        with open(path_to_prompt, 'r', encoding='utf-8') as f:
            system_prompt = f.read().strip()
        
        prompt = aws_bedrock.CfnPrompt(
            self, prompt_name,
            name=prompt_name,
            default_variant="default",
            description=prompt_description,
            variants=[{
                "name": "default",
                "templateConfiguration": {
                    "chat": {
                        "inputVariables": [{"name": "query"}],
                        "messages": [{
                            "role": "user",
                            "content": [{"text": "{{query}}"}]
                        }],
                        "system": [
                            {"text": system_prompt},
                            {"cachePoint": {"type": "default"}}
                        ]
                    }
                },
                "modelId": f"us.amazon.nova-pro-v1:0",
                "templateType": "CHAT",
                "inferenceConfiguration": {
                    "text": {
                        "maxTokens": 512,
                        "temperature": 0.0,
                        "topP": 0.0
                    }
                }
            }]
        )
        
        return aws_bedrock.CfnPromptVersion(
            self, f"{prompt_name}Version",
            prompt_arn=prompt.attr_arn,
            description=hashlib.sha256(system_prompt.encode()).hexdigest()
        )

    def _create_general_question_prompt(self, path_to_prompt: str) -> aws_bedrock.CfnPromptVersion:
        prompt_name = f"{cdk.Stack.of(self).stack_name}-GeneralQPrompt"
        prompt_description = "A general q prompt to stitch it all together"
        
        with open(path_to_prompt, 'r', encoding='utf-8') as f:
            system_prompt = f.read().strip()
        
        prompt = aws_bedrock.CfnPrompt(
            self, prompt_name,
            name=prompt_name,
            default_variant="default",
            description=prompt_description,
            variants=[{
                "name": "default",
                "templateConfiguration": {
                    "chat": {
                        "inputVariables": [{"name": "query"}],
                        "messages": [{
                            "role": "user",
                            "content": [{"text": "{{query}}"}]
                        }],
                        "system": [
                            {"text": system_prompt},
                            {"cachePoint": {"type": "default"}}
                        ]
                    }
                },
                "modelId": f"us.amazon.nova-pro-v1:0",
                "templateType": "CHAT",
                "inferenceConfiguration": {
                    "text": {
                        "maxTokens": 512,
                        "temperature": 0.7,
                        "topP": 0.9
                    }
                }
            }]
        )
        
        return aws_bedrock.CfnPromptVersion(
            self, f"{prompt_name}Version",
            prompt_arn=prompt.attr_arn,
            description=hashlib.sha256(system_prompt.encode()).hexdigest()
        )