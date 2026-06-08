terraform {
  # Partial backend config: bucket / key / dynamodb_table are supplied per
  # deployment so each account (primary, ccc) keeps its own isolated state.
  # Select the target at init time — switching only AWS_PROFILE does NOT switch
  # state:
  #   terraform init -reconfigure -backend-config=primary.s3.tfbackend  # roged10
  #   terraform init -reconfigure -backend-config=ccc.s3.tfbackend      # roged10_ccc
  backend "s3" {
    region  = "us-east-1"
    encrypt = true
  }
}
