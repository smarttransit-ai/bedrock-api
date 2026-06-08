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

output "backend_config" {
  description = "Per-deployment backend config for terraform/main (partial backend.tf). Write to a .tfbackend file, e.g.: terraform output -raw backend_config > ../main/ccc.s3.tfbackend, then: terraform init -reconfigure -backend-config=ccc.s3.tfbackend"
  value       = <<-EOT
  bucket         = "${aws_s3_bucket.state.bucket}"
  key            = "${var.state_key}"
  dynamodb_table = "${aws_dynamodb_table.lock.name}"
  EOT
}
