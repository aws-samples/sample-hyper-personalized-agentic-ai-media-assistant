#!/usr/bin/env python3
"""Add a custom User Preference long-term-memory strategy to the AgentCore memory.

This attaches a *built-in-with-overrides* User Preference strategy to the existing
memory resource. It overrides both the extraction and consolidation prompts so the
service captures the entertainment/sports taste signals this demo cares about
(genres, themes, favorite sports/teams/players, liked/disliked titles), keyed by
actor (`/preferences/{actorId}`) so it aggregates across sessions and personas.

The write path is handled by the service asynchronously: the agent already writes
raw conversation events (create_event), and AgentCore runs extraction +
consolidation in the background into the namespace. The agent then retrieves the
consolidated preferences at request time.

Notes:
- addMemoryStrategies is additive — it does NOT touch the existing SessionSummarizer.
- appendToPrompt REPLACES the default instruction block; the output schema is fixed
  by the built-in User Preference strategy and is NOT editable here. Do not
  reference the schema or rename consolidation operations in these prompts.
- Adding is idempotent and never changes an existing strategy; use --update to push
  edited prompts to the live strategy.

Usage:
  AGENTCORE_MEMORY_ID=... MEMORY_EXECUTION_ROLE_ARN=... python setup_ltm_strategy.py            # add (idempotent)
  AGENTCORE_MEMORY_ID=... MEMORY_EXECUTION_ROLE_ARN=... python setup_ltm_strategy.py --check    # show strategies
  AGENTCORE_MEMORY_ID=... MEMORY_EXECUTION_ROLE_ARN=... python setup_ltm_strategy.py --update --dry-run  # show payload
  AGENTCORE_MEMORY_ID=... MEMORY_EXECUTION_ROLE_ARN=... python setup_ltm_strategy.py --update   # push prompts
"""

import argparse
import json
import os
import time

import boto3

# The CDK deployment creates the memory with this strategy already attached
# (stacks.py reads the prompts below); this script is only for a memory created
# by hand. Set AGENTCORE_MEMORY_ID and MEMORY_EXECUTION_ROLE_ARN for it.
MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
EXECUTION_ROLE_ARN = os.environ.get("MEMORY_EXECUTION_ROLE_ARN", "")
STRATEGY_NAME = "TastePreferences"
PREFERENCE_NAMESPACE = "/preferences/{actorId}"
# Fast, cheap, already enabled in-account (the shard-warmer uses it).
EXTRACTION_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
CONSOLIDATION_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


# --- Extraction: what to pull out of a conversation ------------------------
# Identifies durable preferences from the USER's messages. Self-contained so it
# works whether the service appends or replaces the default instruction block.
EXTRACTION_PROMPT = """\
You are extracting DURABLE entertainment and sports preferences for a
hyper-personalized movie, TV, and live-sports recommendation assistant. Work only
from the USER's messages; use assistant messages solely as supporting context.

Capture a preference only when the user explicitly states it or it is clearly
implied. For each one, record the specific subject and its polarity (a like or a
dislike). Focus on these facets:
- Genres liked or disliked (e.g. action, sci-fi, romance, horror, comedy, thriller,
  documentary, animation).
- Themes, tones, or moods liked or disliked (e.g. mind-bending plots, feel-good,
  dark and gritty, slow-burn, based on a true story, strong female lead, minimal
  violence, family-friendly).
- Favorite sports and leagues (e.g. soccer/football, Formula 1, NBA, tennis).
- Favorite sports teams (e.g. Real Madrid, Ferrari, Lakers).
- Favorite athletes/players, and favorite actors or directors the user follows.
- Specific titles the user reacted positively or negatively to, and hard
  constraints (e.g. "no horror", "kids-friendly only", "nothing too violent").

Rules:
- Prefer durable, reusable taste over one-off situational requests. A momentary
  "tonight I feel like X" is NOT a preference unless it signals a lasting taste.
- Always attach polarity: distinguish likes from dislikes.
- Do not infer sensitive personal attributes. Do not use outside knowledge.
- Avoid duplicates. If there is no durable preference in the conversation, extract
  nothing.

Record shape (keeps stored memories small and retrievable):
- Emit ONE preference per subject. Never combine several genres, themes, people,
  titles, or constraints into one item; emit separate items instead.
- Keep each "preference" under 20 words: the subject plus its polarity
  (e.g. "Likes mind-bending sci-fi", "Dislikes superhero films").
- Keep each "context" to one short sentence of supporting evidence.
"""

# --- Consolidation: how to merge new signals into the stored profile -------
# appendToPrompt REPLACES the default consolidation instructions (only the output
# schema is re-appended by the service), so this keeps the default ROLE/TASK/
# OPERATIONS structure and operation names, and adds single-subject record rules.
# The previous version asked for a whole faceted profile per record, which made
# every UpdateMemory rebuild a growing catch-all list (5-6 KB records).
CONSOLIDATION_PROMPT = """\
# ROLE
You are a Memory Manager that evaluates new memories against existing stored
memories to determine the appropriate operation, for a movie, TV, and live-sports
recommender.

# TASK
For each new memory, select exactly ONE operation: AddMemory, UpdateMemory, or
SkipMemory.

# RECORD SHAPE (applies to every memory you write)
- One subject per memory: a single genre, theme/mood, sport, team, player,
  actor/director, title, or hard constraint, with its polarity (like or dislike).
- "preference" is at most 20 words. Never write a multi-facet profile or a list of
  unrelated preferences in one memory.
- "context" is at most 2 sentences: the latest supporting evidence, plus
  "reinforced" if this has come up repeatedly.

# OPERATIONS
1. AddMemory: the new memory's subject is not covered by any existing memory, and
   it is a lasting preference.
2. UpdateMemory: an existing memory has the SAME subject. REWRITE that memory (do
   not append to it) so it stays within the record shape:
   - If the new memory confirms it, keep the wording and note reinforcement in
     context.
   - If it refines it, fold in the refinement concisely.
   - If it contradicts it (like vs dislike), the more recent, explicit signal
     wins; state only the current polarity.
   Never use UpdateMemory to attach a different subject to an existing memory; use
   AddMemory for that.
3. SkipMemory: the new memory is redundant with an existing memory, a one-time or
   situational request, a temporary state, overly speculative, or contains PII or
   harmful content.

Prefer SkipMemory over UpdateMemory when nothing materially new is added.
"""


def _strategy_input():
    return {
        "customMemoryStrategy": {
            "name": STRATEGY_NAME,
            "description": "Durable entertainment/sports taste (genres, themes, "
                           "sports, teams, players) for hyper-personalized recs.",
            "namespaces": [PREFERENCE_NAMESPACE],
            "configuration": {
                "userPreferenceOverride": {
                    "extraction": {
                        "appendToPrompt": EXTRACTION_PROMPT,
                        "modelId": EXTRACTION_MODEL_ID,
                    },
                    "consolidation": {
                        "appendToPrompt": CONSOLIDATION_PROMPT,
                        "modelId": CONSOLIDATION_MODEL_ID,
                    },
                }
            },
        }
    }


def _modify_input(strategy_id):
    """modifyMemoryStrategies item: push the current prompts/models to an existing strategy."""
    return {
        "memoryStrategyId": strategy_id,
        "configuration": {
            "extraction": {
                "customExtractionConfiguration": {
                    "userPreferenceExtractionOverride": {
                        "appendToPrompt": EXTRACTION_PROMPT,
                        "modelId": EXTRACTION_MODEL_ID,
                    }
                }
            },
            "consolidation": {
                "customConsolidationConfiguration": {
                    "userPreferenceConsolidationOverride": {
                        "appendToPrompt": CONSOLIDATION_PROMPT,
                        "modelId": CONSOLIDATION_MODEL_ID,
                    }
                }
            },
        },
    }


def _get_strategies(client):
    r = client.get_memory(memoryId=MEMORY_ID)
    m = r.get("memory", r)
    return m, m.get("strategies", m.get("memoryStrategies", []))


def check(client):
    m, strategies = _get_strategies(client)
    print(f"memory status: {m.get('status')}")
    print(f"executionRole: {m.get('memoryExecutionRoleArn')}")
    print(f"{len(strategies)} strateg-ies:")
    for s in strategies:
        print(f"  - name={s.get('name')} type={s.get('type')} "
              f"namespaces={s.get('namespaces')} id={s.get('strategyId')}")


def add(client):
    m, strategies = _get_strategies(client)
    if any(s.get("name") == STRATEGY_NAME for s in strategies):
        print(f"Strategy '{STRATEGY_NAME}' already present — nothing to do.")
        check(client)
        return

    print(f"Adding custom User Preference strategy '{STRATEGY_NAME}' "
          f"(namespace {PREFERENCE_NAMESPACE})...")
    client.update_memory(
        memoryId=MEMORY_ID,
        memoryExecutionRoleArn=EXECUTION_ROLE_ARN,
        memoryStrategies={"addMemoryStrategies": [_strategy_input()]},
    )

    # Wait for the resource to return to ACTIVE after provisioning the strategy.
    for _ in range(40):
        m, _ = _get_strategies(client)
        status = m.get("status")
        if status == "ACTIVE":
            print("memory ACTIVE — strategy added.")
            break
        print(f"  status={status} ...")
        time.sleep(5)
    check(client)


def _wait_active(client):
    for _ in range(40):
        m, _ = _get_strategies(client)
        status = m.get("status")
        if status == "ACTIVE":
            return True
        print(f"  status={status} ...")
        time.sleep(5)
    return False


def update(client, dry_run=False):
    """Push EXTRACTION_PROMPT / CONSOLIDATION_PROMPT to the existing strategy.

    Only affects extraction/consolidation from now on; existing records are left
    as-is until a new signal updates them. Revert by restoring the previous prompts
    (git history) and re-running --update.
    """
    _, strategies = _get_strategies(client)
    match = next((s for s in strategies if s.get("name") == STRATEGY_NAME), None)
    if not match:
        raise SystemExit(f"Strategy '{STRATEGY_NAME}' not found — run without flags to add it.")
    item = _modify_input(match["strategyId"])
    if dry_run:
        print(json.dumps({"modifyMemoryStrategies": [item]}, indent=2))
        return

    print(f"Modifying strategy {match['strategyId']} prompts...")
    client.update_memory(
        memoryId=MEMORY_ID,
        memoryExecutionRoleArn=EXECUTION_ROLE_ARN,
        memoryStrategies={"modifyMemoryStrategies": [item]},
    )
    print("memory ACTIVE — strategy updated." if _wait_active(client)
          else "timed out waiting for ACTIVE.")
    check(client)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="Show current strategies and exit.")
    ap.add_argument("--update", action="store_true",
                    help="Push the current prompts/models to the existing strategy.")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --update: print the request payload without calling the API.")
    args = ap.parse_args()
    if not MEMORY_ID or not EXECUTION_ROLE_ARN:
        raise SystemExit("Set AGENTCORE_MEMORY_ID and MEMORY_EXECUTION_ROLE_ARN")
    client = boto3.client("bedrock-agentcore-control", region_name=REGION)
    if args.check:
        check(client)
    elif args.update:
        update(client, dry_run=args.dry_run)
    else:
        add(client)


if __name__ == "__main__":
    main()
