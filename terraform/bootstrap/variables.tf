variable "region" {
  description = "AWS region in which to create the state bucket and lock table."
  type        = string
  default     = "us-east-1"
}

variable "bucket_name_prefix" {
  description = "Prefix for the S3 state bucket name. A random 8-character hex suffix is appended to ensure global uniqueness."
  type        = string
  default     = "bedrock-api-tfstate"
}

variable "lock_table_name" {
  description = "Name of the DynamoDB table used for Terraform state locking."
  type        = string
  default     = "bedrock-api-tfstate-lock"
}

variable "state_key" {
  description = "S3 object key (path) for the main stack's Terraform state file."
  type        = string
  default     = "bedrock-api/terraform.tfstate"
}
