# bedrock-api CLI

Admin CLI for managing API tokens and querying usage against the bedrock-api gateway.
Talks directly to DynamoDB using your operator AWS credentials — no Lambda in the path.

## Installation

```bash
pip install -e ".[dev]"   # from repo root
```

The `bedrock-api` command is registered via `pyproject.toml` console_scripts.

## Global flags

| Flag | Default | Description |
|---|---|---|
| `--region` | `AWS_REGION` / `AWS_DEFAULT_REGION` / `us-east-1` | AWS region |
| `--table-prefix` | `bedrock-api` | DynamoDB table name prefix |

## Subcommands

### `issue` — create a new token

```bash
# Issue with all limits; pipe bearer token to file (stdout only)
bedrock-api issue alice \
  --budget 10.00 \
  --rps 5 \
  --monthly-requests 1000 \
  --max-input-tokens 4000 \
  --max-output-tokens 4000 \
  --models us.anthropic.claude-sonnet-4-6,us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --note "grad student" \
  > token.txt

# Issue with no limits (unlimited)
bedrock-api issue bob
```

The **bearer token is printed to stdout exactly once** and never shown again.
All metadata (token_id, owner, limits, created_at) goes to stderr.
This means you can safely redirect stdout to a file while still seeing metadata in the terminal.

### `revoke` — revoke a token

```bash
bedrock-api revoke bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
```

Idempotent: revoking an already-revoked token exits 0 with a message.

### `list` — list tokens

```bash
# Active tokens (default)
bedrock-api list

# All tokens for an owner
bedrock-api list --owner alice --status all

# Revoked tokens only
bedrock-api list --status revoked
```

Output is a wide table: TOKEN_ID (truncated), OWNER, STATUS, CREATED_AT, REQUESTS, USD.

### `show` — token details + current usage

```bash
bedrock-api show bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4
```

Outputs all metadata (never the secret hash) and current-month usage aggregates.

### `set-limit` — update limits

```bash
# Update budget and RPS together (single DynamoDB UpdateItem)
bedrock-api set-limit bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 --budget 25.00 --rps 10

# Restrict models
bedrock-api set-limit bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 \
  --models us.anthropic.claude-haiku-4-5-20251001-v1:0

# Remove model restriction (pass empty string to REMOVE the attribute)
bedrock-api set-limit bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 --models ""
```

At least one flag is required. Multiple flags are applied atomically in a single UpdateItem.

### `usage` — query usage counters

```bash
# Current month
bedrock-api usage bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4

# Historical period
bedrock-api usage bk_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4 --period 2026-04
```

Outputs: requests, input_tokens, output_tokens, usd (4 decimal places).

## Token format

```
bk_<32hex>.<64hex>
```

- `token_id` (`bk_` + 32 hex): the DynamoDB key, safe to log and display
- `secret` (64 hex): never stored; only its salted SHA-256 hash is written to DynamoDB
- Full bearer token (100 chars): sent as `Authorization: Bearer <token>`

## IAM requirements

The operator's IAM principal needs:

| Resource | Actions |
|---|---|
| `{prefix}-tokens` table | `GetItem`, `PutItem`, `UpdateItem`, `Scan` |
| `{prefix}-tokens/index/owner-index` GSI | `Query` |
| `{prefix}-usage` table | `GetItem` |
