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
| `model_allowlist` | Sonnet 4.6 + Haiku 4.5 | Bedrock cross-region inference profile IDs |
| `default_models` | `""` | Comma-separated default model allowlist for Lambda (`ALLOWED_MODELS_DEFAULT`) |
| `log_retention_days` | `14` | CloudWatch log retention in days |
| `lambda_memory_mb` | `512` | Lambda memory in MB |
| `lambda_timeout_s` | `60` | Lambda timeout in seconds |
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
| Bedrock inference profiles | `bedrock:InvokeModel` |
| Lambda log group | `logs:CreateLogStream`, `logs:PutLogEvents` |

Bedrock IAM uses `arn:aws:bedrock:REGION::inference-profile/MODEL_ID` for
system-defined cross-region inference profiles. If `AccessDeniedException`
occurs at runtime with cross-region profiles, change the resource to
`arn:aws:bedrock:*::foundation-model/*` in `modules/proxy/main.tf`.
