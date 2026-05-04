variable "name_prefix" {
  description = "Prefix for all resource names."
  type        = string
}

variable "lambda_invoke_arn" {
  description = "Lambda invoke ARN for the APIGW AWS_PROXY integration."
  type        = string
}

variable "lambda_function_name" {
  description = "Lambda function name (used in the resource-based policy)."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch access log retention in days."
  type        = number
  default     = 14
}

variable "throttling_rate_limit" {
  description = "Stage-level default steady-state requests per second."
  type        = number
  default     = 100
}

variable "throttling_burst_limit" {
  description = "Stage-level default maximum requests per second (burst)."
  type        = number
  default     = 200
}

variable "domain_name" {
  description = "Custom domain name (e.g. api.example.com). Leave empty to use the default APIGW endpoint."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone ID. Required when domain_name is set."
  type        = string
  default     = ""
}
