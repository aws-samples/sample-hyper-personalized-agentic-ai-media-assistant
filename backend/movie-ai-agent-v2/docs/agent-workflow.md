# Movie AI Agent — Workflow

The movie AI agent is a single AgentCore runtime (`main.py`) serving two
pipelines behind one streaming endpoint. It dispatches on `request_type`,
resolves the caller identity from the JWT, runs the chosen pipeline on a
background thread, and drains its yielded events out over SSE (with a 5s
keepalive during silent gaps).

## Recommendation pipeline

A five-stage loop where each stage is its own Strands `Agent` with its own
model, so cost tracks difficulty.

- **Router** decides whether the message needs a catalog search. Greetings,
  off-topic asks, and follow-ups about already-recommended titles are answered
  directly and skip the rest of the pipeline.
- **Planner** produces a natural-language search plan (does not write code).
- **Executor** turns the plan into a single Python script run through
  `python_repl`, calling the helpers in `tools/search.py` against OpenSearch. If
  the search returns nothing, it runs once more with relaxed filters.
- **Reflector** grades results and can loop back to planning. Skipped on the
  first pass for latency, so it only runs on a retry.
- **Responder** reranks against user preferences and emits JSON with the final
  `title_ids`, which are rehydrated from the module-level stash into full
  records plus presigned `content_urls`.

## Analysis pipeline

An intent classifier routes each scene question to one of four paths:

- **Scene detail** — chapter summaries, audio-segment dialog, and Knowledge
  Base retrieval + rerank.
- **Scene actors** — Rekognition celebrity detection on the scene screenshot
  combined with a catalog cast lookup.
- **Overall story** — history-aware query rewrite, then `retrieve_and_generate`
  against the Knowledge Base.
- **Recommendation** — rewrites the question into a self-contained query and
  hands off to the recommendation pipeline mid-stream (crossover), so "find me
  more like this" works while watching.

## Workflow diagram

```mermaid
flowchart TD
    Client([Client: frontend or voice agent]) -->|POST invocations, SSE| Entry["main.py handle()"]
    Entry --> Resolve["_resolve_user_id: JWT username + profile"]
    Resolve --> Dispatch{request_type?}

    Dispatch -->|recommendation default| RecStart
    Dispatch -->|analysis| AnaStart

    subgraph REC [Recommendation pipeline - agent.py]
        RecStart["run_agent_stream"] --> Router["Router agent"]
        Router --> RouteQ{Needs catalog search?}
        RouteQ -->|No: greeting / off-topic / follow-up| DirectResp["Return router's direct answer"]
        RouteQ -->|Yes: RECOMMENDATION| Plan["Planner: natural-language plan"]
        Plan --> Exec["Executor: python_repl + tools/search.py"]
        Exec --> ZeroQ{0 results?}
        ZeroQ -->|Yes| Fallback["Re-run with relaxed filters"]
        ZeroQ -->|No| Reflect
        Fallback --> Reflect
        Reflect{"First attempt?"}
        Reflect -->|Yes: skip reflection| Respond
        Reflect -->|No: grade results| Verdict{PASS or RETRY?}
        Verdict -->|RETRY & retries left| Plan
        Verdict -->|PASS / max retries| Respond
        Respond["Responder: rerank, emit JSON title_ids"] --> Hydrate["Rehydrate from stash + attach content_urls"]
    end

    subgraph ANA [Analysis pipeline - analysis_agent.py]
        AnaStart["run_analysis_stream"] --> Classify["classify_intent"]
        Classify --> Intent{Intent?}
        Intent -->|scene_detail| Detail["Chapter summaries + audio segment + KB retrieve/rerank"]
        Intent -->|scene_actors| Actors["Rekognition on screenshot + cast lookup"]
        Intent -->|overall_story| Story["Query rewrite + retrieve_and_generate KB"]
        Intent -->|movie_recommendation| Cross["Rewrite to self-contained query"]
        Cross -->|crossover event| RecStart
    end

    subgraph DATA [Data sources]
        OS[(OpenSearch: movie + scene indices)]
        KB[(Bedrock Knowledge Base)]
        DDB[(DynamoDB: profiles + title mapping)]
        S3[(S3: screenshots + video)]
        MEM[(AgentCore Memory: STM + LTM)]
    end

    Exec -.-> OS
    Detail -.-> OS
    Detail -.-> KB
    Story -.-> KB
    Actors -.-> S3
    Actors -.-> DDB
    Hydrate -.-> DDB
    Hydrate -.-> S3
    RecStart -.-> MEM

    Hydrate --> RespEvent["yield response event"]
    DirectResp --> RespEvent
    Detail --> RespEvent
    Actors --> RespEvent
    Story --> RespEvent

    RespEvent --> Queue["asyncio.Queue drained by handle()"]
    Queue -->|status events + keepalive every 5s| Client
    RespEvent -->|final response event| Client
```

## Notes

- Every stage transition also yields `status` events; the diagram shows only the
  final `response`/status flow back to the client for readability.
- Memory (STM history + one LTM retrieval on the first turn of a session) feeds
  the router and planner inputs. Each completed turn is written back, with
  previously recommended `title_id`s tracked for exclusion.
- The stash is a module-level dict keyed by `title_id`: the executor fills it,
  the models only see a trimmed projection, and full records are pulled back out
  at the `Hydrate` step, keeping images and full plots out of the token budget.
