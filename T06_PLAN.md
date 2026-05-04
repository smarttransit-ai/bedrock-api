# T06 — Admin CLI (`cli/bedrock_api/`)

## Problem Summary

Implement an operator-facing Python CLI (`bedrock-api`) installed via `pyproject.toml`
console_scripts. The CLI talks directly to DynamoDB using the operator's boto3 credentials
and supports six subcommands: `issue`, `revoke`, `list`, `show`, `set-limit`, `usage`.

It must:
- Generate tokens in `bk_<32hex>.<64hex>` format matching `lambda/proxy/auth.py`
- Hash secrets identically to what `verify_secret()` in `auth.py` expects
- Read/write DynamoDB tables `{prefix}-tokens` and `{prefix}-usage` exactly as the schema
  in `terraform/main/modules/data/README.md` specifies
- Print the secret bearer token to stdout *exactly once* (on `issue`); all other output
  to stderr
- Exit 0 on all successful operations; exit 1 on errors

---

## Design Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| A | argparse vs click/typer | `argparse` (stdlib) | Minimises runtime deps; only boto3 needed beyond stdlib; requirement from task spec |
| B | Period helper location | Duplicate in `tokens.py` with cross-ref comment | CLI can't import from `lambda/proxy/` at runtime; computation is a one-liner; zero coupling |
| C | Budget input type | `--budget` accepts float USD → stored as `int(round(val * 1_000_000))` | Operator thinks in dollars; DynamoDB stores integer µUSD; `round()` avoids float truncation error |
| D | `revoke` idempotency | GetItem first; skip if already revoked; exit 0 | Clearest operator UX; avoids ConditionalCheckFailed on double-revoke |
| E | `set-limit --models` clearing | `--models ""` (empty string) → REMOVE the attribute | Never write an empty SS to DynamoDB (rejected); document `--models ""` in README |
| F | `list` default | `--status active` | Most common operator query; scan with no filter is more expensive |
| G | Decimal handling | `int(Decimal)` for numeric attrs returned by boto3 | boto3 returns DynamoDB numbers as `decimal.Decimal`; convert before arithmetic |
| H | stdout/stderr split | Bearer token → sys.stdout only; all metadata/errors → sys.stderr | Enables `bedrock-api issue ... > token.txt`; captured by `capsys` in tests |
| I | Table fixture in tests | Standalone `pytest.fixture` in `tests/cli/conftest.py` | Mirrors existing lambda test pattern; avoids duplication |
| J | `--models` on `issue` | Comma-separated string, parsed to set; absent = no `allowed_models` written | Never write empty SS; absent = unlimited per schema |
| K | Conditional PutItem | `ConditionExpression="attribute_not_exists(token_id)"` | Prevents accidental overwrite; should never collide with 128-bit entropy token_ids |
| L | `set-limit` with no flags | Exit 1 with "at least one limit flag required" | Empty UpdateExpression raises DynamoDB ValidationException; better to fail fast with clear message |
| M | `--models` clearing sentinel | Only `""` (empty string) triggers REMOVE | Drop `"none"` synonym — extra state with no benefit; document `--models ""` in README |
| N | `--models` whitespace | `.strip()` each element after comma-split | Prevents invalid model IDs with leading/trailing spaces |
| O | `--models` default on issue | Absent = unlimited (no `allowed_models` written) | Lambda's `ALLOWED_MODELS_DEFAULT` env var serves as system-level fallback; no CLI default needed |
| P | format_token_show secret_hash | Actively drop `secret_hash` from display | Never output credential data; drop explicitly in formatter, not by trusting caller |
| Q | Pagination | LastEvaluatedKey loop on both Scan and Query | Both operations page at 1 MB; must paginate both code paths |
| R | run_cli test helper | Fixture factory requesting capsys | capsys is a pytest fixture; run_cli must be a fixture itself that returns a callable |

---

## Files to Create

### 1. `cli/bedrock_api/__init__.py`
Empty file marking the package.

### 2. `cli/bedrock_api/tokens.py`

Token generation + hashing primitives. Must agree exactly with `lambda/proxy/auth.py`.

```python
import hashlib
import secrets
from datetime import datetime, timezone


def generate_token() -> tuple[str, str, str]:
    """Return (token_id, bearer_token, secret_hash).

    token_id     = "bk_" + secrets.token_hex(16)   # 35 chars, safe to log
    bearer_token = "<token_id>.<secret>"             # 99 chars, sent in Authorization header
    secret_hash  = "<32hex-salt>:<64hex-sha256>"     # stored in DynamoDB, NEVER the secret

    Hash algorithm mirrors lambda/proxy/auth.py:verify_secret().
    """
    token_id = f"bk_{secrets.token_hex(16)}"
    secret = secrets.token_hex(32)
    salt_hex = secrets.token_hex(16)
    digest = hashlib.sha256(bytes.fromhex(salt_hex) + bytes.fromhex(secret)).hexdigest()
    secret_hash = f"{salt_hex}:{digest}"
    bearer_token = f"{token_id}.{secret}"
    return token_id, bearer_token, secret_hash


def current_period() -> str:
    """Return the current UTC billing period as YYYY-MM.

    Mirrors the period computation in lambda/proxy/limits.py:write_usage().
    """
    return datetime.now(timezone.utc).strftime("%Y-%m")
```

### 3. `cli/bedrock_api/formatting.py`

Table rendering for `list` and `show`/`usage` output. All output to stderr (caller passes
`file=sys.stderr`).

```python
def format_table(rows: list[dict], columns: list[tuple[str, str, int]]) -> str:
    """Render rows as a fixed-width table.

    columns: list of (header, row_key, width)
    """
    ...

def truncate(s: str, n: int) -> str:
    # Use n-1 chars + ellipsis so total width = n
    return s[: n - 1] + "…" if len(s) > n else s.ljust(n)
```

Exported helpers:
- `format_token_list(items)` — renders list output
- `format_token_show(token_row, usage_row)` — renders show output
- `format_usage(usage_row)` — renders usage output

### 4. `cli/bedrock_api/cli.py`

argparse entrypoint + all subcommand handlers. Structure:

```
main()
  → build_parser() → ArgumentParser with 6 subparsers
  → get_clients(args) → (tokens_table, usage_table) boto3 resource handles
  → dispatch to cmd_issue / cmd_revoke / cmd_list / cmd_show / cmd_set_limit / cmd_usage
```

Global flags (added to parent parser, inherited by all subcommands):
- `--region` — default from `AWS_REGION` / `AWS_DEFAULT_REGION`, fallback `us-east-1`
- `--table-prefix` — default `bedrock-api`

#### Subcommand: `issue`

```
issue OWNER
  --budget FLOAT       USD/month budget (stored as int µUSD)
  --rps INT            requests/second cap
  --monthly-requests INT
  --max-input-tokens INT
  --max-output-tokens INT
  --models STR         comma-separated model IDs (absent = no restriction)
  --note STR
```

Logic:
1. `token_id, bearer_token, secret_hash = generate_token()`
2. Build item dict with `created_at = datetime.now(timezone.utc).isoformat()`
3. Add optional limit attrs only when provided (absent = unlimited per schema)
4. For `--models`: parse comma-separated, store as String Set; omit if not given
5. For `--budget`: `int(round(float(args.budget) * 1_000_000))`
6. `PutItem` with `ConditionExpression="attribute_not_exists(token_id)"`
7. Print `bearer_token` to **stdout**
8. Print metadata to **stderr**: token_id, owner, all set limits, created_at

#### Subcommand: `revoke`

```
revoke TOKEN_ID
```

Logic:
1. GetItem; if not found → stderr error, exit 1
2. If already revoked → stderr message, exit 0 (idempotent)
3. UpdateItem: `SET #status = :revoked, revoked_at = :now`
   with `ConditionExpression="attribute_exists(token_id)"`
4. Print confirmation to stderr

#### Subcommand: `list`

```
list
  --status active|revoked|all   (default: active)
  --owner OWNER
```

Logic:
- With `--owner`: `Query` on `owner-index` GSI, `KeyConditionExpression`
  `Key("owner").eq(owner)`, then filter by status client-side (or
  `FilterExpression` on status if not `all`)
- Without `--owner`: `Scan` with optional `FilterExpression` on status (if not `all`)
- Paginate through all items using `LastEvaluatedKey`
- Output via `format_token_list(items)` to stderr

#### Subcommand: `show`

```
show TOKEN_ID
```

Logic:
1. `GetItem` on tokens table
2. If not found → error, exit 1
3. `GetItem` on usage table for `current_period()`
4. Output all metadata (never secret_hash) + usage to stderr

#### Subcommand: `set-limit`

```
set-limit TOKEN_ID
  --budget FLOAT
  --rps INT
  --monthly-requests INT
  --max-input-tokens INT
  --max-output-tokens INT
  --models STR        "" or "none" = REMOVE the attribute
```

Logic:
1. Build SET expressions for non-None flags
2. `--models ""` or `--models none` → add `REMOVE allowed_models` expression
3. `--models M1,M2,...` → SET `allowed_models = :models` (SS)
4. Execute single `UpdateItem` with all expressions combined
   with `ConditionExpression="attribute_exists(token_id)"` (error if not found)
5. Print confirmation to stderr

Combining SET and REMOVE in one UpdateItem:
```python
UpdateExpression="SET #rps = :rps, ... REMOVE allowed_models"
```

#### Subcommand: `usage`

```
usage TOKEN_ID
  --period YYYY-MM    (default: current_period())
```

Logic:
1. `GetItem` on usage table with `(token_id, period)`
2. If not found → print zeros (no calls this period), exit 0
3. Output: requests, input_tokens, output_tokens, `usd_micros / 1_000_000:.4f`

### 5. `cli/README.md`

Examples for each subcommand, including piped-secret example:

```bash
# Issue a token and save bearer token to file
bedrock-api issue alice --budget 10.00 --rps 5 --monthly-requests 1000 \
  --max-input-tokens 4000 --max-output-tokens 4000 \
  --models us.anthropic.claude-sonnet-4-6-20250514-v1:0 > token.txt

# Revoke
bedrock-api revoke bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4

# List active tokens
bedrock-api list

# List all tokens for an owner
bedrock-api list --owner alice --status all

# Show token details
bedrock-api show bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4

# Update limits
bedrock-api set-limit bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 --budget 25.00 --rps 10

# Remove model restriction
bedrock-api set-limit bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 --models ""

# Query usage
bedrock-api usage bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
bedrock-api usage bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 --period 2026-04
```

---

## Tests

One test file per subcommand under `tests/cli/`. Use `moto[dynamodb]` for DynamoDB.

### `tests/cli/conftest.py`

Shared fixtures:
- `aws_credentials` fixture: set fake env vars (AWS_DEFAULT_REGION, AWS_ACCESS_KEY_ID, etc.)
- `mock_aws_env` autouse fixture: wraps each test in `mock_aws()`
- `tables` fixture: create `test-tokens` and `test-usage` tables with correct schemas
- `tokens_table` / `usage_table` convenience fixtures
- `make_token_item(token_id, bearer_token, secret_hash, **kwargs)` helper
- Constants: `TABLE_PREFIX = "test"`, `TOKENS_TABLE = "test-tokens"`, `USAGE_TABLE = "test-usage"`

Each test imports CLI functions by invoking `main()` with patched `sys.argv` and capturing
stdout/stderr with `capsys`. Alternatively, call subcommand handlers directly with a
parsed `Namespace`.

**Preferred approach**: call the subcommand handler functions directly (not via `main()`)
passing a mock `args` namespace and the DynamoDB table objects. This is cleaner and avoids
sys.argv patching.

Actually, the cleanest approach is to have a `run_cli(args_list)` helper in conftest that:
1. Patches `sys.argv`
2. Calls `main()`
3. Returns `(stdout, stderr, exit_code)` via `capsys` + `SystemExit` capture

But since we want to test the DynamoDB interaction directly with moto tables, we need the
CLI to accept table names via `--table-prefix`. Set `TABLE_PREFIX="test"` (which resolves
to `test-tokens` and `test-usage`).

### `tests/cli/test_issue.py`

Happy path:
- Call `bedrock-api issue alice --budget 10.00 --rps 5 --monthly-requests 1000 --max-input-tokens 4000 --max-output-tokens 4000 --models us.anthropic.claude-sonnet-4-6-20250514-v1:0`
- Assert stdout contains a line matching `bk_[0-9a-f]{32}\.[0-9a-f]{64}`
- Assert DynamoDB item exists with correct attributes
- Assert `secret_hash` starts with 32-hex `:` format
- Assert `status == "active"`
- Assert bearer token on stdout verifies against stored hash (call `verify_secret`)

Failure path:
- Issue same token twice (impossible with random IDs, so: manually insert item then call issue with a token_id collision is not testable)
- Instead test: `--models ""` on issue → no `allowed_models` attribute written (valid)
- Test: `--budget` only, no models → item written without `allowed_models`

### `tests/cli/test_revoke.py`

Happy path:
- Insert active token, revoke it, assert status=revoked and revoked_at set

Failure paths:
- Revoke already-revoked token → exit 0, no error
- Revoke non-existent token → exit 1

### `tests/cli/test_list.py`

Happy paths:
- List active tokens (default) → shows only active
- `--status revoked` → shows only revoked
- `--status all` → shows all
- `--owner alice` → shows only alice's tokens (via GSI Query)

Failure path:
- Empty table → output with 0 rows, exit 0

### `tests/cli/test_show.py`

Happy path:
- Show existing token with usage data → output includes token_id, owner, status, limits, usage

Failure path:
- Show non-existent token → exit 1

### `tests/cli/test_set_limit.py`

Happy paths:
- Set `--rps 10` → DynamoDB item updated
- Set `--budget 25.00` → `limit_monthly_usd_micros` = 25_000_000
- Set `--models "m1,m2"` → `allowed_models` = {"m1", "m2"}
- Set `--models ""` → `allowed_models` attribute removed

Failure path:
- Token not found → exit 1

### `tests/cli/test_usage.py`

Happy paths:
- Token with usage data → correct output
- Token with no usage for period → zeros output, exit 0
- `--period 2026-01` → queries specific period

---

## Validation

After implementation:

```bash
make lint test   # must exit 0
```

- `ruff format --check .` — no formatting issues
- `ruff check .` — no lint issues
- `pytest` — all tests pass
