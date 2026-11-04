# lambda/proxy — Bedrock Proxy Lambda

Single-function Python Lambda that authenticates bearer tokens, enforces all five
limit types, and forwards requests to AWS Bedrock.

---

## Entry point

A FastAPI app (`app.py`) served by uvicorn behind the **AWS Lambda Web Adapter
(LWA)** in a container image (`Dockerfile`), invoke mode `RESPONSE_STREAM`.
Python 3.12; dependencies (`fastapi`, `uvicorn`, `boto3`) are baked into the
image from `requirements.txt`.

---

## Request routing

The Lambda sits behind an **API Gateway REST API** (REGIONAL, AWS_PROXY,
payload format 1.0, response streaming). FastAPI routes by URL path:

| Path | Bedrock API | Notes |
|---|---|---|
| `/model/{modelId}/converse` | `converse()` | Primary path; Claude, Llama, Mistral |
| `/model/{modelId}/invoke` | `invoke_model()` | image/embedding models |
| `/model/{modelId}/converse-stream` | `converse_stream()` | SSE streaming |
| `/model/{modelId}/invoke-with-response-stream` | `invoke_model_with_response_stream()` | SSE streaming |

`modelId` is percent-encoded in the URL (`:` → `%3A`); FastAPI decodes it. The
streaming routes return Server-Sent Events and bill usage post-flight once the
stream ends. The REST API stage (`/v1`) is stripped before the app sees the path.

The invoke routes forward the client's `X-Amzn-Bedrock-Service-Tier` header (what boto3
sends for `serviceTier`) to Bedrock; non-streaming `/invoke` echoes Bedrock's reported tier
back in the same header. Converse and Responses carry the tier in the body both ways.
Invoke usage is read model-agnostically — Anthropic body keys, then Bedrock-style keys, then
the `x-amzn-bedrock-{input,output,cache-read-input,cache-write-input}-token-count` headers (streaming: `metadata.usage` or
`amazon-bedrock-invocationMetrics` on any chunk) — so non-Claude models (gpt-oss, Nova,
Llama, …) bill on `/invoke` and invoke-stream rather than at $0.

---

## Authentication

Clients send:

```
Authorization: Bearer bk_<32hex>.<64hex>
```

The Lambda splits on `.`, looks up the `token_id` in DynamoDB, verifies the
secret against the SHA-256 hash, and rejects with 401 if anything is wrong.
The secret is **never** logged.

---

## Pre-flight order

```
1. parse bearer token          header only, no I/O           → 401
2. token DynamoDB lookup       GetItem tokens table
3. revoked / missing check     in-memory                     → 401
4. secret verification         SHA-256 + HMAC compare        → 401
5. rate limit                  conditional ADD rate_limit    → 429
6+7. monthly quota + budget    GetItem usage table           → 429
8. input token cap (heuristic) in-memory ceil(chars/4)       → 413
9. model allowlist             in-memory set lookup          → 403
10. forward to Bedrock
11. post-flight usage ADD      UpdateItem usage table
```

---

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TOKENS_TABLE` | ✓ | — | DynamoDB tokens table name |
| `USAGE_TABLE` | ✓ | — | DynamoDB usage table name |
| `RATE_LIMIT_TABLE` | ✓ | — | DynamoDB rate_limit table name |
| `BEDROCK_REGION` | — | `us-east-1` | AWS region for `bedrock-runtime` |
| `ALLOWED_MODELS_DEFAULT` | — | `""` (no restriction) | Comma-separated model ID list; applies when a token has no `allowed_models` attribute. |
| `PRICING_BUCKET` | — | — | S3 bucket holding the live pricing catalog (`pricing/catalog-v2.json`). Absent/unreachable → baked-in `DEFAULT_PRICING`. |
| `LITELLM_SOURCE_URL` | — | litellm raw URL | Upstream price map the refresh endpoint pulls from. |
| `PRICING_CACHE_TTL_S` | — | `60` | Per-instance TTL for the live catalog read from S3. |

`TOKENS_TABLE`, `USAGE_TABLE`, and `RATE_LIMIT_TABLE` are read at import time and
cached across warm Lambda invocations.

---

## Error response shape

```json
{"error": {"code": "INVALID_TOKEN", "message": "Invalid or revoked token"}}
```

| Code | HTTP | Meaning |
|---|---|---|
| `INVALID_TOKEN` | 401 | Token not found, revoked, or bad secret |
| `FORBIDDEN` | 403 | Valid token lacks admin rights (admin-only routes) |
| `REFRESH_FAILED` | 502 | `POST /admin/pricing/refresh` could not fetch/validate the catalog |
| `PRICING_READ_FAILED` | 502 | `GET /admin/pricing` could not read the live catalog object |
| `RATE_LIMIT_EXCEEDED` | 429 | Per-second request rate exceeded |
| `MONTHLY_REQUEST_QUOTA_EXCEEDED` | 429 | Monthly request count exhausted |
| `MONTHLY_BUDGET_EXCEEDED` | 429 | Monthly USD budget exhausted |
| `INPUT_TOKEN_LIMIT_EXCEEDED` | 413 | Heuristic input token estimate exceeds cap |
| `MODEL_NOT_ALLOWED` | 403 | Model not in this token's allowlist |
| `BEDROCK_THROTTLED` | 429 | Bedrock returned ThrottlingException |
| `BEDROCK_ERROR` | 502 | Other Bedrock error |
| `BAD_REQUEST` | 400 | Malformed path or request body |
| `INTERNAL_ERROR` | 500 | Unhandled exception |

---

## Pricing

`pricing.py` computes cost by service tier and token class (`input`, `output`,
`cache_read_input`, `cache_write_input`) in **integer USD-micros per 1,000 tokens** (no
floats).

**The tier billed is the one the response reports**, not the one requested: Converse
`serviceTier.type`, the converse-stream `metadata` event, InvokeModel's top-level
`serviceTier`, Responses `service_tier`. Absent, `default` or `auto` mean `standard`.

**Catalog shape:** `{model_id: {"standard": rates, "flex"?: rates, "priority"?: rates}}`,
built from litellm's base and `*_flex` / `*_priority` cost fields. A Flex/Priority tier is
kept only when its input and output costs are present and each matches Standard on
zero-ness (so an upstream `*_flex: 0` cannot make a model free). A missing Standard cache
cost defaults to the output rate; a missing or zero-mismatched tier cache cost becomes
Standard's cache rate scaled by the tier's input-cost ratio (output-cost ratio when
Standard input is 0). When reading the live catalog, a malformed tier (not an object, missing
its input or output rate, or any rate that isn't a non-negative integer) is logged as
`pricing_entry_invalid` and replaced by the baked-in `DEFAULT_PRICING` rate for that model
and tier; with no baked-in rate it is dropped, and so is the whole model when that leaves no
`standard` tier. A live catalog with no valid entry at all is treated as corrupt.

**Source of rates — single path:** the live catalog is one S3 object
(`PRICING_BUCKET`/`pricing/catalog-v2.json`), read per-instance and cached for
`PRICING_CACHE_TTL_S`. When the object is absent or S3 is unreachable, the baked-in
`DEFAULT_PRICING` (generated by `scripts/gen_pricing.py`) is used as the
bootstrap/availability fallback. An unknown model falls back to conservative Opus-tier
rates; a known model without a rate for the reported tier (including commitment-priced
`reserved`) bills its own Standard rate. Both emit a `pricing_fallback_rate` warning.

**Refresh — `POST /admin/pricing/refresh` (admin token only):** re-pulls
`LITELLM_SOURCE_URL`, rebuilds the catalog with the shared `pricing_catalog`
transform, validates it (non-empty; not >20% smaller than `DEFAULT_PRICING`;
required anchor models present with sane, in-band rates at every tier they carry),
then writes S3 — all or nothing, so a bad pull never zeroes billing. Other instances
pick up the new catalog within one TTL. An admin token is one whose row has `admin == True` (CLI:
`bedrock-api issue --admin`).

`GET /admin/pricing` (admin only) returns the live catalog's provenance —
`{"source": "live", "meta": {fetched_at, source, entry_count}}` when an object
exists, or `{"source": "default", "meta": null}` when the baked-in catalog is in
use. Read-only.

Default-model rates live in `DEFAULT_PRICING` (sourced from litellm via
`scripts/gen_pricing.py`); unknown models fall back to Opus-tier conservative rates.

---

## Input token estimation

The `limit_max_input_tokens` cap is enforced using the heuristic:

```
estimated_tokens = ceil(total_prompt_chars / 4)
```

This avoids shipping a tokenizer (extra image weight and cold-start cost).
The cap is a **ceiling guard**; the true token count from Bedrock's response
metadata is what's used for billing.

---

## Output token cap

If a token row has `limit_max_output_tokens`, the Lambda injects it into the
Bedrock request before forwarding:

- **Converse:** `inferenceConfig.maxTokens = min(client_value, cap)`
- **InvokeModel:** `max_tokens = min(client_value, cap)`

---

## Structured logging

All log lines are JSON to CloudWatch Logs. Fields:

| Field | Notes |
|---|---|
| `event` | `request_complete`, `request_rejected`, `usage_write_failed`, `billing_failed`, `usage_parse_warning`, `stream_error`, `pricing_refresh`, `pricing_refresh_failed`, `pricing_read_failed`, `pricing_source_unavailable`, `pricing_fallback_rate`, `pricing_entry_invalid` |
| `token_id` | The `bk_<32hex>` prefix only — never the secret |
| `owner` | Human label from the token row |
| `model_id` | Bedrock model/profile ID |
| `input_tokens` | True count from Bedrock response |
| `output_tokens` | True count from Bedrock response |
| `cache_read_input_tokens` | Bedrock cache-read token count |
| `cache_write_input_tokens` | Bedrock cache-write token count |
| `service_tier` | Tier billed: `standard`, `flex`, `priority` (or e.g. `reserved`, billed Standard) |
| `reasoning_tokens` | Responses route: reasoning subset of `output_tokens` (logged, never billed) |
| `terminal_event` | Streaming Responses: terminal event type (`response.completed` / `.incomplete` / `.failed`) |
| `usd_micros` | Integer cost for this request |
| `status` | HTTP status code |
| `latency_ms` | Wall-clock ms |
| `error_code` | Present on rejections |

Additional pricing audit records are emitted with:
- `event=pricing_audit`
- per-component micros
- fallback flags and dimensions

`pricing_fallback_rate` (WARNING) carries `model_id`, `service_tier` and `dimension`
(`model_id`: unknown model, billed at the Opus-tier fallback; `service_tier`: known model
without a rate for that tier, billed at its Standard rate). `pricing_entry_invalid`
(WARNING) carries `model_id` and `tier` for a malformed live-catalog tier; it is replaced
from `DEFAULT_PRICING` when possible, else dropped (the whole model when it is `standard`).

---

## Partial-failure behaviour

If Bedrock succeeds but the post-flight usage `ADD` fails, the Lambda logs the
failure at `ERROR` level and **still returns 200** to the client. This accepts
rare under-counting over returning misleading 5xx responses to clients that
already received a valid Bedrock response.

---

## Packaging

Built as a container image and pushed to ECR:

```bash
docker buildx build --platform linux/amd64 --provenance=false \
  -t "$ECR_URL:latest" lambda/proxy/
docker push "$ECR_URL:latest"
```

The image bundles the FastAPI app, its dependencies (`requirements.txt`), and
the Lambda Web Adapter. `.dockerignore` excludes tests, `*.md`, and
`__pycache__/`.
