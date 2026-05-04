# ---------------------------------------------------------------------------
# tokens — auth, limits, and token metadata
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "tokens" {
  name         = "${var.name_prefix}-tokens"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "token_id"

  deletion_protection_enabled = true

  attribute {
    name = "token_id"
    type = "S"
  }

  attribute {
    name = "owner"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "S"
  }

  global_secondary_index {
    name            = "owner-index"
    hash_key        = "owner"
    range_key       = "created_at"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ---------------------------------------------------------------------------
# usage — monthly aggregate counters per token
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "usage" {
  name         = "${var.name_prefix}-usage"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "token_id"
  range_key    = "period"

  deletion_protection_enabled = true

  attribute {
    name = "token_id"
    type = "S"
  }

  attribute {
    name = "period"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

# ---------------------------------------------------------------------------
# rate_limit — per-second request counters with TTL auto-cleanup
# ---------------------------------------------------------------------------
resource "aws_dynamodb_table" "rate_limit" {
  name         = "${var.name_prefix}-rate-limit"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "token_id"
  range_key    = "window_second"

  attribute {
    name = "token_id"
    type = "S"
  }

  attribute {
    name = "window_second"
    type = "N"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  server_side_encryption {
    enabled = true
  }
}
