# API v2 Durable Tasks

阶段 2 将 `/api/v2` 从 mock 升级为 PostgreSQL 持久任务 API。阶段 3 在 Worker 内增加可切换的
JourneyGraph。阶段 4 在 JourneyGraph 输出及 legacy Adapter 中增加来源证据，但不改变任务提交、
轮询和 WebSocket 契约。API 先提交数据库事务，再投递 Celery；Redis 只承载 broker、Pub/Sub
事件和有 TTL 的来源缓存，不是任务事实源。

JourneyGraph 原生输出按 `TripPlanV2` 校验并保存在 `trip_versions.native_payload`，客户端 `result`
继续返回现有前端可消费的 legacy `TripPlanResponse`。Planner engine 由服务端 Feature Flag 选择，
不是请求参数。

## Endpoints

| Method | Path | Result |
| --- | --- | --- |
| `POST` | `/api/v2/trips` | 创建或返回幂等任务，返回 `202` |
| `GET` | `/api/v2/trips/tasks/{task_id}` | 从 PostgreSQL 查询任务 |
| `POST` | `/api/v2/trips/tasks/{task_id}/cancel` | 取消排队任务或请求协作式取消 |
| `POST` | `/api/v2/trips/tasks/{task_id}/retry` | 重试失败或已取消任务 |
| `WS` | `/api/v2/trips/tasks/{task_id}/ws` | 订阅 Redis Pub/Sub 进度 |

旧 `/api/trip/plan`、`/api/trip/status/{task_id}`、`/api/trip/history` 和
`/api/trip/ws/{task_id}` 保留原响应结构，但内部使用同一个数据库、Worker 和 Pub/Sub。

## Idempotency

- 推荐客户端为 `POST` 提供 `Idempotency-Key`。
- 未提供时，V2 使用规范化请求体摘要作为幂等键。
- legacy API 未提供 Header 时保留“每次创建新任务”的旧行为。
- 相同键和相同请求返回原任务；相同键但请求内容不同返回 `409 conflict`。
- 数据库对 `trips.idempotency_key` 和 `(trip_id, version)` 施加唯一约束。

## Request Example

```json
{
  "origin": "Shanghai",
  "destinations": [
    { "city": "Tokyo", "days": 3 },
    { "city": "Kyoto", "days": 2 }
  ],
  "start_date": "2026-10-10",
  "end_date": "2026-10-14",
  "travel_days": 5,
  "budget_total": "12000.00",
  "currency": "CNY",
  "travelers": 2,
  "transport_preferences": ["flight", "train"],
  "accommodation_preference": "midscale hotel",
  "interests": ["food", "museums"],
  "must_visit": ["Senso-ji"],
  "avoid": ["red-eye flights"],
  "pace": "balanced",
  "daily_start_time": "09:00:00",
  "daily_end_time": "21:00:00",
  "max_daily_walking_minutes": 180,
  "accessibility_needs": ["elevator access"],
  "free_text_input": "Keep the first day light after arrival.",
  "language": "en",
  "timezone": "Asia/Tokyo"
}
```

## Task Example

```json
{
  "task_id": "task_1234567890ab",
  "trip_id": "trip_1234567890ab",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "attempt_count": 0,
  "max_attempts": 3,
  "created_at": "2026-08-07T00:00:00Z",
  "updated_at": "2026-08-07T00:00:00Z",
  "started_at": null,
  "finished_at": null,
  "message": "Task queued for durable execution.",
  "result": null,
  "error": null
}
```

状态集合为 `queued`、`processing`、`retrying`、`cancel_requested`、`cancelled`、
`completed`、`failed`。WebSocket 首先返回数据库快照，随后返回 Pub/Sub 事件；终态后关闭。

## Source Evidence

成功结果的 `result.data` 增加以下兼容字段：

| Field | Meaning |
| --- | --- |
| `source_evidence` | 事实来源数组；关键查询无结果时包含 URL 为 `null` 的 `unknown` 记录 |
| `research_updated_at` | 本次结果所用证据中最新的抓取时间 |
| `research_status` | `complete`、`partial` 或 `unavailable` |

每条 `SourceEvidence` 包含 `id`、`title`、`url`、`domain`、`provider`、`claim_type`、
`claim_text`、`published_at`、`fetched_at`、`freshness_status`、`trust_level` 和 `confidence`。
服务端只接受 `http`/`https` 来源 URL；前端也会再次过滤可点击协议。模型不能覆盖 Graph
收集到的证据。

## Error Contract

V2 保持统一 envelope，内部异常、数据库 URL、Redis URL、Cookie 和 API Key 不进入响应。Worker
持久化外部供应商异常前会脱敏已配置凭据、认证头、敏感键值和带凭据 URL。

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed.",
    "details": []
  }
}
```
