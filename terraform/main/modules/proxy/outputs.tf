output "function_name" {
  description = "Lambda function name."
  value       = aws_lambda_function.proxy.function_name
}

output "function_arn" {
  description = "Lambda function ARN."
  value       = aws_lambda_function.proxy.arn
}

output "invoke_arn" {
  description = "Lambda invoke ARN (used by APIGW AWS_PROXY integration)."
  value       = aws_lambda_function.proxy.invoke_arn
}

output "function_url" {
  description = "Lambda Function URL (RESPONSE_STREAM). Use this as the API base URL."
  value       = aws_lambda_function_url.proxy.function_url
}

output "ecr_repository_url" {
  description = "ECR repository URL. Tag and push the proxy image here before terraform apply."
  value       = aws_ecr_repository.proxy.repository_url
}

output "role_arn" {
  description = "Lambda execution role ARN."
  value       = aws_iam_role.lambda.arn
}
