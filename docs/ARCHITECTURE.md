# JourneyOps Architecture

## Phase 5 Runtime

```mermaid
flowchart LR
    Client["Vue client or API consumer"]
    API1["FastAPI instance A"]
    API2["FastAPI instance B"]
    DB[("PostgreSQL\nsource of truth")]
    Broker[("Redis DB 1\nCelery broker")]
    Events[("Redis DB 0\nPub/Sub events")]
    Worker["Celery worker"]
    Selector{"Planner engine flags"}
    Legacy["Legacy Trip Planner"]
    Graph["JourneyGraph typed workflow"]
    Queries["Bounded research queries"]
    WebResearch["Brave / Noop / Fallback"]
    SourceCache[("Redis source cache with TTL")]
    Evidence[("SourceEvidence and version links")]
    Community["Optional XHS community context"]
    Routing["AMap / Noop / Fallback route estimates"]
    Enrich["Deterministic timeline and budget"]
    Validate["Six deterministic validators"]
    Revise["Bounded revise loop, max 2"]
    Checkpoints[("PostgreSQL checkpoints")]
    Adapter["TripPlanV2 to legacy Adapter"]
    UI["Transport, timeline and validation UI"]
    Providers["LLM, maps and weather"]

    Client -->|"legacy or v2 POST"| API1
    Client -->|"status query"| API2
    API1 -->|"commit trip + task"| DB
    API1 -->|"dispatch after commit"| Broker
    Broker --> Worker
    Worker -->|"load request and state"| DB
    Worker --> Selector
    Selector --> Legacy
    Selector --> Graph
    Legacy --> Providers
    Legacy --> Community
    Legacy --> Worker
    Graph --> Queries
    Queries --> WebResearch
    WebResearch <--> SourceCache
    WebResearch --> Graph
    Graph --> Providers
    Graph --> Routing
    Routing --> Enrich
    Enrich --> Validate
    Validate --> Revise
    Revise --> Enrich
    Graph <--> Checkpoints
    Graph --> Adapter
    Graph --> Evidence
    Adapter --> Worker
    Worker -->|"progress + immutable version"| DB
    Worker -->|"best-effort snapshot"| Events
    Events -->|"WebSocket trigger"| API1
    API1 -->|"reconciled event"| Client
    API2 --> DB
    DB --> UI
```

## Persistence Boundaries

- `trips` stores the canonical request and idempotency digest.
- `trip_tasks` stores execution status, progress, attempts, cancellation and terminal errors.
- `trip_versions` stores immutable outputs with unique `(trip_id, version)`. Phase 3 adds planner engine,
  primary/comparison role, schema version and optional native `TripPlanV2` payload metadata.
- The phase 5 native payload owns the normalized origin, route estimates, intercity transport options,
  contiguous schedule items, recalculated budget, validation report and revision count. These values remain
  part of the immutable version rather than mutable process state.
- `source_evidence` deduplicates source metadata and explicit `unknown` markers. `trip_source_links` binds
  evidence to an immutable trip version, so a later refresh cannot silently rewrite historical output.
- LangGraph checkpoint tables store resumable typed graph state by durable task id. Their serializer rejects
  types outside the explicit allowlist when `LANGGRAPH_STRICT_MSGPACK=true`.
- Redis broker messages and Pub/Sub events may be lost or duplicated; database constraints and Worker
  transitions make redelivery safe.
- `backend/data/trip_tasks/*.json`, process dictionaries and process-local WebSocket queues are no
  longer used as task state.

## Execution Sequence

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant P as PostgreSQL
    participant R as Redis
    participant W as Celery Worker
    participant S as Engine Selector
    participant G as JourneyGraph or Legacy Planner

    C->>A: POST trip request
    A->>P: INSERT trip and task
    P-->>A: COMMIT
    A->>R: enqueue task_id
    A-->>C: 202 or legacy receipt
    R->>W: deliver task_id
    W->>P: lock/read task and increment attempt
    W->>S: read PLANNER_ENGINE and comparison flag
    S->>G: run primary and optional shadow engine
    loop planner progress
        G->>W: stage, message, progress
        W->>P: COMMIT progress
        W->>R: PUBLISH snapshot
        R-->>A: Pub/Sub event
        A-->>C: WebSocket event
    end
    W->>P: INSERT trip version and complete task
```

## JourneyGraph

The phase 5 graph adds route planning, deterministic enrichment, validation and bounded revision while
retaining phase 4 research and durable checkpoints:

```mermaid
flowchart LR
    Start([START]) --> Normalize[normalize_request]
    Normalize --> Prepare[prepare_research_queries]
    Prepare --> Research[research_web]
    Research --> Collect[collect]
    Collect --> Transport[plan_intercity_transport]
    Transport --> Draft[draft]
    Draft --> Enrich[enrich_plan]
    Enrich --> Validate[deterministic_validate]
    Validate -->|"critical and revisions < 2"| Revise[revise_plan]
    Revise --> Enrich
    Validate -->|"no critical or limit reached"| Persist[persist]
    Persist --> End([END])
```

`prepare_research_queries` and `research_web` retain the phase 4 evidence policy. After collection,
`plan_intercity_transport` estimates the origin-to-first-city and between-city legs through the configured
route Provider and creates at most two deterministic planning options per leg with exactly one recommendation.
AMap supplies driving distance and duration as route evidence; train and flight durations/costs are planning
estimates, not schedules, availability or live fares.

`draft` validates provider-native JSON as `TripPlanV2`, then restores request-owned identity fields and
graph-owned evidence. `enrich_plan` creates unique schedule items that close each requested daily window and
recalculates all budget components. `deterministic_validate` runs structure/date, time, route, budget, opening
hours and intensity checks. A critical issue loops through `revise_plan` at most twice; unresolved issues are
persisted and shown instead of being hidden. The Adapter carries these fields into the legacy frontend response.

Official trust is assigned only through configured domain allowlists or recognized government suffixes.
Web evidence is cached in Redis using `SOURCE_CACHE_TTL_SECONDS`; XHS is optional community context and is
disabled unless both `XHS_ENABLED=true` and a Cookie are configured.

## Failure Policy

- API failure before broker dispatch leaves an explicit failed task. If the broker accepted a message
  but the API could not persist its broker id, the task remains queued for automatic recovery.
- Worker uses late acknowledgement, worker-loss rejection, a visibility timeout longer than the hard
  task limit, and a cross-thread renewable Redis execution lock. Redelivery reuses the task row and
  immutable version.
- Worker startup and its periodic recovery loop scan undispatched, stale queued/retrying, and stale
  processing tasks. A row lock revalidates each candidate, a persisted recovery claim prevents parallel
  dispatch, and a live execution lock protects slow work. Work below its attempt limit is requeued;
  exhausted work becomes `failed` with `worker_lost`.
- Cancellation is immediate for queued work and cooperative for an executing Planner.
- Shadow comparison errors are redacted and do not fail a successful primary run. A primary failure cancels
  outstanding comparison work.
- Web research maps authentication, rate-limit, timeout, empty and malformed responses to fixed safe codes.
  One Provider failure falls through to the next Provider; when none succeeds, planning continues with
  `unknown` evidence.
- Route lookup maps Provider failure to a safe unavailable estimate and deterministic fallback options. Raw
  upstream payloads and credential-bearing request URLs do not enter graph state, API responses or INFO logs.
- Validation is program-owned. The LLM cannot override request identity, computed route metadata, timeline,
  budget totals, validation results or the two-revision limit.
- XHS failures return a language-specific fallback context. Raw Cookie values and upstream exception text do
  not enter graph state, API responses or logs.

## Health

- API `/health/live`: process liveness only.
- API `/health/ready`: data directory, PostgreSQL and Redis.
- Compose PostgreSQL: `pg_isready`.
- Compose Redis: `redis-cli ping`.
- Compose Worker: Celery `inspect ping` against the named worker.
