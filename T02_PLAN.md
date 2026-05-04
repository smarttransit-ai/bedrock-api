# T02 — Terraform State Backend Bootstrap

## Problem Summary

Create a Terraform module at `terraform/bootstrap/` that provisions:

1. An S3 bucket with versioning, AES256 server-side encryption, public-access
   blocked, and a lifecycle rule that expires noncurrent versions after 90 days.
2. A DynamoDB table for Terraform state locking (PAY_PER_REQUEST, hash key
   `LockID`, AWS-managed SSE).

This module is applied **once** with local state by an operator. The bucket and
table it creates become the backend for `terraform/main/`. Once bootstrapped,
the module is never re-applied (unless recreating the environment from scratch).

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| State for this module | Local (no backend block) | Applied once; moving the state of the state bucket into itself is a circular dependency |
| Bucket uniqueness | `bucket_name_prefix` + `random_id` (8-char hex) | Bucket names are global; appending random bytes makes cross-account re-apply safe without manual input |
| `random_id` byte length | 4 bytes → 8 hex chars | Short enough to read, long enough to avoid collisions |
| SSE algorithm | `AES256` (S3-managed) | Sufficient for TF state; simpler than KMS (no key management overhead) |
| DynamoDB SSE | AWS-managed default key | Sufficient; no cross-account key sharing needed |
| DynamoDB billing | `PAY_PER_REQUEST` | Lock table has bursty, low-volume writes; no provisioned capacity waste |
| Lifecycle rule | Expire noncurrent versions after 90 days; omit `filter` block (matches all) | `filter {}` or `filter { prefix = "" }` is rejected by AWS provider v5; omitting filter is the v5-correct way to match all objects |
| `backend_block` output | Heredoc-style string output; `state_key` variable controls the key | Lets the operator pipe directly into `terraform/main/backend.tf`; key is configurable via variable |
| File layout | `versions.tf`, `main.tf`, `variables.tf`, `outputs.tf` | One file per concern; matches T01 conventions |
| Terraform version pin | `>= 1.7` | Project-wide minimum established in task spec |
| Provider pins | `aws ~> 5.0`, `random ~> 3.0` | Avoids surprise upgrades; matches task spec |
| Bucket TLS policy | `aws_s3_bucket_policy` denying `aws:SecureTransport = false` | State files may contain sensitive values; deny-HTTP is a standard hardening measure |
| `prevent_destroy` on bucket | `lifecycle { prevent_destroy = true }` | Guards against accidental `terraform destroy -target`; operator must remove before intentional teardown |
| `force_destroy` on bucket | Omitted (defaults to false) | State buckets should never allow silent force-deletion; the "bucket not empty" error is an intentional last-resort safety net. Operator must manually empty before destroy. |
| Satellite resource ordering | Explicit `depends_on` where implicit reference is absent | `lifecycle_configuration` and `versioning` have no implicit dependency chain; race condition would cause apply-time failure |

## Files to Create

### `terraform/bootstrap/versions.tf`

```hcl
terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  region = var.region
}
```

### `terraform/bootstrap/variables.tf`

Four variables:
- `region` — string, default `"us-east-1"`
- `bucket_name_prefix` — string, default `"bedrock-api-tfstate"`, description
  explains it gets a random suffix appended
- `lock_table_name` — string, default `"bedrock-api-tfstate-lock"`
- `state_key` — string, default `"bedrock-api/terraform.tfstate"`, description
  explains it is the S3 key used by the main stack backend

### `terraform/bootstrap/main.tf`

Resources (one resource per logical concern, in this order):

1. `random_id.suffix` — `byte_length = 4`
2. `aws_s3_bucket.state` — `bucket = "${var.bucket_name_prefix}-${random_id.suffix.hex}"`,
   `force_destroy = true`, `lifecycle { prevent_destroy = true }`
3. `aws_s3_bucket_versioning.state` — `versioning_configuration { status = "Enabled" }`;
   implicit dependency via `aws_s3_bucket.state.id`
4. `aws_s3_bucket_server_side_encryption_configuration.state` — AES256;
   implicit dependency via `aws_s3_bucket.state.id`
5. `aws_s3_bucket_public_access_block.state` — all four flags true;
   implicit dependency via `aws_s3_bucket.state.id`
6. `aws_s3_bucket_lifecycle_configuration.state` — single rule with
   `id = "expire-noncurrent-versions"`, `status = "Enabled"`, **no `filter` block**
   (matches all objects in provider v5), `noncurrent_version_expiration { noncurrent_days = 90 }`;
   `depends_on = [aws_s3_bucket_versioning.state]`
7. `aws_s3_bucket_policy.state` — deny-HTTP policy; `Resource` must be both
   `aws_s3_bucket.state.arn` (bucket-level ops) **and** `"${aws_s3_bucket.state.arn}/*"`
   (object-level ops); `Principal = "*"` (bare string, not `{"AWS":"*"}` — covers anonymous
   + non-IAM principals); `Effect = "Deny"`, `Action = "s3:*"`,
   `Condition = { Bool = { "aws:SecureTransport" = "false" } }`;
   `depends_on = [aws_s3_bucket_public_access_block.state]` (block_public_policy must
   be applied before AWS will accept the policy attachment)
8. `aws_dynamodb_table.lock` — name `var.lock_table_name`,
   `billing_mode = "PAY_PER_REQUEST"`, `hash_key = "LockID"`,
   `attribute { name = "LockID", type = "S" }`,
   `server_side_encryption { enabled = true }`

### `terraform/bootstrap/outputs.tf`

Four outputs:
- `state_bucket` — `aws_s3_bucket.state.bucket`
- `state_bucket_region` — `aws_s3_bucket.state.region`
- `lock_table` — `aws_dynamodb_table.lock.name`
- `backend_block` — multi-line string (use `<<-EOT ... EOT` heredoc) containing
  the HCL snippet interpolated with the actual values of
  `aws_s3_bucket.state.bucket`, `aws_s3_bucket.state.region`,
  `aws_dynamodb_table.lock.name`, and `var.state_key`:
  ```
  terraform {
    backend "s3" {
      bucket         = "<bucket>"
      key            = "<state_key>"
      region         = "<region>"
      dynamodb_table = "<lock_table>"
      encrypt        = true
    }
  }
  ```

### `terraform/bootstrap/README.md`

Sections:
1. **Purpose** — brief description of what the module does
2. **Pre-requisites** — AWS credentials with S3 + DynamoDB create permissions
3. **Apply** — `terraform init && terraform apply`
4. **After apply** — pipe the output directly into the backend file:
   ```
   terraform output -raw backend_block > ../main/backend.tf
   ```
5. **Local state file** — `terraform.tfstate` is created in `terraform/bootstrap/`.
   Keep it or store it safely. It is covered by the `*.tfstate` pattern in the
   root `.gitignore`. If the state file is lost, the existing bucket and table
   can be re-imported with `terraform import`.
6. **⚠️ Warning: do not destroy carelessly** — destroying this module destroys all
   Terraform state for the main stack. Before running `terraform destroy`, you must:
   (a) ensure the main stack is destroyed first, (b) remove the
   `lifecycle { prevent_destroy = true }` block from `aws_s3_bucket.state` in
   `main.tf`, then (c) run `terraform destroy`.

## Validation

After implementation:
- `terraform fmt -check -recursive` → exits 0
- `terraform -chdir=terraform/bootstrap init -backend=false && terraform -chdir=terraform/bootstrap validate` → exits 0
