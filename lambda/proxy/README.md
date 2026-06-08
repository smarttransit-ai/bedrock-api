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
streaming routes return Server-Sent Events and bill usage post-flight from the
terminal `metadata` event. The REST API stage (`/v1`) is stripped before the
app sees the path.

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
| `PRICING_JSON` | — | built-in map | JSON object overriding the static price map. Same shape as `DEFAULT_PRICING` in `pricing.py`. |

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

`pricing.py` computes pricing by mode (`on_demand` or `batch`) and token class
(`input`, `output`, `cache_read_input`, `cache_write_input`) in **integer USD-micros
per 1,000 tokens** (no floats). Override at runtime via `PRICING_JSON`.

Unknown model/mode/class mappings use conservative fallback rates and emit
structured fallback warning events.

Default models (prices from https://aws.amazon.com/bedrock/pricing/ on 2025-05-14):

| Model | Input µUSD/1k | Output µUSD/1k |
|---|---|---|
| `us.anthropic.claude-sonnet-4-6` | 6 000 | 15 000 |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` | 2 000 | 5 000 |

Unknown models fall back to Opus-tier conservative rates.

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
| `event` | `request_complete`, `request_rejected`, `usage_write_failed`, `billing_failed`, `stream_error` |
| `token_id` | The `bk_<32hex>` prefix only — never the secret |
| `owner` | Human label from the token row |
| `model_id` | Bedrock model/profile ID |
| `input_tokens` | True count from Bedrock response |
| `output_tokens` | True count from Bedrock response |
| `cache_read_input_tokens` | Bedrock cache-read token count |
| `cache_write_input_tokens` | Bedrock cache-write token count |
| `pricing_mode` | `on_demand` or `batch` |
| `usd_micros` | Integer cost for this request |
| `status` | HTTP status code |
| `latency_ms` | Wall-clock ms |
| `error_code` | Present on rejections |

Additional pricing audit records are emitted with:
- `event=pricing_audit`
- per-component micros
- fallback flags and dimensions

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
