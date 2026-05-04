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

variable "domain_name" {
  description = "Custom domain for the API (e.g. api.example.com). Leave empty to use the default APIGW endpoint."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone ID. Required when domain_name is set; used for ACM DNS validation and the API A record."
  type        = string
  default     = ""
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
