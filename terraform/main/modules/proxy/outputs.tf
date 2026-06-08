output "function_name" {
  description = "Lambda function name."
  value       = aws_lambda_function.proxy.function_name
}

output "function_arn" {
  description = "Lambda function ARN."
  value       = aws_lambda_function.proxy.arn
}

output "invoke_arn" {
  description = "Lambda invoke ARN (base — alias invoke ARN used by API Gateway)."
  value       = aws_lambda_function.proxy.invoke_arn
}

output "alias_invoke_arn" {
  description = "Lambda alias 'live' invoke ARN."
  value       = aws_lambda_alias.live.invoke_arn
}

output "ecr_repository_url" {
  description = "ECR repository URL. Tag and push the proxy image here before terraform apply."
  value       = aws_ecr_repository.proxy.repository_url
}

output "pricing_bucket" {
  description = "S3 bucket holding the live pricing catalog (pricing/current.json)."
  value       = aws_s3_bucket.pricing.bucket
}

output "role_arn" {
  description = "Lambda execution role ARN."
  value       = aws_iam_role.lambda.arn
}

output "rest_api_id" {
  description = "REST API ID."
  value       = aws_api_gateway_rest_api.proxy.id
}

output "rest_api_execution_arn" {
  description = "REST API execution ARN (base for Lambda resource-based policy)."
  value       = aws_api_gateway_rest_api.proxy.execution_arn
}

output "api_url" {
  description = "REST API invoke URL (REGIONAL endpoint, stage v1)."
  value       = "https://${aws_api_gateway_rest_api.proxy.id}.execute-api.${data.aws_region.current.region}.amazonaws.com/${aws_api_gateway_stage.v1.stage_name}"
}
