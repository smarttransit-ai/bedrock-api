# T03 — DynamoDB Schema & Terraform Module

## Problem Summary

Design and provision the DynamoDB tables that back all authentication, limit
enforcement, and usage metering for the bedrock-api gateway. The schema must
support:

- **Token auth:** fast lookup by `token_id`, constant-time secret verification.
- **Five limit types** (GOALS.md): req/sec, monthly requests, monthly $ budget,
  per-request input/output token caps, per-token model allowlist.
- **Usage metering:** monthly aggregate counters (requests, input tokens, output
  tokens, USD) updated atomically after each successful Bedrock call.
- **Rate limiting:** per-second request counting with automatic expiry.
- **Admin operations:** list tokens by owner, show usage, revoke, set limits.

The schema is the shared contract between T04 (Lambda proxy), T06 (admin CLI),
and T05 (IAM policy). It must be fully documented so those tasks can be
implemented from this README alone.

---

## Design Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| a | Table design | Three tables: `tokens`, `usage`, `rate_limit` | Different access patterns, TTL, and write volume per entity type; single-table would mix hot auth reads with high-frequency rate-limit writes |
| b | Token format | `bk_<32hex>.<64hex>` (99 chars) | Prefixed for recognizability; dot separator splits public `token_id` from secret; hex is URL-safe, padding-free, copy-paste safe |
| c | Secret hashing | SHA-256 + 16-byte per-token salt; `hmac.compare_digest` for CT comparison | 256-bit random secret makes brute-force infeasible; SHA-256 is ~microseconds vs. 50–300 ms for bcrypt/argon2 — critical on every hot-path auth |
| d | Usage counter updates | Single `UpdateItem` with `ADD` on all four counters | DynamoDB `ADD` is atomic at the item level; no transaction needed when writing to one item; auto-initializes missing attributes to zero |
| e | Rate limiting | Separate `rate_limit` table, second-bucketed items + TTL | APIGW usage plans rejected (GOALS.md chose custom DynamoDB auth); token-bucket on tokens row creates write contention on auth reads; separate table cleanly isolates ephemeral rate state |
| f | Period rollover | `period` = UTC `YYYY-MM` string | New month → new item; no explicit rollover logic; historical periods fully preserved; Lambda computes `datetime.utcnow().strftime("%Y-%m")` |
| g | Status encoding | `status` string: `"active"` \| `"revoked"`; `revoked_at` ISO-8601; no TTL, no hard-delete | Preserves audit trail; explicit state change is safer than expiry-based revocation |
| h | GSIs | `owner-index` on `tokens` (PK=`owner`, SK=`created_at`, Projection=ALL) | Enables efficient `list` by owner; status filtering done client-side (owners have ≤ hundreds of tokens); no GSI needed on `usage` or `rate_limit` |
| i | "Unlimited" sentinel | Absent attribute = unlimited; `0` means "block all" | Absent avoids accidental `0`-budget tokens; T04/T06 check `if attr in row` before enforcing limit |
| j | Rate-limit condition | `attribute_not_exists(count) OR count < limit_rps` | DynamoDB evaluates missing attribute comparison as false; the `attribute_not_exists` guard lets first-of-second requests through instead of instant 429 |
| k | Rate-limit exception | `botocore.exceptions.ClientError` with `Code == "ConditionalCheckFailedException"` | The exception class is dynamically generated per-client, not statically importable |

### a) Table design

**Three tables: `tokens`, `usage`, `rate_limit`**

Single-table DynamoDB works well when entity types share access paths. Here,
the three types are accessed independently:

- `tokens`: read on every request (auth), write on issue/revoke/set-limit.
- `usage`: write after every successful Bedrock call (`ADD`), read on quota
  check.
- `rate_limit`: write + conditional check on every request, TTL-auto-cleaned.

Collapsing `rate_limit` into `tokens` would create hot-partition contention —
~1 write per request on the same item as auth reads, and TTL attribute
management in the token row. Collapsing `usage` into `tokens` would lose the
ability to keep full monthly history (one item per month per token).

The task description says "Two-table design proposed (validated in planning)"
and "Deliverables: both tables (tokens + usage + any rate-limit table the
design picks)." The design here adds the rate-limit table as the explicitly
requested third option.

### b) Token format

**`bk_<32hex>.<64hex>`** — total 99 characters.

```
bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4.f7e8d9c0b1a2f7e8d9c0b1a2f7e8d9c0b1a2f7e8d9c0b1a2f7e8d9c0b1a2f7e8
```

- **`token_id`** (DynamoDB PK, safe to log/show in UI): `bk_` + 16 random bytes
  as 32 lowercase hex chars = 35 chars total.
  - 128-bit entropy — collision-resistant across millions of tokens.
- **`secret`**: 32 random bytes as 64 lowercase hex chars.
  - 256-bit entropy — brute-force infeasible regardless of hash speed.
- **Bearer token** (sent by client in `Authorization: Bearer <token>`):
  `<token_id>.<secret>` = 99 chars.
  - Lambda splits on the first `.` to get `token_id` for DynamoDB lookup and
    `secret` for verification.
- `bk_` prefix: makes tokens recognizable in logs, configs, and code review;
  enables token scanning in leak detection.
- Hex encoding: no `+`, `/`, `=` padding chars; safe in HTTP headers and
  environment variables.

### c) Secret hashing

**SHA-256 with 16-byte per-token random salt; `hmac.compare_digest` for
constant-time comparison.**

Storage in `secret_hash` attribute: `<32hex-salt>:<64hex-sha256>` (99 chars).

Verification:
```python
salt_hex, hash_hex = row["secret_hash"].split(":", 1)
candidate = hashlib.sha256(bytes.fromhex(salt_hex) + bytes.fromhex(secret)).hexdigest()
hmac.compare_digest(candidate, hash_hex)  # constant-time
```

Rationale:
- **256-bit random secret → brute force infeasible.** A slow KDF (bcrypt,
  argon2id) adds 50–300 ms latency per request with zero additional security
  gain when the secret has 256 bits of entropy. SHA-256 runs in <1 µs.
- **Per-token salt** prevents rainbow tables and ensures two tokens with the
  same secret (impossible in practice with 256-bit randoms) produce different
  hashes.
- **`hmac.compare_digest`** provides constant-time string comparison, preventing
  timing attacks that could leak hash bytes.

### d) Usage counter updates

**Single `UpdateItem` with `ADD` expression on all four counters.**

```python
table.update_item(
    Key={"token_id": token_id, "period": period},
    UpdateExpression="ADD requests :r, input_tokens :i, output_tokens :o, usd_micros :u",
    ExpressionAttributeValues={":r": 1, ":i": in_tok, ":o": out_tok, ":u": usd_micro},
)
```

- DynamoDB `ADD` on a single item is **atomic** — all four attributes are
  updated together, no partial writes under concurrent calls.
- No transaction needed: `ADD` is the native DynamoDB primitive for concurrent
  counters on one item.
- Transactions (TransactWriteItems) add 2× round-trip cost and are only needed
  when atomically writing across multiple items.
- `ADD` auto-initializes missing numeric attributes to zero, so the first write
  of a new month creates the item.

### e) Rate limiting

**Separate `rate_limit` table with second-bucketed items + TTL.**

Schema: `PK=token_id (S)`, `SK=window_second (N)`, `count (N)`, `ttl (N)`.

Lambda algorithm:
```python
from botocore.exceptions import ClientError

now_sec = int(time.time())
# Only enforce if limit_rps attribute is present in the token row (absent = unlimited)
if "limit_rps" in token_row:
    limit_rps = token_row["limit_rps"]
    # 0 = block all; pre-check required because the attribute_not_exists
    # guard in the condition would allow 1 req through even with limit=0
    if limit_rps == 0:
        return 429
    try:
        rate_table.update_item(
            Key={"token_id": token_id, "window_second": now_sec},
            UpdateExpression="ADD #c :one SET #ttl = if_not_exists(#ttl, :exp)",
            # attribute_not_exists guard handles the first request in a new second
            # bucket — missing attribute comparisons evaluate to false in DynamoDB
            ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
            ExpressionAttributeNames={"#c": "count", "#ttl": "ttl"},
            ExpressionAttributeValues={":one": 1, ":limit": limit_rps, ":exp": now_sec + 10},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return 429
        raise
```

DynamoDB evaluates the condition atomically against the **pre-update** value,
then applies the `ADD`. The `attribute_not_exists(#c)` clause allows the first
request in any new second-bucket (item not yet existing) to proceed; subsequent
requests use the numeric `< :limit` comparison.

TTL = `window_second + 10` (10-second grace for clock skew); DynamoDB auto-
deletes expired items (may take up to 48 h, but they won't be checked after
their second has passed).

Rationale:
- APIGW usage plans rejected: GOALS.md explicitly chose custom DynamoDB auth
  (Answer: b). Using APIGW rate limiting would require APIGW API keys which
  bypass our custom auth layer.
- Token-bucket on tokens row: every request writes to the same item as auth
  reads, creating hot-partition risk and complicating TTL management in the
  token item.
- Second-bucket separate table: each second is a new item (no stale counter
  buildup), TTL handles garbage collection, no interference with token reads,
  independently scalable.

### f) Period rollover

**`period` = UTC `"YYYY-MM"` string, e.g. `"2026-05"`.**

- Lambda computes: `period = datetime.utcnow().strftime("%Y-%m")`.
- Each calendar month produces a new `usage` item under the same `token_id`.
- No explicit rollover code: the new month produces a naturally empty item.
- Historical usage is permanently preserved (old periods remain as immutable
  DynamoDB items).
- CLI `usage TOKEN_ID --period 2026-04` queries a specific period; default is
  current UTC month.
- Quota check: Lambda reads `usage.requests` and `usage.usd_micros` for the
  current period and compares to `tokens.limit_monthly_requests` and
  `tokens.limit_monthly_usd_micros`.

### g) Status field encoding

**`status` string: `"active"` or `"revoked"`; add `revoked_at` ISO-8601 on
revocation; no TTL on token items; no hard-delete.**

- Lambda auth: `if row.get("status") != "active": return 401`.
- Revocation: `UpdateItem` sets `status = "revoked"` and
  `revoked_at = utcnow().isoformat()`.
- Hard-delete omitted: preserves the audit trail (who had a token, when it was
  issued, what limits it had, when revoked).
- No TTL on token items: token lifecycle is explicit state, not time-based.
- Admin CLI `list --status active|revoked|all` filters post-query client-side
  or by `FilterExpression` on the GSI query.

### h) GSIs

**One GSI: `owner-index` on `tokens` (PK=`owner`, SK=`created_at`, Projection=ALL).**

- Enables `list [--owner X]` in O(tokens_per_owner) without a table scan.
- `created_at` as SK provides chronological ordering within each owner's tokens.
- Projection=ALL: CLI list view needs most attributes; avoids a second read.
- Status filtering is done client-side after the GSI query: any single owner
  will have at most hundreds of tokens — far below the point where server-side
  filtering would matter.
- No GSI on `usage`: always accessed by `(token_id, period)`.
- No GSI on `rate_limit`: always accessed by `(token_id, window_second)`.

---

## Files to Create

### 1. `T03_PLAN.md` — this file

### 2. `terraform/main/modules/data/versions.tf`

```hcl
terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
```

No `provider "aws" {}` block — child modules must not declare provider
configurations; doing so creates a separate unconfigured provider instance
instead of inheriting the parent's region/profile settings. `terraform validate`
works fine with only `required_providers`.

### 3. `terraform/main/modules/data/variables.tf`

One variable:

- `name_prefix` (string, default `"bedrock-api"`) — prefix for all three table
  names. Tables will be named `<prefix>-tokens`, `<prefix>-usage`,
  `<prefix>-rate-limit`.

### 4. `terraform/main/modules/data/main.tf`

Three `aws_dynamodb_table` resources:

**`tokens` table:**
- `name = "${var.name_prefix}-tokens"`
- `billing_mode = "PAY_PER_REQUEST"`
- `hash_key = "token_id"`
- `deletion_protection_enabled = true`
- Attributes declared: `token_id (S)`, `owner (S)`, `created_at (S)`
- GSI `owner-index`: PK=`owner`, SK=`created_at`, Projection=ALL
- `point_in_time_recovery { enabled = true }`
- `server_side_encryption { enabled = true }`

**`usage` table:**
- `name = "${var.name_prefix}-usage"`
- `billing_mode = "PAY_PER_REQUEST"`
- `hash_key = "token_id"`, `range_key = "period"`
- `deletion_protection_enabled = true`
- Attributes: `token_id (S)`, `period (S)`
- `point_in_time_recovery { enabled = true }`
- `server_side_encryption { enabled = true }`

**`rate_limit` table:**
- `name = "${var.name_prefix}-rate-limit"`
- `billing_mode = "PAY_PER_REQUEST"`
- `hash_key = "token_id"`, `range_key = "window_second"`
- No `deletion_protection_enabled` — ephemeral table; omitting protection
  allows `terraform destroy` without a manual pre-step
- Attributes: `token_id (S)`, `window_second (N)`
- `ttl { attribute_name = "ttl" / enabled = true }` (multi-line block — commas
  are not valid between block arguments in HCL2)
- No `point_in_time_recovery` — ephemeral second-bucket counters have no
  recovery scenario; enabling PITR costs ~$0.20/GB/month for zero benefit
- `server_side_encryption { enabled = true }`

### 5. `terraform/main/modules/data/outputs.tf`

Eight outputs:

| Output | Value |
|---|---|
| `tokens_table_name` | `aws_dynamodb_table.tokens.name` |
| `tokens_table_arn` | `aws_dynamodb_table.tokens.arn` |
| `tokens_owner_index_arn` | `"${aws_dynamodb_table.tokens.arn}/index/owner-index"` — T05 needs this for the Lambda IAM policy `dynamodb:Query` on the GSI |
| `usage_table_name` | `aws_dynamodb_table.usage.name` |
| `usage_table_arn` | `aws_dynamodb_table.usage.arn` |
| `rate_limit_table_name` | `aws_dynamodb_table.rate_limit.name` |
| `rate_limit_table_arn` | `aws_dynamodb_table.rate_limit.arn` |
| `tables_json` | `jsonencode({tokens: {name, arn}, usage: {name, arn}, rate_limit: {name, arn}})` — aggregate for CLI (`terraform output -raw tables_json`) |

### 6. `terraform/main/modules/data/README.md`

Sections (see "README Content" below).

---

## README Content

The README must be comprehensive enough that T04 (Lambda proxy) and T06 (admin
CLI) can be implemented from it without asking. It must cover:

1. **Purpose** — brief module purpose.
2. **Terraform usage** — how to call from the parent root module.
3. **Inputs/Outputs** — variable and output table.
4. **Token format** — exact format of bearer tokens and how Lambda splits them.
5. **Secret hashing** — salt:hash format and verification algorithm
   (including `hmac.compare_digest` for constant-time comparison).
6. **"Unlimited" sentinel** — absent attribute = unlimited. `0` as a numeric
   limit has specific semantics: `limit_rps = 0` means "block all requests."
   T04 must check `if "limit_rps" in token_row:` before enforcing each limit.
   For `limit_rps`, add an **explicit pre-check before the DynamoDB call**:
   `if token_row.get("limit_rps") == 0: return 429` — because the
   `attribute_not_exists(#c) OR #c < :limit` condition would allow the first
   request through even with `:limit = 0`. The same pattern applies to
   monthly-requests and budget: check `if value == 0: reject` before reading
   the usage table. `allowed_models`: **absent = all models allowed**; never
   write an empty String Set (DynamoDB rejects it).
7. **Table: `tokens`** — full canonical attribute dictionary:

   | Attribute | DynamoDB Type | Required | Notes |
   |---|---|---|---|
   | `token_id` | S | ✓ | PK; `bk_<32hex>`; safe to log |
   | `secret_hash` | S | ✓ | `<32hex-salt>:<64hex-sha256>` |
   | `owner` | S | ✓ | GSI PK; human name (e.g. `"alice"`) |
   | `created_at` | S | ✓ | ISO-8601 UTC; GSI SK |
   | `status` | S | ✓ | `"active"` or `"revoked"` |
   | `revoked_at` | S | — | ISO-8601 UTC; set when revoked |
   | `note` | S | — | Free-text label |
   | `limit_rps` | N | — | Req/sec cap; absent = unlimited |
   | `limit_monthly_requests` | N | — | Monthly request quota; absent = unlimited |
   | `limit_monthly_usd_micros` | N | — | Monthly budget × 1e6 (integer); absent = unlimited |
   | `limit_max_input_tokens` | N | — | Per-request input token cap; absent = unlimited |
   | `limit_max_output_tokens` | N | — | Per-request output token cap; absent = unlimited |
   | `allowed_models` | SS | — | String Set of model/inference-profile IDs; **absent = all models permitted**. Never write an empty SS (DynamoDB rejects it). T06 must omit this attribute on `issue` when no `--models` given, and use `REMOVE allowed_models` on `set-limit` when clearing the restriction. |

8. **Table: `usage`** — full canonical attribute dictionary and write pattern:

   | Attribute | DynamoDB Type | Notes |
   |---|---|---|
   | `token_id` | S | PK |
   | `period` | S | SK; `YYYY-MM` UTC (e.g. `"2026-05"`) |
   | `requests` | N | Total successful Bedrock calls this period |
   | `input_tokens` | N | Total input tokens this period |
   | `output_tokens` | N | Total output tokens this period |
   | `usd_micros` | N | Total USD × 1 000 000 (integer) |

   Write pattern (`ADD` expression); period format; quota-check algorithm
   (read `usage` row → compare `requests` vs `limit_monthly_requests` and
   `usd_micros` vs `limit_monthly_usd_micros`).

   *Note: `bedrock-api_TASKS.md` T03 description uses `dollars_micro` — the
   canonical attribute name in this schema is **`usd_micros`**. T04 and T06
   must use `usd_micros`.*
9. **Table: `rate_limit`** — all attributes; algorithm (conditional
   `attribute_not_exists OR count < limit` + `ClientError` with
   `ConditionalCheckFailedException` = 429); TTL handling.
10. **IAM permissions** — two separate policies; the CLI talks directly to AWS
    using the operator's own credentials (GOALS.md) and never goes through Lambda.

    *Lambda execution role (T05):*
    - `tokens`: `GetItem`
    - `usage`: `GetItem`, `UpdateItem`
    - `rate_limit`: `UpdateItem`

    *Operator/CLI IAM principal (T06 operator):*
    - `tokens`: `GetItem`, `PutItem`, `UpdateItem`, `Scan`
    - `tokens` GSI (`tokens_owner_index_arn`): `Query`
    - `usage`: `GetItem`
11. **Default model allowlist** — documents the cross-region inference profile
    IDs for Claude Sonnet 4.6 and Haiku 4.5 that T05 should wire as variables.
12. **Read/write patterns summary** — quick-reference table of all DynamoDB
    calls T04 and T06 make, covering both reads and writes:
    - T04 auth: `GetItem` on `tokens` by `token_id`
    - T04 rate-limit: `UpdateItem` on `rate_limit` with conditional ADD
    - T04 quota check: `GetItem` on `usage` by `(token_id, period)`
    - T04 usage update: `UpdateItem` on `usage` with ADD
    - T06 issue: `PutItem` on `tokens`
    - T06 revoke: `UpdateItem` on `tokens` (set `status`, `revoked_at`)
    - T06 list (no owner): `Scan` on `tokens`
    - T06 list (with owner): `Query` on `tokens/owner-index`
    - T06 show: `GetItem` on `tokens` + `GetItem` on `usage` (current period)
    - T06 set-limit: `UpdateItem` on `tokens`
    - T06 usage: `GetItem` on `usage` by `(token_id, period)`

---

## Validation

After implementation:

- `terraform fmt -check -recursive` → exits 0 (run from repo root).
- `terraform -chdir=terraform/main/modules/data init -backend=false` → exits 0.
- `terraform -chdir=terraform/main/modules/data validate` → exits 0.
