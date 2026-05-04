variable "name_prefix" {
  description = "Prefix for all DynamoDB table names. Tables will be named <prefix>-tokens, <prefix>-usage, and <prefix>-rate-limit."
  type        = string
  default     = "bedrock-api"
}
