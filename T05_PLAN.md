# T05 — Terraform: API Gateway + Lambda + IAM Wiring

## Problem Summary

Wire together the deployable `terraform/main/` stack: package the T04 Lambda,
create an IAM execution role with least-privilege access to the three DynamoDB
tables and Bedrock models, expose it through an HTTP API Gateway v2, and provide
a `$default` stage with throttling and access logging. Optionally attach a
custom domain with a Route 53 DNS record and regional ACM cert.

The data layer (`terraform/main/modules/data/`) already exists from T03. This
task must consume its outputs unchanged and must not modify it.

---

## Design Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| a | Module split | `modules/proxy/` (Lambda + IAM), `modules/api/` (APIGW), `modules/data/` (existing) | Clear separation of compute, network, and storage concerns; each module independently testable |
| b | Lambda source path threading | Root module passes `${path.module}/../../lambda/proxy` as `lambda_source_dir` variable into `proxy` module | Keeps cross-module relative paths DRY; avoids fragile `../../../` chains inside sub-modules |
| c | Lambda ZIP location | `archive_file` writes to `${path.module}/dist/proxy.zip` inside proxy module | Self-contained to the module; does not conflict with `make package` (repo-root `dist/`) |
| d | Bedrock IAM ARN form | `arn:aws:bedrock:${region}::inference-profile/${model_id}` | Matches system-defined cross-region inference profile ARN form (no account ID, double-colon). The API call is made in `${region}`, so the IAM check fires in that region. If runtime `AccessDeniedException` occurs, fall back to `arn:aws:bedrock:*::foundation-model/*` as documented in GOALS.md / task spec |
| d2 | Bedrock IAM actions | `bedrock:InvokeModel` only | `bedrock:Converse` and `bedrock:ConverseStream` are **not valid IAM actions**; all Bedrock invocation APIs (`converse()`, `invoke_model()`) are gated by `bedrock:InvokeModel`. Streaming is unsupported in v1 |
| e | IAM scope per table | tokens=`GetItem` only; usage=`GetItem`+`UpdateItem`; rate_limit=`UpdateItem` only | Exact minimum from data/README.md IAM table — Lambda never writes tokens or reads rate_limit items |
| f | CloudWatch Logs IAM | `logs:CreateLogStream`+`PutLogEvents` on `${log_group.arn}` and `${log_group.arn}:*` | Scope to the managed log group; no wildcard `*`; two ARN forms cover group-level and stream-level permissions |
| g | Lambda log group | Create `aws_cloudwatch_log_group` before Lambda via `depends_on`; name `/aws/lambda/${name_prefix}-proxy` | Terraform manages retention (14 days default); Lambda reuses existing group matching the default naming convention |
| h | APIGW stage throttling | `default_route_settings` block on `$default` stage, 100 rps / 200 burst | Defense in depth above per-token Lambda-side rate limits; modest values suitable for a lab proxy |
| i | APIGW access log format | JSON with requestId, requestTime, httpMethod, routeKey, status, responseLength, integrationErrorMessage | Enough for debugging + alerting; avoids logging request body / auth headers |
| j | Custom domain conditional | `count = var.domain_name != "" ? 1 : 0` on ACM cert + R53 records + APIGW domain name + API mapping | Simplest Terraform idiom; TF plan with empty domain_name still validates fully |
| k | ACM cert region | Same region as the API Gateway | HTTP API requires a regional ACM cert (not us-east-1 global); one provider config handles both |
| l | Model ID defaults | Use `us.anthropic.claude-sonnet-4-6-20250514-v1:0` and `us.anthropic.claude-haiku-4-5-20250207-v1:0` | These IDs are already established in T04 pricing.py and data/README.md — consistent with existing code |
| m | `default_models` var type | `string` (CSV) passed directly as `ALLOWED_MODELS_DEFAULT` env var | Lambda reads CSV string; Terraform doesn't parse it; default `""` = no restriction |
| n | Backend placeholder | `backend.tf` uses `# PLACEHOLDER` comments with instructions | Values only available after `terraform/bootstrap/` is applied; operator pastes them once |

---

## Step-by-Step Implementation

### Step 1 — `terraform/main/versions.tf`

```hcl
terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.region
}
```

### Step 2 — `terraform/main/backend.tf`

```hcl
# ---------------------------------------------------------------------------
# Remote state backend (S3 + DynamoDB locking).
#
# Fill in the values from `terraform/bootstrap/` outputs BEFORE running
# `terraform init` for the first time:
#
#   cd terraform/bootstrap
#   terraform output -raw backend_block
#
# Paste the printed block here, replacing the PLACEHOLDER values below.
# ---------------------------------------------------------------------------
terraform {
  backend "s3" {
    bucket         = "PLACEHOLDER-state-bucket-name"
    key            = "bedrock-api/terraform.tfstate"
    region         = "PLACEHOLDER-region"
    dynamodb_table = "PLACEHOLDER-lock-table-name"
    encrypt        = true
  }
}
```

### Step 3 — `terraform/main/variables.tf`

Variables with sensible defaults. Model IDs match T04 pricing map.

```hcl
variable "region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
  default     = "bedrock-api"
}

variable "model_allowlist" {
  description = "List of Bedrock cross-region inference profile IDs that the Lambda may invoke."
  type        = list(string)
  default = [
    "us.anthropic.claude-sonnet-4-6-20250514-v1:0",
    "us.anthropic.claude-haiku-4-5-20250207-v1:0",
  ]
}

variable "default_models" {
  description = "Comma-separated default model allowlist passed to Lambda as ALLOWED_MODELS_DEFAULT. Empty string = no system-level restriction."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 14
}

variable "domain_name" {
  description = "Custom domain for the API (e.g. api.example.com). Leave empty to use the default APIGW endpoint."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone ID for domain_name DNS validation and A record. Required when domain_name is set."
  type        = string
  default     = ""
}

variable "lambda_memory_mb" {
  description = "Lambda memory in MB."
  type        = number
  default     = 512
}

variable "lambda_timeout_s" {
  description = "Lambda timeout in seconds."
  type        = number
  default     = 60
}
```

### Step 4 — `terraform/main/outputs.tf`

```hcl
output "api_url" {
  description = "Public API endpoint URL."
  value       = module.api.api_url
}

output "lambda_function_name" {
  description = "Name of the proxy Lambda function."
  value       = module.proxy.function_name
}

output "tokens_table" {
  description = "DynamoDB tokens table name."
  value       = module.data.tokens_table_name
}

output "usage_table" {
  description = "DynamoDB usage table name."
  value       = module.data.usage_table_name
}

output "rate_limit_table" {
  description = "DynamoDB rate_limit table name."
  value       = module.data.rate_limit_table_name
}
```

### Step 5 — `terraform/main/main.tf`

Root module composition wiring data → proxy → api.

```hcl
module "data" {
  source      = "./modules/data"
  name_prefix = var.name_prefix
}

module "proxy" {
  source = "./modules/proxy"

  name_prefix            = var.name_prefix
  region                 = var.region
  lambda_source_dir      = "${path.module}/../../lambda/proxy"
  lambda_memory_mb       = var.lambda_memory_mb
  lambda_timeout_s       = var.lambda_timeout_s
  log_retention_days     = var.log_retention_days
  model_allowlist        = var.model_allowlist
  allowed_models_default = var.default_models

  tokens_table_name     = module.data.tokens_table_name
  tokens_table_arn      = module.data.tokens_table_arn
  usage_table_name      = module.data.usage_table_name
  usage_table_arn       = module.data.usage_table_arn
  rate_limit_table_name = module.data.rate_limit_table_name
  rate_limit_table_arn  = module.data.rate_limit_table_arn
}

module "api" {
  source = "./modules/api"

  name_prefix          = var.name_prefix
  lambda_invoke_arn    = module.proxy.invoke_arn
  lambda_function_name = module.proxy.function_name
  log_retention_days   = var.log_retention_days
  domain_name          = var.domain_name
  hosted_zone_id       = var.hosted_zone_id
}
```

### Step 6 — `terraform/main/terraform.tfvars.example`

```hcl
region             = "us-east-1"
name_prefix        = "bedrock-api"
log_retention_days = 14
lambda_memory_mb   = 512
lambda_timeout_s   = 60
default_models     = ""

# Uncomment to restrict to specific models:
# model_allowlist = [
#   "us.anthropic.claude-sonnet-4-6-20250514-v1:0",
#   "us.anthropic.claude-haiku-4-5-20250207-v1:0",
# ]

# Uncomment for custom domain (also requires hosted_zone_id):
# domain_name    = "api.example.com"
# hosted_zone_id = "Z1234567890ABC"
```

### Step 7a — `terraform/main/modules/proxy/dist/.gitkeep`

Create an empty `.gitkeep` so the `dist/` directory is tracked by git and
exists on fresh checkouts. `archive_file` writes `proxy.zip` here; it does
not create missing parent directories.

Also add `terraform/main/modules/proxy/dist/*.zip` to the repo-root `.gitignore`
if not already present — the ZIP is a build artifact, not source.

### Step 7 — `terraform/main/modules/proxy/versions.tf`

```hcl
terraform {
  required_version = ">= 1.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}
```

### Step 8 — `terraform/main/modules/proxy/variables.tf`

One attribute per line — HCL does not support semicolons.

```hcl
variable "name_prefix" {
  type = string
}

variable "region" {
  type = string
}

variable "lambda_source_dir" {
  description = "Absolute path to the lambda/proxy/ source directory."
  type        = string
}

variable "lambda_memory_mb" {
  type    = number
  default = 512
}

variable "lambda_timeout_s" {
  type    = number
  default = 60
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "model_allowlist" {
  type = list(string)
}

variable "allowed_models_default" {
  type    = string
  default = ""
}

variable "tokens_table_name" {
  type = string
}

variable "tokens_table_arn" {
  type = string
}

variable "usage_table_name" {
  type = string
}

variable "usage_table_arn" {
  type = string
}

variable "rate_limit_table_name" {
  type = string
}

variable "rate_limit_table_arn" {
  type = string
}
```

### Step 9 — `terraform/main/modules/proxy/main.tf`

```hcl
# --- Lambda source archive ---------------------------------------------------
# archive_file excludes accepts exact relative paths only — no glob patterns.
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = var.lambda_source_dir
  output_path = "${path.module}/dist/proxy.zip"
  excludes    = [".gitkeep", "__pycache__"]
}

# --- CloudWatch log group ----------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}-proxy"
  retention_in_days = var.log_retention_days
}

# --- IAM execution role ------------------------------------------------------
resource "aws_iam_role" "lambda" {
  name = "${var.name_prefix}-proxy-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.name_prefix}-proxy-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDBTokens"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [var.tokens_table_arn]
      },
      {
        Sid      = "DynamoDBUsage"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = [var.usage_table_arn]
      },
      {
        Sid      = "DynamoDBRateLimit"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = [var.rate_limit_table_arn]
      },
      {
        Sid    = "Bedrock"
        Effect = "Allow"
        # bedrock:InvokeModel covers converse(), invoke_model(), and all
        # other Bedrock invocation APIs. bedrock:Converse is not a valid
        # IAM action. Streaming is unsupported in v1.
        Action   = ["bedrock:InvokeModel"]
        Resource = [for m in var.model_allowlist : "arn:aws:bedrock:${var.region}::inference-profile/${m}"]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          aws_cloudwatch_log_group.lambda.arn,
          "${aws_cloudwatch_log_group.lambda.arn}:*",
        ]
      },
    ]
  })
}

# --- Lambda function ---------------------------------------------------------
resource "aws_lambda_function" "proxy" {
  function_name    = "${var.name_prefix}-proxy"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_s

  environment {
    variables = {
      TOKENS_TABLE           = var.tokens_table_name
      USAGE_TABLE            = var.usage_table_name
      RATE_LIMIT_TABLE       = var.rate_limit_table_name
      BEDROCK_REGION         = var.region
      ALLOWED_MODELS_DEFAULT = var.allowed_models_default
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]
}
```

### Step 10 — `terraform/main/modules/proxy/outputs.tf`

```hcl
output "function_name"  { value = aws_lambda_function.proxy.function_name }
output "function_arn"   { value = aws_lambda_function.proxy.arn }
output "invoke_arn"     { value = aws_lambda_function.proxy.invoke_arn }
output "role_arn"       { value = aws_iam_role.lambda.arn }
```

### Step 11 — `terraform/main/modules/api/versions.tf`

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

### Step 12 — `terraform/main/modules/api/variables.tf`

One attribute per line — HCL does not support semicolons.

```hcl
variable "name_prefix" {
  type = string
}

variable "lambda_invoke_arn" {
  type = string
}

variable "lambda_function_name" {
  type = string
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "throttling_rate_limit" {
  type    = number
  default = 100
}

variable "throttling_burst_limit" {
  type    = number
  default = 200
}

variable "domain_name" {
  type    = string
  default = ""
}

variable "hosted_zone_id" {
  type    = string
  default = ""
}
```

### Step 13 — `terraform/main/modules/api/main.tf`

```hcl
# --- Access log group --------------------------------------------------------
resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${var.name_prefix}-http"
  retention_in_days = var.log_retention_days
}

# --- HTTP API ----------------------------------------------------------------
resource "aws_apigatewayv2_api" "main" {
  name          = "${var.name_prefix}-http"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.main.id
  integration_type       = "AWS_PROXY"
  integration_uri        = var.lambda_invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "proxy" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "ANY /{proxy+}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_rate_limit  = var.throttling_rate_limit
    throttling_burst_limit = var.throttling_burst_limit
    logging_level          = "OFF"
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId               = "$context.requestId"
      requestTime             = "$context.requestTime"
      httpMethod              = "$context.httpMethod"
      routeKey                = "$context.routeKey"
      status                  = "$context.status"
      protocol                = "$context.protocol"
      responseLength          = "$context.responseLength"
      integrationStatus       = "$context.integration.integrationStatus"
      integrationErrorMessage = "$context.integrationErrorMessage"
    })
  }
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

# --- Custom domain (only when var.domain_name is set) -----------------------
resource "aws_acm_certificate" "domain" {
  count             = var.domain_name != "" ? 1 : 0
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = var.domain_name != "" ? {
    for dvo in aws_acm_certificate.domain[0].domain_validation_options :
    dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  zone_id = var.hosted_zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.record]
  ttl     = 60

  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "domain" {
  count                   = var.domain_name != "" ? 1 : 0
  certificate_arn         = aws_acm_certificate.domain[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

resource "aws_apigatewayv2_domain_name" "custom" {
  count       = var.domain_name != "" ? 1 : 0
  domain_name = var.domain_name

  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.domain[0].certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

resource "aws_route53_record" "api" {
  count   = var.domain_name != "" ? 1 : 0
  zone_id = var.hosted_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_apigatewayv2_domain_name.custom[0].domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.custom[0].domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_apigatewayv2_api_mapping" "custom" {
  count       = var.domain_name != "" ? 1 : 0
  api_id      = aws_apigatewayv2_api.main.id
  domain_name = aws_apigatewayv2_domain_name.custom[0].id
  stage       = aws_apigatewayv2_stage.default.id
}
```

### Step 14 — `terraform/main/modules/api/outputs.tf`

```hcl
output "api_endpoint" {
  description = "Default APIGW endpoint URL."
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "api_url" {
  description = "API URL — custom domain if set, else the default APIGW endpoint."
  value       = var.domain_name != "" ? "https://${var.domain_name}" : aws_apigatewayv2_api.main.api_endpoint
}

output "api_id" {
  value = aws_apigatewayv2_api.main.id
}

output "execution_arn" {
  value = aws_apigatewayv2_api.main.execution_arn
}
```

### Step 15 — `terraform/main/README.md`

Document:
- Prerequisites (bootstrap applied, backend.tf filled in)
- Step-by-step apply instructions
- Variable reference
- Output reference
- Custom domain setup

### Validation

After all files are written:

1. `terraform fmt -check -recursive terraform/main` — exits 0 (run `terraform fmt -recursive terraform/main` to fix)
2. Install terraform >= 1.7 if not present
3. `terraform -chdir=terraform/main init -backend=false`
4. `terraform -chdir=terraform/main validate`
