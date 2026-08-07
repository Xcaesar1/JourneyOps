# JourneyOps Architecture

## Phase 3 Runtime

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
    Checkpoints[("PostgreSQL checkpoints")]
    Adapter["TripPlanV2 to legacy Adapter"]
    Providers["LLM, maps, weather, XHS"]

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
    Legacy --> Worker
    Graph --> Providers
    Graph <--> Checkpoints
    Graph --> Adapter
    Adapter --> Worker
    Worker -->|"progress + immutable version"| DB
    Worker -->|"best-effort snapshot"| Events
    Events -->|"WebSocket trigger"| API1
    API1 -->|"reconciled event"| Client
    API2 --> DB
```

## Persistence Boundaries

- `trips` stores the canonical request and idempotency digest.
- `trip_tasks` stores execution status, progress, attempts, cancellation and terminal errors.
- `trip_versions` stores immutable outputs with unique `(trip_id, version)`. Phase 3 adds planner engine,
  primary/comparison role, schema version and optional native `TripPlanV2` payload metadata.
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

The phase 3 graph is deliberately small and recoverable:

```mermaid
flowchart LR
    Start([START]) --> Normalize[normalize_request]
    Normalize --> Collect[collect]
    Collect --> Draft[draft]
    Draft --> Validate[validate_stub]
    Validate --> Persist[persist]
    Persist --> End([END])
```

`draft` uses provider-native JSON output and validates it directly as `TripPlanV2`. It does not call legacy
string sanitizers, bracket completion or LLM JSON repair. The Adapter converts the validated native plan to
the existing `TripPlanResponse` shape while the native payload remains available in `trip_versions`.

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

## Health

- API `/health/live`: process liveness only.
- API `/health/ready`: data directory, PostgreSQL and Redis.
- Compose PostgreSQL: `pg_isready`.
- Compose Redis: `redis-cli ping`.
- Compose Worker: Celery `inspect ping` against the named worker.
