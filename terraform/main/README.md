# terraform/main — bedrock-api deployable stack

This is the main Terraform root module. It wires together:

- **`modules/data`** — DynamoDB tables (tokens, usage, rate_limit)
- **`modules/proxy`** — Lambda container function + ECR repo + IAM role + CloudWatch logs/alarms + API Gateway REST API (REGIONAL, streaming)

---

## Architecture

```
Clients
  │
  └── API Gateway REST API (REGIONAL)
         └── Lambda alias "live"  (provisioned concurrency = 1)
                  └── Lambda (container image from ECR)
                           ├── DynamoDB tokens table   (auth)
                           ├── DynamoDB usage table    (quota + billing)
                           ├── DynamoDB rate_limit     (per-second RPS)
                           └── Bedrock (InvokeModel / InvokeModelWithResponseStream)
```

The Lambda is served by the **AWS Lambda Web Adapter (LWA)** running uvicorn on port 8080. LWA is configured with `AWS_LWA_INVOKE_MODE=RESPONSE_STREAM`.

The REST API uses a REGIONAL endpoint (5-minute idle timeout — required for streaming) with:
- `response_transfer_mode = "STREAM"` on the integration
- `timeout_milliseconds = 900000` (15 minutes — covers longest streaming calls)
- Lambda alias "live" with `provisioned_concurrent_executions = 1` to eliminate cold-start 500s
- Stage throttle: 20 rps steady / 40 burst

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

### 2. Backend state is per-deployment

`backend.tf` is a **partial** config — `bucket`, `key`, and `dynamodb_table` come from a per-deployment `-backend-config` file so each account keeps its own isolated state:

- `primary.s3.tfbackend` (committed) → account 343084147688 (profile `roged10`)
- `ccc.s3.tfbackend` → account 066949051849 (profile `roged10_ccc`). Fill in the ccc-owned state bucket + lock table; if they don't exist yet, run `terraform/bootstrap` in the ccc account first.

Switching only `AWS_PROFILE` does **not** switch state — you must re-init with the right `-backend-config`.

### 3. Initialize the main stack

```bash
cd terraform/main
AWS_PROFILE=roged10 AWS_REGION=us-east-1 \
  terraform init -reconfigure -backend-config=primary.s3.tfbackend
```

### 4. Configure variables

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars as needed
```

### 5. Bootstrap ECR (one-time, no image needed)

```bash
AWS_PROFILE=roged10 AWS_REGION=us-east-1 \
  terraform apply -target=module.proxy.aws_ecr_repository.proxy \
    -var='image_uri=placeholder'
```

### 6. Build and push the proxy image

```bash
ECR_URL=$(AWS_PROFILE=roged10 AWS_REGION=us-east-1 terraform output -raw ecr_repository_url)
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin "$ECR_URL"
docker buildx build --platform linux/amd64 --provenance=false \
  -t "$ECR_URL:latest" lambda/proxy/
docker push "$ECR_URL:latest"
```

### 7. Apply

```bash
AWS_PROFILE=roged10 AWS_REGION=us-east-1 \
  terraform apply -var="image_uri=$ECR_URL:latest"
```

The `api_url` output contains the REST API invoke URL (REGIONAL endpoint, stage `v1`).

Run for both deployments:
- Primary: `AWS_PROFILE=roged10` → account 343084147688
- CCC: `AWS_PROFILE=roged10_ccc` → account 066949051849

---

## Migration from the old HTTP API v2 deployment

The **ccc** deployment is still on the pre-streaming architecture (HTTP API v2, ZIP Lambda). Removing the `module "api"` block from this config means a normal apply already plans the destruction of the old resources as orphans — `apigatewayv2` api/routes/integration/stage, its access-log group, and the old Lambda permission. No `-target` surgery or `removed`/`moved` blocks are needed. The proxy Lambda also changes from a ZIP package to a container image, so it is **replaced** in place.

Always review the plan before applying:

```bash
AWS_PROFILE=roged10_ccc AWS_REGION=us-east-1 \
  terraform plan  -var="image_uri=$ECR_URL:latest"   # review the orphan destroys + Lambda replace
AWS_PROFILE=roged10_ccc AWS_REGION=us-east-1 \
  terraform apply -var="image_uri=$ECR_URL:latest"
```

Before the first ccc apply, also decide `apigw_cloudwatch_role_already_set` (see Variables) — check with `aws apigateway get-account --profile roged10_ccc`.

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
| `lambda_timeout_s` | `900` | Lambda timeout in seconds. Must be >= the 900s API Gateway integration timeout, or streaming responses get killed mid-stream. |
| `lambda_reserved_concurrency` | `50` | Reserved concurrent executions cap |
| `provisioned_concurrency` | `1` | Provisioned concurrent executions on alias "live". Set to 0 to disable (not recommended). |
| `throttling_rate_limit` | `20` | API Gateway stage steady-state rps |
| `throttling_burst_limit` | `40` | API Gateway stage burst rps |
| `apigw_cloudwatch_role_already_set` | `false` | Skip creating the account-level API Gateway CloudWatch role if already configured. `aws_api_gateway_account` is account-global. Check: `aws apigateway get-account`. |
| `litellm_source_url` | litellm raw URL | Upstream price map the admin `POST /admin/pricing/refresh` endpoint pulls from. |
| `pricing_cache_ttl_s` | `60` | Per-instance TTL (seconds) for the live pricing catalog read from S3. |

---

## Outputs

| Name | Description |
|---|---|
| `api_url` | REST API invoke URL (REGIONAL, stage v1) |
| `ecr_repository_url` | ECR repository URL |
| `pricing_bucket` | S3 bucket holding the live pricing catalog |
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
| pricing bucket (`${name_prefix}-pricing-<account>`) | `s3:GetObject`, `s3:PutObject` on `/*`; `s3:ListBucket` on the bucket (so a missing object reads as NoSuchKey, not AccessDenied) |

Bedrock IAM is intentionally permissive (`Resource = ["*"]`). The model allowlist is enforced inside the Lambda — first by per-token `allowed_models`, then by the optional system-wide `ALLOWED_MODELS_DEFAULT` env var. Per-token `--budget` caps the cost blast radius of any leaked token.

---

## Security controls

- **Bearer-token auth.** The app handles authentication — all unauthenticated requests are rejected before any DynamoDB read.
- **Reserved Lambda concurrency.** Caps parallel executions at `var.lambda_reserved_concurrency` (default 50). Bounds Lambda spend under any flood.
- **REST API route narrowing.** `ANY /{proxy+}` and `ANY /` routes forward all paths to Lambda. Unrecognized paths return 404 from the FastAPI app.
- **REST API stage throttling.** Steady-state 20 rps / 40 burst across all callers.
- **TLS-only.** REGIONAL REST API enforces TLS.

For per-IP rate limiting or geo restrictions, attach an AWS WAF web ACL to the REST API stage.
