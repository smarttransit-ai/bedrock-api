# ---------------------------------------------------------------------------
# Remote state backend (S3 + DynamoDB locking).
#
# Fill in the values from `terraform/bootstrap/` outputs BEFORE running
# `terraform init` for the first time:
#
#   cd terraform/bootstrap
#   terraform output -raw backend_block
#
# Paste the printed block here, replacing the PLACEHOLDER values below.
# ---------------------------------------------------------------------------
terraform {
  backend "s3" {
    bucket         = "PLACEHOLDER-state-bucket-name"
    key            = "bedrock-api/terraform.tfstate"
    region         = "PLACEHOLDER-region"
    dynamodb_table = "PLACEHOLDER-lock-table-name"
    encrypt        = true
  }
}
