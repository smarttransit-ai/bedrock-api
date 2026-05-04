# terraform/main — bedrock-api deployable stack

This is the main Terraform root module. It wires together:

- **`modules/data`** — DynamoDB tables (tokens, usage, rate_limit)
- **`modules/proxy`** — Lambda function + IAM role + CloudWatch log group
- **`modules/api`** — API Gateway HTTP API + stage + optional custom domain

---

## Prerequisites

1. AWS credentials configured (e.g. `aws configure` or environment variables).
2. `terraform/bootstrap/` has been applied and its outputs are known.

---

## First-time setup

### 1. Apply the bootstrap module

The bootstrap module creates the S3 state bucket and DynamoDB lock table. It
is applied once with local state, then never touched again.

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

Open `terraform/main/backend.tf` and replace the `PLACEHOLDER-*` values with
the real bucket name, region, and lock table name from the bootstrap output.

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

### 5. Plan and apply

```bash
terraform plan
terraform apply
```

The `api_url` output contains the endpoint clients should send requests to.

---

## Variables

| Name | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region |
| `name_prefix` | `bedrock-api` | Prefix for all resource names |
| `default_models` | `""` | Comma-separated system-wide model allowlist passed to the Lambda as `ALLOWED_MODELS_DEFAULT`. Empty = no system-level restriction; per-token `allowed_models` still applies. IAM grants `bedrock:InvokeModel` on `*`, so the Lambda is the model gate. |
| `log_retention_days` | `14` | CloudWatch log retention in days |
| `lambda_memory_mb` | `512` | Lambda memory in MB |
| `lambda_timeout_s` | `60` | Lambda timeout in seconds |
| `lambda_reserved_concurrency` | `50` | Reserved concurrent executions cap on the proxy Lambda. Caps blast radius from any flood; account default is 1000. |
| `throttling_rate_limit` | `20` | APIGW stage steady-state requests per second across all callers. |
| `throttling_burst_limit` | `40` | APIGW stage burst requests per second across all callers. |
| `domain_name` | `""` | Custom domain (leave empty to use default APIGW URL) |
| `hosted_zone_id` | `""` | Route 53 hosted zone ID (required when `domain_name` is set) |

---

## Outputs

| Name | Description |
|---|---|
| `api_url` | Public API endpoint URL |
| `lambda_function_name` | Proxy Lambda function name |
| `tokens_table` | DynamoDB tokens table name |
| `usage_table` | DynamoDB usage table name |
| `rate_limit_table` | DynamoDB rate_limit table name |

---

## Custom domain

Set `domain_name` and `hosted_zone_id` in `terraform.tfvars`:

```hcl
domain_name    = "api.example.com"
hosted_zone_id = "Z1234567890ABC"
```

Terraform will:
1. Create a regional ACM certificate in the same region as the API.
2. Add the DNS validation CNAME to the Route 53 hosted zone.
3. Wait for certificate validation.
4. Create an APIGW custom domain name and Route 53 A record.

---

## IAM notes

The proxy Lambda's execution role is least-privilege:

| Resource | Actions |
|---|---|
| `tokens` table | `dynamodb:GetItem` |
| `usage` table | `dynamodb:GetItem`, `dynamodb:UpdateItem` |
| `rate_limit` table | `dynamodb:UpdateItem` |
| Bedrock | `bedrock:InvokeModel` on `*` |
| Lambda log group | `logs:CreateLogStream`, `logs:PutLogEvents` |

Bedrock IAM is intentionally permissive (`Resource = ["*"]`). The model
allowlist is enforced inside the Lambda — first by the per-token
`allowed_models` attribute, then by the optional system-wide
`ALLOWED_MODELS_DEFAULT` env var (`var.default_models`). Per-token
`--budget` caps the cost blast radius of any leaked token.

---

## Anonymous-attack-surface controls

The deployment is hardened against unauthenticated probing/scanning:

- **Route narrowing.** APIGW only accepts `POST /model/{proxy+}`. Every
  other path or method (`GET /`, `GET /.env`, `OPTIONS /healthcheck`,
  `DELETE *`, etc.) returns 404 directly from API Gateway — no Lambda
  invocation, no DynamoDB read, no log entry, no cost. To add a new route
  later (e.g. a `/healthz`), add a second `aws_apigatewayv2_route`.
- **Stage throttling.** APIGW caps total requests at
  `var.throttling_rate_limit` rps with `var.throttling_burst_limit` burst,
  applied across all callers. Requests over the cap are rejected at APIGW
  with HTTP 429 and never reach Lambda. Default 20/40 — sized for lab-scale
  interactive traffic; raise if batch jobs need it.
- **Reserved Lambda concurrency.** The proxy Lambda is capped at
  `var.lambda_reserved_concurrency` parallel executions (default 50). Even
  if a flood gets past the throttle (e.g. burst), Lambda spend is bounded.
- **TLS-only.** API Gateway HTTP API doesn't accept plain HTTP at all —
  port 80 is closed at the edge.

For per-IP rate limiting or geo restrictions, attach an AWS WAF web ACL to
the API stage. Not in this stack — adds cost (~$5/mo + per-request
charges) and most labs don't need it.
