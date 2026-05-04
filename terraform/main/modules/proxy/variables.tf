variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
}

variable "region" {
  description = "AWS region (used in Bedrock IAM resource ARNs)."
  type        = string
}

variable "lambda_source_dir" {
  description = "Absolute path to the lambda/proxy/ source directory."
  type        = string
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

variable "log_retention_days" {
  description = "CloudWatch log group retention in days."
  type        = number
  default     = 14
}

variable "model_allowlist" {
  description = "Bedrock cross-region inference profile IDs that the Lambda may invoke. Used to scope the Bedrock IAM policy."
  type        = list(string)
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
