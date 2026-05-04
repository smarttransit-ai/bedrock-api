output "state_bucket" {
  description = "Name of the S3 bucket used for Terraform state."
  value       = aws_s3_bucket.state.bucket
}

output "state_bucket_region" {
  description = "AWS region of the Terraform state bucket."
  value       = aws_s3_bucket.state.region
}

output "lock_table" {
  description = "Name of the DynamoDB table used for Terraform state locking."
  value       = aws_dynamodb_table.lock.name
}

output "backend_block" {
  description = "Copy-pasteable HCL backend configuration for terraform/main/backend.tf. Pipe with: terraform output -raw backend_block > ../main/backend.tf"
  value       = <<-EOT
  terraform {
    backend "s3" {
      bucket         = "${aws_s3_bucket.state.bucket}"
      key            = "${var.state_key}"
      region         = "${aws_s3_bucket.state.region}"
      dynamodb_table = "${aws_dynamodb_table.lock.name}"
      encrypt        = true
    }
  }
  EOT
}
