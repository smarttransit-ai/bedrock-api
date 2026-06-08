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
  description = "Lambda function timeout in seconds. Must be >= the API Gateway integration timeout (900s) to prevent Lambda from terminating streaming responses before the gateway gives up."
  type        = number
  default     = 900
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions cap for the proxy Lambda. Account default is 1000; 50 is generous for lab-scale interactive use."
  type        = number
  default     = 50
}

variable "provisioned_concurrency" {
  description = "Provisioned concurrent executions on the 'live' alias. Eliminates cold-start 500s. Set to 0 to disable (not recommended for production)."
  type        = number
  default     = 1
}

variable "throttling_rate_limit" {
  description = "API Gateway stage steady-state requests per second. Sized for lab-scale interactive traffic; raise for batch jobs."
  type        = number
  default     = 20
}

variable "throttling_burst_limit" {
  description = "API Gateway stage maximum requests per second (burst)."
  type        = number
  default     = 40
}

variable "apigw_cloudwatch_role_already_set" {
  description = "Set true if the account already has a CloudWatch role configured for API Gateway. aws_api_gateway_account is an account-global singleton; setting it twice across stacks conflicts. When true, skips creating the IAM role and aws_api_gateway_account resources (the account must already have a CW role, or stage access logging will fail). Check with: aws apigateway get-account."
  type        = bool
  default     = false
}

variable "litellm_source_url" {
  description = "Upstream litellm price map URL the admin pricing-refresh endpoint pulls from."
  type        = string
  default     = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
}

variable "pricing_cache_ttl_s" {
  description = "Per-instance TTL (seconds) for the live pricing catalog read from S3."
  type        = number
  default     = 60
}
