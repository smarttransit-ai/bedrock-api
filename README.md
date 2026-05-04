# bedrock-api

A Terraform-deployed AWS API Gateway → Lambda proxy that fronts AWS Bedrock
with per-token authentication, rate limits, monthly request quotas, monthly
USD budgets, input/output token caps, and per-model allowlists.

The API Gateway only accepts `POST /model/{proxy+}` — any other path or
method is rejected at the edge before reaching the Lambda. Stage throttling
is 20 rps / 40 burst and the Lambda is capped at 50 reserved concurrent
executions. Tunable via [`terraform/main/`](./terraform/main/) variables.

---

## Prerequisites

| Tool | Min version | Notes |
|---|---|---|
| AWS CLI + credentials | any | `aws configure` or env vars; needs IAM access for the resources below |
| Terraform | 1.5+ | |
| Python | 3.12+ | for the admin CLI |
| pip | any | to install the CLI |

IAM permissions needed by the operator account:

- S3 and DynamoDB (bootstrap): create bucket, create table
- IAM, Lambda, API Gateway, CloudWatch Logs, Route 53, ACM (main stack)
- DynamoDB read/write on the tokens and usage tables (CLI)

---

## Deploy

### 1. Bootstrap the Terraform state backend

Applied once with local state. Creates the S3 state bucket and DynamoDB
lock table.

```bash
cd terraform/bootstrap
terraform init
terraform apply
```

Note the printed outputs (bucket name, region, table name).

### 2. Wire the remote backend for the main stack

```bash
# From terraform/bootstrap — generates a ready-to-paste backend block
terraform output -raw backend_block > ../main/backend.tf
```

Commit `terraform/main/backend.tf`.

### 3. Configure variables

```bash
cd terraform/main
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set region, name_prefix, and optionally
# domain_name + hosted_zone_id for a custom domain.
```

### 4. Apply the main stack

```bash
terraform init
terraform plan
terraform apply
```

Note the `api_url` output — this is the endpoint clients call.

---

## Install the admin CLI

The `bedrock-api` admin CLI ships as a Python package in this repo. Pick
whichever installer you prefer.

### uv (recommended — isolates the CLI in its own environment)

```bash
# Install once, available on PATH everywhere:
uv tool install git+https://github.com/smarttransit-ai/bedrock-api.git

# …or run a one-off without installing:
uvx --from git+https://github.com/smarttransit-ai/bedrock-api.git bedrock-api --help
```

### pip

```bash
pip install git+https://github.com/smarttransit-ai/bedrock-api.git
```

### From a local checkout (for development on the CLI itself)

```bash
git clone https://github.com/smarttransit-ai/bedrock-api.git
cd bedrock-api
pip install -e ".[dev]"   # adds pytest, ruff, moto, httpx
```

Pin to a specific commit or tag with `...bedrock-api.git@<commit-or-tag>`.
Verify in any terminal:

```bash
bedrock-api --help
```

Global flags (can also be set via `AWS_REGION` / `AWS_DEFAULT_REGION`):

```
--region         AWS region (default: us-east-1)
--table-prefix   DynamoDB table prefix (default: bedrock-api)
```

---

## Issue your first token

```bash
# Override the $200/month default budget and restrict to two models
bedrock-api issue alice \
  --budget 10.00 \
  --models us.anthropic.claude-sonnet-4-6,us.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --note "grad student"
```

`--budget` defaults to **$200/month** if you don't pass it. Other limits
(`--rps`, `--monthly-requests`, `--max-input-tokens`, `--max-output-tokens`,
`--models`) are off by default — absent = unlimited. To issue a token with
no monthly USD cap at all, you must remove the `limit_monthly_usd_micros`
attribute manually with `aws dynamodb update-item ... REMOVE` (intentionally
not exposed via CLI).

The **bearer token is printed to stdout exactly once and never shown again.**
All other metadata (token_id, owner, limits) goes to stderr. To capture the
secret for handoff:

```bash
bedrock-api issue alice --budget 10.00 > token.txt
# stdout (token.txt) contains only the bearer token
# stderr shows the metadata in your terminal
```

Hand `token.txt` (or the raw token string) to the lab member. They will use
it as:

```
Authorization: Bearer bk_<32hex>.<64hex>
```

---

## First API call

See **[docs/clients.md](./docs/clients.md)** for copy-pasteable examples
using `curl`, Python `httpx`, and `boto3`.

Quick curl test:

```bash
API_URL="<api_url from terraform output>"
TOKEN="$(cat token.txt)"
MODEL="us.anthropic.claude-sonnet-4-6"

curl -s -X POST "${API_URL}/model/${MODEL}/converse" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":[{"type":"text","text":"Hello!"}]}]}' \
  | jq .output.message.content[0].text
```

---

## Monitoring and usage

```bash
# Show all metadata + current-month counters for one token
bedrock-api show bk_<token_id>

# List all active tokens with current-month requests and spend
bedrock-api list

# List all tokens for a specific owner
bedrock-api list --owner alice --status all

# Query usage for a past period
bedrock-api usage bk_<token_id> --period 2026-04
```

---

## Update limits

```bash
# Raise budget, tighten RPS
bedrock-api set-limit bk_<token_id> --budget 25.00 --rps 5

# Restrict to Haiku only
bedrock-api set-limit bk_<token_id> \
  --models us.anthropic.claude-haiku-4-5-20251001-v1:0

# Remove model restriction (allow all)
bedrock-api set-limit bk_<token_id> --models ""
```

---

## Revoke a token

```bash
bedrock-api revoke bk_<token_id>
```

Revocation is immediate. Any request using the revoked token returns 401.
Revoking an already-revoked token is a no-op (exits 0).

---

## End-to-end smoke test

Against a deployed stack:

```bash
export E2E=1
export BEDROCK_API_URL="<api_url from terraform output>"
export BEDROCK_API_REGION="us-east-1"
export BEDROCK_API_TABLE_PREFIX="bedrock-api"   # optional, this is the default

make e2e
```

The test issues a token, calls the API, checks usage counters, revokes the
token, and verifies the revoked token is rejected.

---

## Teardown

```bash
cd terraform/main
terraform destroy

# Then destroy the state backend (removes S3 bucket and lock table)
# See terraform/bootstrap/README.md for the required pre-steps.
```
