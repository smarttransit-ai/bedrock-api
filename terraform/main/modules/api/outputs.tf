output "api_endpoint" {
  description = "Default APIGW endpoint URL."
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "api_url" {
  description = "API URL — custom domain if set, else the default APIGW endpoint."
  value       = var.domain_name != "" ? "https://${var.domain_name}" : aws_apigatewayv2_api.main.api_endpoint
}

output "api_id" {
  description = "APIGW HTTP API ID."
  value       = aws_apigatewayv2_api.main.id
}

output "execution_arn" {
  description = "APIGW execution ARN (base for Lambda resource-based policy)."
  value       = aws_apigatewayv2_api.main.execution_arn
}
