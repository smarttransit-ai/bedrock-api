output "tokens_table_name" {
  description = "Name of the DynamoDB tokens table."
  value       = aws_dynamodb_table.tokens.name
}

output "tokens_table_arn" {
  description = "ARN of the DynamoDB tokens table."
  value       = aws_dynamodb_table.tokens.arn
}

output "tokens_owner_index_arn" {
  description = "ARN of the owner-index GSI on the tokens table. Use in Lambda and CLI IAM policies for dynamodb:Query."
  value       = "${aws_dynamodb_table.tokens.arn}/index/owner-index"
}

output "usage_table_name" {
  description = "Name of the DynamoDB usage table."
  value       = aws_dynamodb_table.usage.name
}

output "usage_table_arn" {
  description = "ARN of the DynamoDB usage table."
  value       = aws_dynamodb_table.usage.arn
}

output "rate_limit_table_name" {
  description = "Name of the DynamoDB rate_limit table."
  value       = aws_dynamodb_table.rate_limit.name
}

output "rate_limit_table_arn" {
  description = "ARN of the DynamoDB rate_limit table."
  value       = aws_dynamodb_table.rate_limit.arn
}

output "tables_json" {
  description = "JSON object with name and ARN for every table. Consume via: terraform output -raw tables_json"
  value = jsonencode({
    tokens = {
      name            = aws_dynamodb_table.tokens.name
      arn             = aws_dynamodb_table.tokens.arn
      owner_index_arn = "${aws_dynamodb_table.tokens.arn}/index/owner-index"
    }
    usage = {
      name = aws_dynamodb_table.usage.name
      arn  = aws_dynamodb_table.usage.arn
    }
    rate_limit = {
      name = aws_dynamodb_table.rate_limit.name
      arn  = aws_dynamodb_table.rate_limit.arn
    }
  })
}
