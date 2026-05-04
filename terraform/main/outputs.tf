output "api_url" {
  description = "Public API endpoint URL (custom domain if set, otherwise the default APIGW endpoint)."
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
