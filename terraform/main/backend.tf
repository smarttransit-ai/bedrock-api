terraform {
  backend "s3" {
    bucket         = "bedrock-api-tfstate-22178a0f"
    key            = "bedrock-api/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "bedrock-api-tfstate-lock"
    encrypt        = true
  }
}
