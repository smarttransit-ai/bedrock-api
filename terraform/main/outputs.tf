output "api_url" {
  description = "Public API endpoint URL. When enable_http_api=true (default), points to the legacy APIGW URL for backward compatibility. When enable_http_api=false, points to the Function URL. Use function_url to always get the Function URL regardless of flag."
  value       = var.enable_http_api ? one(module.api[*].api_url) : module.proxy.function_url
}

output "function_url" {
  description = "Lambda Function URL (RESPONSE_STREAM). Always exposed so operators can validate and cut over during the APIGW coexistence window."
  value       = module.proxy.function_url
}

output "ecr_repository_url" {
  description = "ECR repository URL for the proxy container image."
  value       = module.proxy.ecr_repository_url
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
