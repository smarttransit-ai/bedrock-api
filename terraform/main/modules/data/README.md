# `data` module — DynamoDB tables for bedrock-api

This Terraform module provisions the three DynamoDB tables that back all
authentication, limit enforcement, usage metering, and rate limiting for the
bedrock-api gateway.

| Table | Purpose |
|---|---|
| `<prefix>-tokens` | Token metadata, hashed secret, and per-token limits |
| `<prefix>-usage` | Monthly aggregate counters (requests, tokens, USD) |
| `<prefix>-rate-limit` | Per-second request counters with TTL auto-cleanup |

All tables use `PAY_PER_REQUEST` billing and AWS-managed SSE. `tokens` and
`usage` also have deletion protection and point-in-time recovery (PITR) enabled.
`rate_limit` is ephemeral state — no PITR, no deletion protection — so that
`terraform destroy` works without a manual pre-step.

---

## Terraform usage

```hcl
module "data" {
  source      = "./modules/data"
  name_prefix = "bedrock-api"   # optional; this is the default
}
```

Consume outputs in the parent root module or via `terraform output`:

```hcl
module.data.tokens_table_name
module.data.tokens_table_arn
module.data.tokens_owner_index_arn   # for IAM policy on the GSI
module.data.usage_table_name
module.data.usage_table_arn
module.data.rate_limit_table_name
module.data.rate_limit_table_arn
module.data.tables_json              # aggregate JSON string
```

---

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `name_prefix` | `string` | `"bedrock-api"` | Prefix for table names |

## Outputs

| Name | Description |
|---|---|
| `tokens_table_name` | Table name |
| `tokens_table_arn` | Table ARN |
| `tokens_owner_index_arn` | ARN of the `owner-index` GSI (for IAM `dynamodb:Query`) |
| `usage_table_name` | Table name |
| `usage_table_arn` | Table ARN |
| `rate_limit_table_name` | Table name |
| `rate_limit_table_arn` | Table ARN |
| `tables_json` | JSON object with `name`, `arn` (and `owner_index_arn`) for each table |

---

## Token format

Every bearer token has this structure:

```
bk_<32hex>.<64hex>
```

- **`token_id`** — `bk_` + 16 random bytes as 32 lowercase hex chars (35 chars
  total). This is the DynamoDB partition key. Safe to log and display in the
  CLI.
- **`secret`** — 32 random bytes as 64 lowercase hex chars.
- **Full bearer token** — `<token_id>.<secret>` = 99 chars. Sent by the client
  as `Authorization: Bearer <token>`.

**Parsing:** split on the first `.`. Everything before it is the `token_id`;
everything after is the `secret`. Example:

```python
token_id, secret = bearer_token.split(".", 1)
# token_id = "bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"
# secret   = "f7e8d9c0b1a2...64-hex-chars..."
```

---

## Secret hashing

The `secret` portion of the bearer token is never stored. Only a salted SHA-256
hash is stored in the `secret_hash` attribute.

**Format stored in DynamoDB:**
```
<32hex-salt>:<64hex-sha256>
```

**Generating (on token issue):**

```python
import hashlib, hmac, os

salt = os.urandom(16)
digest = hashlib.sha256(salt + bytes.fromhex(secret)).hexdigest()
secret_hash = f"{salt.hex()}:{digest}"
```

**Verifying (on every auth request):**

```python
import hashlib, hmac

salt_hex, stored_hash = row["secret_hash"].split(":", 1)
candidate = hashlib.sha256(bytes.fromhex(salt_hex) + bytes.fromhex(secret)).hexdigest()
if not hmac.compare_digest(candidate, stored_hash):
    return 401  # constant-time comparison prevents timing attacks
```

---

## "Unlimited" sentinel

**Absent attribute = unlimited.** When a limit attribute is absent from the
token row, that limit is not enforced.

- Do **not** write `0` to mean "unlimited" — `0` means "block everything."
  - `limit_rps = 0` → every request is rate-limited immediately.
  - `limit_monthly_usd_micros = 0` → zero budget, all requests rejected.
- `allowed_models`: absent means all models are permitted. **Never write an
  empty String Set** — DynamoDB rejects empty sets with `ValidationException`.
  When clearing a model restriction, use `REMOVE allowed_models` in an
  `UpdateItem` expression.

T04 and T06 must check `if "limit_rps" in token_row:` (etc.) before enforcing
each limit. The `limit_rps = 0` case also needs an explicit pre-check **before**
the DynamoDB conditional write (see rate-limit section below).

---

## Table: `tokens`

**Key schema:** `token_id` (PK, String). No sort key.

**GSI:** `owner-index` — PK = `owner`, SK = `created_at`, Projection = ALL.

### Attributes

| Attribute | DynamoDB Type | Required | Notes |
|---|---|---|---|
| `token_id` | S | ✓ | PK; `bk_<32hex>`; safe to log |
| `secret_hash` | S | ✓ | `<32hex-salt>:<64hex-sha256>` (99 chars) |
| `owner` | S | ✓ | GSI PK; human label e.g. `"alice"` |
| `created_at` | S | ✓ | ISO-8601 UTC; GSI SK; used for chronological ordering |
| `status` | S | ✓ | `"active"` or `"revoked"` |
| `revoked_at` | S | — | ISO-8601 UTC; written only when status → `"revoked"` |
| `note` | S | — | Free-text label set at issue time or via `set-limit` |
| `limit_rps` | N | — | Requests/second cap; **absent = unlimited**; `0` = block all |
| `limit_monthly_requests` | N | — | Monthly request quota; **absent = unlimited**; `0` = block all |
| `limit_monthly_usd_micros` | N | — | Monthly budget × 1 000 000 (integer µUSD); **absent = unlimited** |
| `limit_max_input_tokens` | N | — | Per-request input token cap; **absent = unlimited** |
| `limit_max_output_tokens` | N | — | Per-request output token cap; **absent = unlimited** |
| `allowed_models` | SS | — | String Set of Bedrock model/inference-profile IDs; **absent = all models permitted**; never write empty SS |

### Auth check (T04 step 1)

```python
row = tokens_table.get_item(Key={"token_id": token_id}).get("Item")
if not row or row.get("status") != "active":
    return 401
```

---

## Table: `usage`

**Key schema:** `token_id` (PK, String), `period` (SK, String).

No GSIs.

### Attributes

| Attribute | DynamoDB Type | Notes |
|---|---|---|
| `token_id` | S | PK; matches `tokens.token_id` |
| `period` | S | SK; UTC `YYYY-MM` e.g. `"2026-05"` |
| `requests` | N | Total successful Bedrock calls this period |
| `input_tokens` | N | Total input tokens this period |
| `output_tokens` | N | Total output tokens this period |
| `usd_micros` | N | Total USD × 1 000 000 (integer) |

> **Note:** `bedrock-api_TASKS.md` T03 calls this field `dollars_micro`. The
> canonical attribute name in this schema is **`usd_micros`**. T04 and T06
> must use `usd_micros`.

### Period computation

```python
from datetime import datetime, timezone
period = datetime.now(timezone.utc).strftime("%Y-%m")
```

### Quota check (T04 step 3)

```python
usage = usage_table.get_item(
    Key={"token_id": token_id, "period": period}
).get("Item", {})

if "limit_monthly_requests" in row:
    limit = row["limit_monthly_requests"]
    if limit == 0 or usage.get("requests", 0) >= limit:
        return 429

if "limit_monthly_usd_micros" in row:
    limit = row["limit_monthly_usd_micros"]
    if limit == 0 or usage.get("usd_micros", 0) >= limit:
        return 429
```

### Usage update (T04 step 6)

```python
usage_table.update_item(
    Key={"token_id": token_id, "period": period},
    UpdateExpression=(
        "ADD requests :r, input_tokens :i, output_tokens :o, usd_micros :u"
    ),
    ExpressionAttributeValues={
        ":r": 1,
        ":i": in_tokens,
        ":o": out_tokens,
        ":u": usd_micros,
    },
)
```

`ADD` is atomic at the item level — all four counters are incremented together.
`ADD` also auto-creates the item on the first call of a new month.

### µUSD precision

1 µUSD = $0.000001. Claude Sonnet 4.6 at $3/MTok input costs 3 µUSD per 1 000
input tokens — sufficient for accounting at any reasonable scale.

---

## Table: `rate_limit`

**Key schema:** `token_id` (PK, String), `window_second` (SK, Number).

**TTL attribute:** `ttl` (Number, Unix timestamp). DynamoDB auto-deletes items
after expiry (within ≤48 h; expired items are not returned by queries).

No PITR, no deletion protection — this is ephemeral operational state.

### Attributes

| Attribute | DynamoDB Type | Notes |
|---|---|---|
| `token_id` | S | PK |
| `window_second` | N | SK; Unix timestamp in whole seconds |
| `count` | N | Number of requests in this second |
| `ttl` | N | Unix timestamp for auto-expiry (`window_second + 10`) |

### Rate-limit enforcement (T04 step 2)

```python
import time
from botocore.exceptions import ClientError

if "limit_rps" in token_row:
    limit_rps = int(token_row["limit_rps"])
    # Explicit 0-check required: the attribute_not_exists guard below would
    # let the first request of each second through even when limit is 0.
    if limit_rps == 0:
        return 429
    now_sec = int(time.time())
    try:
        rate_limit_table.update_item(
            Key={"token_id": token_id, "window_second": now_sec},
            UpdateExpression=(
                "ADD #c :one SET #ttl = if_not_exists(#ttl, :exp)"
            ),
            # attribute_not_exists(#c) passes when the second-bucket item is
            # brand-new; subsequent requests in the same second use #c < :limit
            ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
            ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":one": 1,
                ":limit": limit_rps,
                ":exp": now_sec + 10,
            },
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return 429
        raise
```

DynamoDB evaluates the `ConditionExpression` against the **pre-update** value,
then applies the `ADD`. The `if_not_exists(#ttl, :exp)` SET is a no-op on
subsequent requests in the same second (TTL already set).

---

## Default model allowlist

The `allowed_models` String Set contains Bedrock model or cross-region
inference profile IDs. Default models for this deployment (configured by T05
as Terraform variables — do **not** hard-code here):

| Model | Cross-region inference profile ID |
|---|---|
| Claude Sonnet 4.6 | `us.anthropic.claude-sonnet-4-6-20250514-v1:0` |
| Claude Haiku 4.5 | `us.anthropic.claude-haiku-4-5-20250207-v1:0` |

T05 must expose these as a `default_allowed_models` variable (type
`list(string)`) so they can be overridden without code changes. T06 `issue`
defaults `--models` to this list when not specified.

---

## IAM permissions

### Lambda execution role (T05)

The proxy Lambda only needs to read tokens and read/write usage and rate-limit:

| Table / resource | Actions |
|---|---|
| `tokens_table_arn` | `dynamodb:GetItem` |
| `usage_table_arn` | `dynamodb:GetItem`, `dynamodb:UpdateItem` |
| `rate_limit_table_arn` | `dynamodb:UpdateItem` |

### Operator / CLI IAM principal (T06)

The CLI runs with the operator's own AWS credentials and talks to DynamoDB
directly (no Lambda in the path):

| Table / resource | Actions |
|---|---|
| `tokens_table_arn` | `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:Scan` |
| `tokens_owner_index_arn` | `dynamodb:Query` |
| `usage_table_arn` | `dynamodb:GetItem` |

---

## Read/write patterns summary

| Operation | Table | DynamoDB call |
|---|---|---|
| T04 — auth check | `tokens` | `GetItem(token_id)` |
| T04 — rate-limit enforce | `rate_limit` | `UpdateItem(token_id, window_second)` conditional ADD |
| T04 — quota check | `usage` | `GetItem(token_id, period)` |
| T04 — usage update | `usage` | `UpdateItem(token_id, period)` ADD |
| T06 issue | `tokens` | `PutItem` |
| T06 revoke | `tokens` | `UpdateItem` SET status, revoked_at |
| T06 list (no owner) | `tokens` | `Scan` with optional `FilterExpression` on `status` |
| T06 list --owner X | `tokens/owner-index` | `Query(owner=X)` with optional filter |
| T06 show TOKEN_ID | `tokens` + `usage` | `GetItem` on each (two sequential calls) |
| T06 set-limit | `tokens` | `UpdateItem` SET/REMOVE limit attributes |
| T06 usage TOKEN_ID | `usage` | `GetItem(token_id, period)` |
