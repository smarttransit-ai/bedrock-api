# terraform/bootstrap

Provisions the S3 bucket and DynamoDB table used as the Terraform remote backend
for `terraform/main/`. Apply it **once per deployment account** with local state
(e.g. once as `roged10` for primary, once as `roged10_ccc` for ccc) — each account
gets its own state bucket + lock table, which the main stack selects via a
per-deployment `-backend-config` file.

## Purpose

- **S3 bucket** — versioned, AES256-encrypted, public-access-blocked, with a
  lifecycle rule that expires noncurrent versions after 90 days and a bucket
  policy that denies non-TLS access.
- **DynamoDB table** — PAY_PER_REQUEST billing, `LockID` hash key, AWS-managed
  encryption. Used by Terraform to prevent concurrent state modifications.

## Pre-requisites

AWS credentials with permission to create and configure S3 buckets and DynamoDB
tables. The following IAM actions are required:

- `s3:CreateBucket`, `s3:PutBucket*`, `s3:PutEncryptionConfiguration`, `s3:PutLifecycleConfiguration`, `s3:PutBucketPolicy`
- `dynamodb:CreateTable`, `dynamodb:DescribeTable`

## Apply

```sh
cd terraform/bootstrap
terraform init
terraform apply
```

## After apply

`terraform/main/backend.tf` is a **partial** backend config; this module's
output supplies the per-deployment `bucket`/`key`/`dynamodb_table`. Write them to
that deployment's `.tfbackend` file (do **not** overwrite `backend.tf`):

```sh
# primary:
terraform output -raw backend_config > ../main/primary.s3.tfbackend
# ccc (run while bootstrapping the ccc account):
terraform output -raw backend_config > ../main/ccc.s3.tfbackend
```

Then init the main stack against it:

```sh
cd ../main
terraform init -reconfigure -backend-config=<deployment>.s3.tfbackend
```

Commit the `.tfbackend` file (it contains only bucket/table names, no secrets).

## Local state file

Running `terraform apply` creates `terraform.tfstate` in `terraform/bootstrap/`.
This file is covered by the `*.tfstate` pattern in the root `.gitignore` and
will **not** be committed automatically. Keep a secure copy or note the resource
IDs — if the state file is lost, the existing bucket and table can be recovered:

```sh
terraform import aws_s3_bucket.state <bucket-name>
terraform import aws_dynamodb_table.lock <table-name>
```

## ⚠️ Warning: do not destroy carelessly

Destroying this module **permanently deletes the Terraform state for the main
stack**. To intentionally tear down the entire environment:

1. Run `terraform destroy` in `terraform/main/` first.
2. In `terraform/bootstrap/main.tf`, remove the `lifecycle { prevent_destroy = true }`
   blocks from both `aws_s3_bucket.state` and `aws_dynamodb_table.lock`.
3. Manually empty the S3 bucket (purge all object versions and delete markers),
   or temporarily set `force_destroy = true` on `aws_s3_bucket.state`.
4. Run `terraform destroy` in `terraform/bootstrap/`.
