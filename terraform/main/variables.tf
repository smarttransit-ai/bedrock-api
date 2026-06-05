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

variable "image_uri" {
  description = "ECR image URI for the proxy Lambda (e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/bedrock-api-proxy:latest). Build and push before running terraform apply."
  type        = string
}

variable "default_models" {
  description = "Comma-separated system-wide model allowlist (passed to Lambda as ALLOWED_MODELS_DEFAULT). Empty string = no system-level restriction; per-token allowed_models still applies. IAM grants the Lambda bedrock:InvokeModel on * — the Lambda is the authoritative model gate."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 14
}

variable "lambda_memory_mb" {
  description = "Lambda function memory in MB."
  type        = number
  default     = 512
}

variable "lambda_timeout_s" {
  description = "Lambda function timeout in seconds."
  type        = number
  default     = 60
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions cap for the proxy Lambda. Function URLs have no built-in throttling — this is the primary global rate cap. Enforce limit_rps on all production tokens."
  type        = number
  default     = 50
}

variable "enable_http_api" {
  description = "Keep the legacy API Gateway HTTP API alongside the Function URL. Set false to retire it AFTER clients are cut over to the Function URL. Default true so that a fresh apply is additive and the live APIGW is never destroyed without operator opt-in."
  type        = bool
  default     = true
}
