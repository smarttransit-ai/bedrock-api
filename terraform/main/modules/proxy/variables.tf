variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
}

variable "image_uri" {
  description = "ECR image URI (repository:tag or digest) for the proxy Lambda container. Build and push before running terraform apply."
  type        = string
  default     = ""

  validation {
    condition     = var.image_uri != ""
    error_message = "image_uri must be set to a valid ECR image URI before applying. Use -target=module.proxy.aws_ecr_repository.proxy with image_uri=placeholder for the initial ECR bootstrap."
  }
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
  description = "Reserved concurrent executions cap for the proxy Lambda. Caps blast radius from a flood; account default is 1000. Function URLs have no built-in throttling — this is the primary global rate cap."
  type        = number
  default     = 50
}

variable "log_retention_days" {
  description = "CloudWatch log group retention in days."
  type        = number
  default     = 14
}

variable "allowed_models_default" {
  description = "Comma-separated default model allowlist passed to Lambda as ALLOWED_MODELS_DEFAULT. Empty string = no system-level restriction."
  type        = string
  default     = ""
}

variable "tokens_table_name" {
  description = "DynamoDB tokens table name (passed to Lambda env)."
  type        = string
}

variable "tokens_table_arn" {
  description = "DynamoDB tokens table ARN (used in IAM policy)."
  type        = string
}

variable "usage_table_name" {
  description = "DynamoDB usage table name (passed to Lambda env)."
  type        = string
}

variable "usage_table_arn" {
  description = "DynamoDB usage table ARN (used in IAM policy)."
  type        = string
}

variable "rate_limit_table_name" {
  description = "DynamoDB rate_limit table name (passed to Lambda env)."
  type        = string
}

variable "rate_limit_table_arn" {
  description = "DynamoDB rate_limit table ARN (used in IAM policy)."
  type        = string
}
