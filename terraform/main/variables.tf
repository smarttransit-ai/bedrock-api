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
