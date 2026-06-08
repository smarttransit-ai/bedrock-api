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

### 2. Fill in `backend.tf`

Run the following to get a ready-to-paste backend block:

```bash
terraform output -raw backend_block
```

Open `terraform/main/backend.tf` and replace the `PLACEHOLDER-*` values with the real bucket name, region, and lock table name from the bootstrap output.

### 3. Initialize the main stack

```bash
cd terraform/main
AWS_PROFILE=roged10 AWS_REGION=us-east-1 terraform init
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

## State migration (for already-applied deployments)

When migrating from the old Lambda Function URL + HTTP API v2 architecture, destroy the old resources explicitly before running the full apply (the `destroy -target` approach actually removes AWS resources, unlike `state rm` which only orphans them):

```bash
# Destroy old Function URL
terraform destroy -target='module.proxy.aws_lambda_function_url.proxy' \
  -var="image_uri=$ECR_URL:latest"

# Destroy old HTTP API (cascades to routes, integrations, stage)
terraform destroy -target='module.api[0].aws_apigatewayv2_api.main' \
  -var="image_uri=$ECR_URL:latest"

# Destroy remaining resources if still in state
terraform destroy -target='module.api[0].aws_cloudwatch_log_group.api_access' \
  -var="image_uri=$ECR_URL:latest"
terraform destroy -target='module.api[0].aws_lambda_permission.apigw' \
  -var="image_uri=$ECR_URL:latest"

# Then apply the full new config
terraform apply -var="image_uri=$ECR_URL:latest"
```

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
| `lambda_reserved_concurrency` | `50` | Reserved concurrent executions cap |

### Proxy module variables

| Name | Default | Description |
|---|---|---|
| `provisioned_concurrency` | `1` | Provisioned concurrent executions on alias "live". Set to 0 to disable (not recommended). |
| `throttling_rate_limit` | `20` | Stage-level steady-state rps |
| `throttling_burst_limit` | `40` | Stage-level burst rps |
| `apigw_cloudwatch_role_already_set` | `false` | Skip creating account-level CloudWatch role if already configured |

---

## Outputs

| Name | Description |
|---|---|
| `api_url` | REST API invoke URL (REGIONAL, stage v1) |
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

## Security controls

- **Bearer-token auth.** The app handles authentication — all unauthenticated requests are rejected before any DynamoDB read.
- **Reserved Lambda concurrency.** Caps parallel executions at `var.lambda_reserved_concurrency` (default 50). Bounds Lambda spend under any flood.
- **REST API route narrowing.** `ANY /{proxy+}` and `ANY /` routes forward all paths to Lambda. Unrecognized paths return 404 from the FastAPI app.
- **REST API stage throttling.** Steady-state 20 rps / 40 burst across all callers.
- **TLS-only.** REGIONAL REST API enforces TLS.

For per-IP rate limiting or geo restrictions, attach an AWS WAF web ACL to the REST API stage.
