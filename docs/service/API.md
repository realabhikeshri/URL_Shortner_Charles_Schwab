# URL Shortener reference

Version 0.1.0. Generated from the live OpenAPI schema by the DocWriter agent; do not edit by hand.

## `POST /api/v1/links`

Create Link

| Parameter | In | Required |
|---|---|---|
| `Idempotency-Key` | header | False |

Request body: `LinkCreate`

Responses: `201`, `422`

## `DELETE /api/v1/links/{code}`

Delete Link

| Parameter | In | Required |
|---|---|---|
| `code` | path | True |

Responses: `204`, `422`

## `GET /api/v1/links/{code}`

Get Link

| Parameter | In | Required |
|---|---|---|
| `code` | path | True |

Responses: `200`, `422`

## `PATCH /api/v1/links/{code}`

Update Link

| Parameter | In | Required |
|---|---|---|
| `code` | path | True |

Request body: `LinkUpdate`

Responses: `200`, `422`

## `GET /api/v1/links/{code}/stats`

Click statistics

| Parameter | In | Required |
|---|---|---|
| `code` | path | True |
| `days` | query | False |

Responses: `200`, `422`

## `GET /healthz`

Liveness probe

Responses: `200`

## `GET /readyz`

Readiness probe (database and cache reachable)

Responses: `200`

## `GET /{code}`

Follow

| Parameter | In | Required |
|---|---|---|
| `code` | path | True |

Responses: `302`, `404`, `410`, `422`, `429`
