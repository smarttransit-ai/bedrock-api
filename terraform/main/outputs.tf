output "api_url" {
  description = "REST API invoke URL (REGIONAL, stage v1). Use this as the API base URL."
  value       = module.proxy.api_url
}

output "ecr_repository_url" {
  description = "ECR repository URL for the proxy container image."
  value       = module.proxy.ecr_repository_url
}

output "pricing_bucket" {
  description = "S3 bucket holding the live pricing catalog."
  value       = module.proxy.pricing_bucket
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
