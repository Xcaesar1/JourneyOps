# API v2 Phase 1

`/api/v2` is a non-persistent mock skeleton in Phase 1. Legacy `/api/*` routes remain the production behavior and still own real trip generation.

## Scope

- `POST /api/v2/trips` validates the new request contract.
- The response is a mock `202 Accepted` task record.
- No planner execution starts.
- No background task starts.
- No `/api/v2` persistence starts.

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

## Accepted Response Example

```json
{
  "task_id": "task_1234567890ab",
  "trip_id": "trip_1234567890ab",
  "status": "accepted",
  "created_at": "2026-08-06T00:00:00Z",
  "message": "Accepted by the Phase 1 mock endpoint. No planner execution or persistence has started."
}
```

## Error Envelope Example

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed.",
    "details": [
      {
        "field": "budget_total",
        "message": "Input should be greater than 0",
        "code": "greater_than"
      }
    ]
  }
}
```

Unexpected v2 failures use the same envelope with HTTP `500` and the stable code
`internal_server_error`; internal exception details are not exposed to clients.

## Validation Rules

- `start_date` and `end_date` must be valid dates.
- `end_date` must be on or after `start_date`.
- `travel_days` must be between `1` and `30`.
- `travel_days` must match the inclusive date range.
- The sum of `destinations[].days` must match `travel_days`.
- `budget_total` must be positive when provided.

## OpenAPI

Swagger and `/openapi.json` now include request, accepted-response, and error examples for `POST /api/v2/trips`.
