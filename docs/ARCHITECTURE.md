# JourneyOps Architecture

## Phase 2 Runtime

```mermaid
flowchart LR
    Client["Vue client or API consumer"]
    API1["FastAPI instance A"]
    API2["FastAPI instance B"]
    DB[("PostgreSQL\nsource of truth")]
    Broker[("Redis DB 1\nCelery broker")]
    Events[("Redis DB 0\nPub/Sub events")]
    Worker["Celery worker"]
    Planner["Legacy Trip Planner"]
    Providers["LLM, maps, weather, XHS"]

    Client -->|"legacy or v2 POST"| API1
    Client -->|"status query"| API2
    API1 -->|"commit trip + task"| DB
    API1 -->|"dispatch after commit"| Broker
    Broker --> Worker
    Worker -->|"load request and state"| DB
    Worker --> Planner
    Planner --> Providers
    Planner --> Worker
    Worker -->|"progress + immutable version"| DB
    Worker -->|"best-effort snapshot"| Events
    Events -->|"WebSocket trigger"| API1
    API1 -->|"reconciled event"| Client
    API2 --> DB
```

## Persistence Boundaries

- `trips` stores the canonical request and idempotency digest.
- `trip_tasks` stores execution status, progress, attempts, cancellation and terminal errors.
- `trip_versions` stores immutable outputs with unique `(trip_id, version)`.
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
    participant L as Legacy Planner

    C->>A: POST trip request
    A->>P: INSERT trip and task
    P-->>A: COMMIT
    A->>R: enqueue task_id
    A-->>C: 202 or legacy receipt
    R->>W: deliver task_id
    W->>P: lock/read task and increment attempt
    W->>L: plan_trip(request, progress_callback)
    loop planner progress
        L->>W: stage, message, progress
        W->>P: COMMIT progress
        W->>R: PUBLISH snapshot
        R-->>A: Pub/Sub event
        A-->>C: WebSocket event
    end
    W->>P: INSERT trip version and complete task
```

## Failure Policy

- API failure after commit but before dispatch leaves an explicit failed task; the same request is
  queryable and can be retried.
- Worker uses late acknowledgement, worker-loss rejection, a visibility timeout longer than the hard
  task limit, and a renewable Redis execution lock. Redelivery reuses the task row and immutable version.
- Worker startup scans undispatched and stale processing tasks. Work below its attempt limit is
  requeued; exhausted work becomes `failed` with `worker_lost`.
- Cancellation is immediate for queued work and cooperative for an executing Planner.

## Health

- API `/health/live`: process liveness only.
- API `/health/ready`: data directory, PostgreSQL and Redis.
- Compose PostgreSQL: `pg_isready`.
- Compose Redis: `redis-cli ping`.
- Compose Worker: Celery `inspect ping` against the named worker.
