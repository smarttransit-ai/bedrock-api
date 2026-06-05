# terraform/main — bedrock-api deployable stack

This is the main Terraform root module. It wires together:

- **`modules/data`** — DynamoDB tables (tokens, usage, rate_limit)
- **`modules/proxy`** — Lambda container function + ECR repo + IAM role + CloudWatch logs/alarms + Lambda Function URL
- **`modules/api`** — API Gateway HTTP API + stage (legacy; count-gated by `enable_http_api`, default `true`)

---

## Architecture

```
Clients
  │
  ├── Lambda Function URL  (RESPONSE_STREAM — supports streaming)
  │      └── Lambda (container image from ECR)
  │               ├── DynamoDB tokens table   (auth)
  │               ├── DynamoDB usage table    (quota + billing)
  │               ├── DynamoDB rate_limit     (per-second RPS)
  │               └── Bedrock (InvokeModel / InvokeModelWithResponseStream)
  │
  └── API Gateway HTTP API  (legacy; live while enable_http_api=true)
         └── same Lambda
```

The Lambda is served by the **AWS Lambda Web Adapter (LWA)** running uvicorn on port 8080. The Function URL is configured with `invoke_mode=RESPONSE_STREAM` so streaming responses pass through without buffering.

---

## Prerequisites

1. AWS credentials configured (e.g. `aws configure` or environment variables).
2. `terraform/bootstrap/` has been applied and its outputs are known.

---

## First-time setup

### 1. Apply the bootstrap module

The bootstrap module creates the S3 state bucket and DynamoDB lock table. It is applied once with local state, then never touched again.

```bash
cd terraform/bootstrap
terraform init
terraform apply
```

Note the printed outputs.

### 2. Fill in `backend.tf`

Run the following to get a ready-to-paste backend block:

```bash
terraform output -raw backend_block
```

Open `terraform/main/backend.tf` and replace the `PLACEHOLDER-*` values with the real bucket name, region, and lock table name from the bootstrap output.

### 3. Initialize the main stack

```bash
cd terraform/main
terraform init
```

### 4. Configure variables

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars as needed
```

### 5. Bootstrap ECR (one-time, no image needed)

```bash
terraform apply -target=module.proxy.aws_ecr_repository.proxy \
  -var='image_uri=placeholder'
```

### 6. Build and push the proxy image

```bash
ECR_URL=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin "$ECR_URL"
docker buildx build --platform linux/amd64 --provenance=false \
  -t "$ECR_URL:latest" lambda/proxy/
docker push "$ECR_URL:latest"
```

### 7. Apply

```bash
terraform apply -var="image_uri=$ECR_URL:latest"
```

The `function_url` output contains the streaming-capable Function URL. The `api_url` output points to the APIGW URL while `enable_http_api=true` (default), or to the Function URL once APIGW is retired.

---

## APIGW → Function URL cutover

Run the sequence **once per AWS profile** (primary `roged10` and ccc `roged10_ccc`):

| Step | Action |
|---|---|
| 1 | Apply with defaults (`enable_http_api=true`). Both APIGW and Function URL are live. No resources destroyed. |
| 2 | Validate Function URL: `curl $(terraform output -raw function_url)/health` |
| 3 | Update client configs to use `function_url`. APIGW remains live. |
| 4 | Retire APIGW: `terraform apply -var="image_uri=..." -var="enable_http_api=false"` |

New deployments with no existing APIGW may set `enable_http_api=false` from the start.

---

## Variables

| Name | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region |
| `name_prefix` | `bedrock-api` | Prefix for all resource names |
| `image_uri` | *(required)* | ECR image URI (build and push before apply) |
| `default_models` | `""` | Comma-separated system-wide model allowlist (`ALLOWED_MODELS_DEFAULT`). Empty = no system restriction. |
| `log_retention_days` | `14` | CloudWatch log retention in days |
| `lambda_memory_mb` | `512` | Lambda memory in MB |
| `lambda_timeout_s` | `60` | Lambda timeout in seconds |
| `lambda_reserved_concurrency` | `50` | Reserved concurrent executions cap. Primary global rate cap for Function URLs. |
| `enable_http_api` | `true` | Keep the legacy APIGW HTTP API. Set `false` to retire after clients cut over to Function URL. |

---

## Outputs

| Name | Description |
|---|---|
| `api_url` | APIGW URL when `enable_http_api=true`; Function URL when `false` |
| `function_url` | Lambda Function URL (always exposed — use this to validate streaming) |
| `ecr_repository_url` | ECR repository URL |
| `lambda_function_name` | Proxy Lambda function name |
| `tokens_table` | DynamoDB tokens table name |
| `usage_table` | DynamoDB usage table name |
| `rate_limit_table` | DynamoDB rate_limit table name |

---

## IAM notes

The proxy Lambda's execution role is least-privilege:

| Resource | Actions |
|---|---|
| `tokens` table | `dynamodb:GetItem` |
| `usage` table | `dynamodb:GetItem`, `dynamodb:UpdateItem` |
| `rate_limit` table | `dynamodb:UpdateItem` |
| Bedrock | `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream` on `*` |
| Lambda log group | `logs:CreateLogStream`, `logs:PutLogEvents` |

Bedrock IAM is intentionally permissive (`Resource = ["*"]`). The model allowlist is enforced inside the Lambda — first by per-token `allowed_models`, then by the optional system-wide `ALLOWED_MODELS_DEFAULT` env var. Per-token `--budget` caps the cost blast radius of any leaked token.

---

## Anonymous-attack-surface controls

- **Function URL auth.** The Function URL uses `authorization_type = "NONE"` — the Lambda handles bearer-token auth. All unauthenticated requests still pass through Lambda but are rejected at step 1 (parse token) before any DynamoDB read.
- **Reserved Lambda concurrency.** Caps parallel executions at `var.lambda_reserved_concurrency` (default 50). Bounds Lambda spend under any flood.
- **APIGW route narrowing (legacy).** While APIGW is live, only `POST /model/*` and `GET /usage` reach Lambda. All other paths return 404 at APIGW with no Lambda invocation.
- **APIGW stage throttling (legacy).** Steady-state 20 rps / 40 burst across all callers.
- **TLS-only.** Both Function URL and APIGW enforce TLS.

For per-IP rate limiting or geo restrictions, attach an AWS WAF web ACL to the APIGW stage or Function URL. Not in this stack.
