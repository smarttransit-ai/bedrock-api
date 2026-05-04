# T04 — Bedrock Proxy Lambda (Python, boto3)

## Problem Summary

Implement the core Lambda handler that authenticates bearer tokens, enforces all
five limit types, and forwards requests to AWS Bedrock. This is the critical path
for every API call — correctness, security, and auditability all depend on it.

The handler must:
- Parse and verify bearer tokens against DynamoDB.
- Enforce: per-second rate limit, monthly request quota, monthly USD budget,
  per-request input-token cap, and per-token model allowlist.
- Forward to Bedrock Converse API (primary) or InvokeModel (when client requests
  `/invoke` path).
- Record usage atomically after each successful Bedrock call.
- Return structured errors with stable codes.
- Log structured JSON to CloudWatch.

---

## Design Decisions

### a) Module layout

| Module | Responsibility |
|---|---|
| `auth.py` | Token parsing, DynamoDB lookup, secret verification (SHA-256 + HMAC) |
| `limits.py` | Rate-limit conditional write, quota/budget reads, input-token heuristic, model allowlist, usage write |
| `pricing.py` | Static price map (USD-micros per 1k tokens), cost computation |
| `bedrock.py` | Path parsing, output-cap injection, Converse + InvokeModel forwarding |
| `handler.py` | Lambda entry point — orchestrates the above, structured logging, error mapping |

Modules expose plain functions and custom exception classes (`AuthError`,
`LimitError`, `BedrockError`). Handler catches them all and maps to HTTP responses.

### b) Lambda entry shape

API Gateway HTTP API v2 (`payloadFormatVersion=2.0`). Key event fields:

```json
{
  "version": "2.0",
  "rawPath": "/model/us.anthropic.claude-sonnet-4-6-20250514-v1%3A0/converse",
  "headers": {
    "authorization": "Bearer bk_<32hex>.<64hex>"
  },
  "body": "{\"messages\": [...], \"inferenceConfig\": {\"maxTokens\": 1024}}",
  "isBase64Encoded": false
}
```

Headers from API GW HTTP API v2 are always lowercase. Body may be base64-encoded
(`isBase64Encoded: true`) for binary requests; handler decodes before parsing.

The model ID is URL-encoded in the path (`:` → `%3A`). `urllib.parse.unquote`
decodes it before use.

### c) Pre-flight ordering (cheap rejects first)

```
1. parse_bearer_token        — header parse only, no I/O; reject bad format → 401
2. token lookup              — 1× GetItem on tokens table
3. revoked/missing check     — in-memory; reject revoked/missing → 401
4. secret verification       — SHA-256 + hmac.compare_digest; no I/O → 401
5. rate-limit check          — 1× conditional UpdateItem on rate_limit table → 429
6. monthly request quota     — 1× GetItem on usage table (shared read for 6+7)
7. monthly USD budget        — same usage item; no additional I/O → 429
8. input-token cap           — in-memory heuristic on request body → 413
9. model allowlist           — in-memory set lookup → 403
10. forward to Bedrock       — Converse or InvokeModel
11. post-flight usage write  — 1× UpdateItem ADD on usage table
```

**Rationale:**
- Steps 1–4: zero or one DynamoDB read, no writes. Cheapest rejections.
- Step 5 (rate limit) writes before the heavier quota read; this is intentional —
  the rate-limit write is the per-second guard and must fire before any per-month
  accounting to avoid counting rejected-rate-limit calls against monthly quotas.
- Steps 6–7 share one DynamoDB read (same usage item).
- Steps 8–9 are in-memory; they come after I/O checks so we never touch the
  rate-limit table for tokens that are already over quota.
- Bedrock call only happens after all pre-flight checks pass.
- Usage write is post-flight (we only bill for successful Bedrock calls).

### d) Input token estimation

**Heuristic: `math.ceil(total_prompt_chars / 4)`**

We do NOT call a tokenizer. Shipping a real tokenizer (e.g., HuggingFace
`tokenizers` or `tiktoken`) would add 5–50 MB to the Lambda ZIP — exceeding our
5 MB budget and adding cold-start latency.

For Converse API requests, we extract text from
`messages[*].content[*].text` and `system[*].text`. For InvokeModel (Anthropic
format) we extract `messages[*].content` text and `system`. If no text is found,
we fall back to `len(json.dumps(body))` characters.

The cap is a **heuristic ceiling**: actual Bedrock token count may differ. The
cap prevents obviously oversized requests. Post-flight, we use the true token
count from Bedrock's response metadata for billing.

### e) Output token cap

The token row may carry `limit_max_output_tokens`. If present, the handler
injects it into the Bedrock request before forwarding:

- **Converse:** `inferenceConfig.maxTokens = min(user_value, cap)`
  (cap wins if user didn't specify; both values present → take minimum).
- **InvokeModel (Anthropic):** `max_tokens = min(user_value, cap)`.

This ensures Bedrock never returns more tokens than the cap allows. If the user
already specified a lower value, we respect theirs.

### f) Pricing

Static map in `pricing.py`, keyed by Bedrock model / inference-profile ID.
Values in **integer USD-micros per 1,000 tokens** (no floats ever).

```
Prices as of 2025-05-14 — source: https://aws.amazon.com/bedrock/pricing/
```

| Model | Input µUSD/1k | Output µUSD/1k |
|---|---|---|
| `us.anthropic.claude-sonnet-4-6-20250514-v1:0` | 3 000 | 15 000 |
| `us.anthropic.claude-haiku-4-5-20250207-v1:0` | 800 | 4 000 |

Fallback for unknown models: Sonnet 4.6 rates (conservative over-charge rather
than under-charge). Overridable via `PRICING_JSON` env var (JSON dict with same
shape as `DEFAULT_PRICING`).

Cost formula (integer arithmetic throughout):
```
input_cost  = (input_tokens  * input_usd_micros_per_1k)  // 1000
output_cost = (output_tokens * output_usd_micros_per_1k) // 1000
total       = input_cost + output_cost
```

### g) Failure handling

Each pre-flight check raises a typed exception (`AuthError`, `LimitError`,
`BedrockError`) with `code`, `message`, `status` attributes.

| Exception | Stable code | HTTP status |
|---|---|---|
| `AuthError` | `INVALID_TOKEN` | 401 |
| `LimitError` | `RATE_LIMIT_EXCEEDED` | 429 |
| `LimitError` | `MONTHLY_REQUEST_QUOTA_EXCEEDED` | 429 |
| `LimitError` | `MONTHLY_BUDGET_EXCEEDED` | 429 |
| `LimitError` | `INPUT_TOKEN_LIMIT_EXCEEDED` | 413 |
| `LimitError` | `MODEL_NOT_ALLOWED` | 403 |
| `BedrockError` | `BEDROCK_THROTTLED` | 429 (passthrough) |
| `BedrockError` | `BEDROCK_ERROR` | 502 |

Bedrock `ThrottlingException` and `TooManyRequestsException` → 429.
All other `ClientError` from Bedrock → 502.
Unhandled Python exceptions → 500 (logged via `logger.exception`).

Error response body:
```json
{"error": {"code": "INVALID_TOKEN", "message": "Invalid or revoked token"}}
```

### h) Logging

Structured JSON to CloudWatch via Python's standard `logging` module (Lambda
routes it to CloudWatch Logs automatically). Log fields:

| Field | Present when | Notes |
|---|---|---|
| `event` | always | `"request_complete"` or `"request_rejected"` |
| `token_id` | after parse | **Never** log the secret portion |
| `owner` | after auth | Human label from token row |
| `model_id` | after route parse | Bedrock model/profile ID |
| `input_tokens` | on success | True count from Bedrock response |
| `output_tokens` | on success | True count from Bedrock response |
| `usd_micros` | on success | Integer cost |
| `status` | always | HTTP status code |
| `latency_ms` | always | Wall-clock ms for the full request |
| `error_code` | on rejection | Stable code string |

Usage write failures also log `"event": "usage_write_failed"` with `"error"` at
`ERROR` level.

### i) Idempotency / partial failure

If Bedrock succeeds but the usage `ADD` write fails (DynamoDB transient error,
throttle, etc.), the handler:
1. Logs the failure at `ERROR` level with full context.
2. **Returns the Bedrock response to the client with HTTP 200.**

We accept rare under-counting over returning hard 5xx to the client. The client
already received the Bedrock output; a 500 at this point would be misleading.
This is a deliberate design choice: the system is metering, not a billing-critical
payment processor. Operators can cross-check Bedrock's own usage dashboard for
reconciliation.

### j) Configuration

All configuration via environment variables:

| Env var | Required | Default | Description |
|---|---|---|---|
| `TOKENS_TABLE` | ✓ | — | DynamoDB tokens table name |
| `USAGE_TABLE` | ✓ | — | DynamoDB usage table name |
| `RATE_LIMIT_TABLE` | ✓ | — | DynamoDB rate_limit table name |
| `BEDROCK_REGION` | — | `us-east-1` | AWS region for bedrock-runtime client |
| `ALLOWED_MODELS_DEFAULT` | — | `""` (no restriction) | Comma-separated list of allowed model IDs; applied when token has no `allowed_models` attribute |
| `PRICING_JSON` | — | built-in map | JSON dict overriding the static pricing map |

`TOKENS_TABLE`, `USAGE_TABLE`, `RATE_LIMIT_TABLE` are read at module import time
(Lambda warm-start cache). The others are read at call time (allows runtime
update via Lambda console without code deploy).

---

## Step-by-Step Implementation

### Step 1: `lambda/proxy/auth.py`

```python
class AuthError(Exception):
    code: str; message: str; status: int = 401

def parse_bearer_token(event) -> tuple[str, str]:
    # Extract Authorization header (lowercase in APIGW HTTP API v2)
    # Strip "Bearer ", split on first "." → (token_id, secret)
    # Raise AuthError on malformed input

def verify_secret(secret: str, secret_hash: str) -> bool:
    # Split secret_hash on ":" → (salt_hex, stored_hash)
    # sha256(bytes.fromhex(salt_hex) + bytes.fromhex(secret)).hexdigest()
    # hmac.compare_digest(candidate, stored_hash)
```

### Step 2: `lambda/proxy/limits.py`

```python
class LimitError(Exception):
    code: str; message: str; status: int

def check_rate_limit(token_id, token_row, rate_limit_table) -> None:
    # Skip if "limit_rps" absent
    # Explicit 0-check before DynamoDB (attribute_not_exists guard would let
    # first-of-second through even with limit=0)
    # conditional UpdateItem ADD on (token_id, window_second)
    # CCF → raise LimitError("RATE_LIMIT_EXCEEDED", ..., 429)

def check_monthly_quota(token_row, usage) -> None:
    # Check limit_monthly_requests vs usage["requests"]
    # Check limit_monthly_usd_micros vs usage["usd_micros"]

def estimate_input_tokens(body, route) -> int:
    # Converse: extract chars from messages[*].content[*].text + system[*].text
    # InvokeModel: extract from messages + system (Anthropic format)
    # Fallback: len(json.dumps(body))
    # Return ceil(chars / 4)

def check_input_cap(estimated_tokens, token_row) -> None:
    # Skip if "limit_max_input_tokens" absent

def check_model_allowlist(model_id, token_row, default_models) -> None:
    # token_row has "allowed_models" → use that set
    # else if default_models list non-empty → use that
    # else → no restriction

def write_usage(token_id, period, in_tokens, out_tokens, usd_micros, usage_table):
    # ADD requests :r, input_tokens :i, output_tokens :o, usd_micros :u
```

### Step 3: `lambda/proxy/pricing.py`

```python
# Static price map. USD-micros per 1k tokens.
# Source: https://aws.amazon.com/bedrock/pricing/ (2025-05-14)
DEFAULT_PRICING = {
    "us.anthropic.claude-sonnet-4-6-20250514-v1:0": {
        "input_usd_micros_per_1k": 3_000,    # $3.00/1M
        "output_usd_micros_per_1k": 15_000,  # $15.00/1M
    },
    "us.anthropic.claude-haiku-4-5-20250207-v1:0": {
        "input_usd_micros_per_1k": 800,      # $0.80/1M
        "output_usd_micros_per_1k": 4_000,   # $4.00/1M
    },
}

def compute_cost(model_id, input_tokens, output_tokens) -> int:
    # Load from PRICING_JSON env var or DEFAULT_PRICING
    # Integer arithmetic: // 1000 (no floats)
```

### Step 4: `lambda/proxy/bedrock.py`

```python
class BedrockError(Exception):
    code: str; message: str; status: int

def parse_route(event) -> tuple[str, str]:
    # rawPath = "/model/{url-encoded-model-id}/converse" or "/invoke"
    # urllib.parse.unquote the model_id
    # route must be "converse" or "invoke"

def apply_output_cap(body, route, max_output_tokens) -> dict:
    # Converse: inferenceConfig.maxTokens = min(existing, cap)
    # InvokeModel: max_tokens = min(existing, cap)

def forward_converse(client, model_id, body) -> tuple[dict, int, int]:
    # client.converse(modelId=model_id, **body_without_modelId)
    # response["usage"]["inputTokens"], ["outputTokens"]
    # Pop ResponseMetadata; return (response, in_tok, out_tok)

def forward_invoke_model(client, model_id, body) -> tuple[dict, int, int]:
    # client.invoke_model(modelId=model_id, body=json.dumps(body), ...)
    # Anthropic format: response_body["usage"]["input_tokens"]
    # Return (response_body, in_tok, out_tok)
```

Bedrock `ThrottlingException` / `TooManyRequestsException` → `BedrockError(..., 429)`.
Other `ClientError` → `BedrockError("BEDROCK_ERROR", ..., 502)`.

**Converse vs InvokeModel:** Route is determined by the URL path (client's choice).
Primary path is `/converse`. InvokeModel (`/invoke`) is the fallback for models
that don't support Converse (e.g., Stable Diffusion, Titan Embeddings).
Claude Sonnet 4.6 and Haiku 4.5 support Converse; use `/invoke` for image/embedding
models. Streaming (`invoke-with-response-stream`) is not supported in v1.

### Step 5: `lambda/proxy/handler.py`

```python
# Read TOKENS_TABLE, USAGE_TABLE, RATE_LIMIT_TABLE at import time (Lambda cache)
# Read BEDROCK_REGION, ALLOWED_MODELS_DEFAULT, PRICING_JSON at call time

def handler(event, context, *, _bedrock_client=None) -> dict:
    start_ms = time.monotonic() * 1000
    # Create DynamoDB resource (moto intercepts in tests)
    # Create bedrock client if not injected (injected in tests)
    log_ctx = {}
    try:
        token_id, secret = parse_bearer_token(event)
        log_ctx["token_id"] = token_id
        token_row = tokens_table.get_item(Key={"token_id": token_id}).get("Item")
        if not token_row or token_row.get("status") != "active":
            raise AuthError("INVALID_TOKEN", "Invalid or revoked token")
        if not verify_secret(secret, token_row["secret_hash"]):
            raise AuthError("INVALID_TOKEN", "Invalid token secret")
        log_ctx["owner"] = token_row.get("owner", "unknown")
        check_rate_limit(token_id, token_row, rate_limit_table)
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        usage = usage_table.get_item(Key={"token_id": token_id, "period": period}).get("Item", {})
        check_monthly_quota(token_row, usage)
        body_str = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body_str = base64.b64decode(body_str).decode()
        body = json.loads(body_str)
        model_id, route = parse_route(event)
        log_ctx["model_id"] = model_id
        check_input_cap(estimate_input_tokens(body, route), token_row)
        check_model_allowlist(model_id, token_row, allowed_models_default())
        max_out = int(token_row["limit_max_output_tokens"]) if "limit_max_output_tokens" in token_row else None
        body = apply_output_cap(body, route, max_out)
        if route == "converse":
            bedrock_resp, in_tok, out_tok = forward_converse(_bedrock_client, model_id, body)
        else:
            bedrock_resp, in_tok, out_tok = forward_invoke_model(_bedrock_client, model_id, body)
        usd = compute_cost(model_id, in_tok, out_tok)
        log_ctx.update(input_tokens=in_tok, output_tokens=out_tok, usd_micros=usd)
        try:
            write_usage(token_id, period, in_tok, out_tok, usd, usage_table)
        except Exception as exc:
            logger.error(json.dumps({**log_ctx, "event": "usage_write_failed", "error": str(exc)}))
        # log success + return 200
    except (AuthError, LimitError, BedrockError) as exc:
        # log rejection + return error
    except Exception:
        logger.exception("Unhandled error")
        return _error_response(500, "INTERNAL_ERROR", "Internal server error")
```

### Step 6: `lambda/proxy/requirements.txt`

Empty — `boto3` and `botocore` are provided by the Lambda Python 3.12 runtime.

### Step 7: `lambda/proxy/README.md`

Documents: entry point, env vars, request routing, error codes, packaging.

### Step 8: Tests (`tests/lambda/proxy/`)

Create `tests/lambda/__init__.py` and `tests/lambda/proxy/__init__.py` (empty)
so pytest can discover the test package.

**`conftest.py`** — module-level setup (runs before any test imports):
```python
import os, sys
# Add lambda/proxy to sys.path so flat imports work in tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../lambda/proxy"))
# Fake AWS credentials required by moto / boto3
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
# Table name env vars must be set before handler is imported
os.environ.setdefault("TOKENS_TABLE", "test-tokens")
os.environ.setdefault("USAGE_TABLE", "test-usage")
os.environ.setdefault("RATE_LIMIT_TABLE", "test-rate-limit")
```

- `aws_mock` fixture (`autouse=True`): `mock_aws()` context wrapping each test.
- `tables` fixture: creates the three DynamoDB tables via moto, yields
  `(tokens_table, usage_table, rate_limit_table)`.
- `test_token` fixture: generates `(token_id, bearer_token)`, inserts active row.
- `bedrock_stub` fixture: creates `bedrock-runtime` client + `Stubber`, yields both.
- `make_event()` helper: builds minimal APIGW HTTP API v2 event dict.

**`test_handler.py`** — 10 tests:

| Test | Setup | Expected |
|---|---|---|
| `test_happy_path` | valid token, Converse stub returns usage | 200, usage row incremented |
| `test_revoked_token` | token status="revoked" | 401 INVALID_TOKEN |
| `test_unknown_token` | no token row in DynamoDB | 401 INVALID_TOKEN |
| `test_over_rate_limit` | pre-fill rate_limit item at count=limit | 429 RATE_LIMIT_EXCEEDED |
| `test_over_monthly_requests` | usage.requests=limit in usage table | 429 MONTHLY_REQUEST_QUOTA_EXCEEDED |
| `test_over_monthly_budget` | usage.usd_micros=limit | 429 MONTHLY_BUDGET_EXCEEDED |
| `test_input_token_cap` | limit_max_input_tokens=1, long prompt | 413 INPUT_TOKEN_LIMIT_EXCEEDED |
| `test_model_not_allowed` | allowed_models={"other-model"} | 403 MODEL_NOT_ALLOWED |
| `test_bedrock_throttle` | Stubber raises ThrottlingException | 429 BEDROCK_THROTTLED |
| `test_usage_write_failure` | inject failing usage table, Bedrock succeeds | 200 (error logged) |

`test_usage_write_failure` injects a `_tables` tuple where the usage table's
`update_item` raises, while `get_item` (quota check) still returns normal data.

---

## Validation

After implementation:
- `make lint` (`ruff format --check` + `ruff check`) → 0
- `make test` (`pytest`) → all 10 new tests + 1 smoke test pass
- `make package` → `dist/proxy.zip` ≤ 5 MB (only stdlib + no external deps)
