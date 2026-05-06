data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Lambda source archive
# output_path is in this module's directory (always exists on checkout).
# archive_file excludes accepts exact relative paths only (no glob patterns).
# The zip is gitignored via *.zip in the root .gitignore.
# ---------------------------------------------------------------------------
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = var.lambda_source_dir
  output_path = "${path.module}/proxy.zip"
  excludes    = [".gitkeep", "__pycache__"]
}

# ---------------------------------------------------------------------------
# CloudWatch log group (created before Lambda so we own retention)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.name_prefix}-proxy"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_metric_filter" "pricing_fallback_count" {
  name           = "${var.name_prefix}-pricing-fallback-count"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.event = \"pricing_fallback_rate\" }"

  metric_transformation {
    name      = "PricingFallbackCount"
    namespace = "${var.name_prefix}/proxy"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "pricing_mode_invalid" {
  name           = "${var.name_prefix}-pricing-mode-invalid"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.event = \"pricing_mode_invalid\" }"

  metric_transformation {
    name      = "PricingModeInvalidCount"
    namespace = "${var.name_prefix}/proxy"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "request_complete_count" {
  name           = "${var.name_prefix}-request-complete-count"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.event = \"request_complete\" }"

  metric_transformation {
    name      = "RequestCompleteCount"
    namespace = "${var.name_prefix}/proxy"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "pricing_mode_on_demand_count" {
  name           = "${var.name_prefix}-pricing-mode-on-demand-count"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.event = \"pricing_audit\" && $.pricing_mode = \"on_demand\" }"

  metric_transformation {
    name      = "PricingModeOnDemandCount"
    namespace = "${var.name_prefix}/proxy"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "pricing_mode_batch_count" {
  name           = "${var.name_prefix}-pricing-mode-batch-count"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.event = \"pricing_audit\" && $.pricing_mode = \"batch\" }"

  metric_transformation {
    name      = "PricingModeBatchCount"
    namespace = "${var.name_prefix}/proxy"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "pricing_fallback_high" {
  alarm_name          = "${var.name_prefix}-pricing-fallback-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  metric_query {
    id          = "fallbacks"
    return_data = false
    metric {
      metric_name = "PricingFallbackCount"
      namespace   = "${var.name_prefix}/proxy"
      period      = 900
      stat        = "Sum"
    }
  }
  metric_query {
    id          = "requests"
    return_data = false
    metric {
      metric_name = "RequestCompleteCount"
      namespace   = "${var.name_prefix}/proxy"
      period      = 900
      stat        = "Sum"
    }
  }
  metric_query {
    id          = "threshold"
    expression  = "IF(requests*0.01 > 100, requests*0.01, 100)"
    return_data = false
  }
  metric_query {
    id          = "alarm_expr"
    expression  = "fallbacks-threshold"
    return_data = true
  }
  alarm_description  = "Fallback pricing rate spikes above threshold in 15m window."
  treat_missing_data = "notBreaching"
}

# ---------------------------------------------------------------------------
# IAM execution role
# ---------------------------------------------------------------------------
resource "aws_iam_role" "lambda" {
  name = "${var.name_prefix}-proxy-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.name_prefix}-proxy-policy"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoDBTokens"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = [var.tokens_table_arn]
      },
      {
        Sid      = "DynamoDBUsage"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = [var.usage_table_arn]
      },
      {
        Sid      = "DynamoDBRateLimit"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem"]
        Resource = [var.rate_limit_table_arn]
      },
      {
        Sid    = "Bedrock"
        Effect = "Allow"
        # Lambda is the model-allowlist gate (per-token allowed_models or
        # ALLOWED_MODELS_DEFAULT). IAM is intentionally permissive so that
        # tokens with no allowlist can call any Bedrock model the operator
        # account has access to. Per-token --budget caps the blast radius.
        Action   = ["bedrock:InvokeModel"]
        Resource = ["*"]
      },
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          aws_cloudwatch_log_group.lambda.arn,
          "${aws_cloudwatch_log_group.lambda.arn}:*",
        ]
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda function
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "proxy" {
  function_name    = "${var.name_prefix}-proxy"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_s

  # Caps blast radius from any high-rate flood. Account default is 1000;
  # 50 is generous for lab-scale interactive use. Bump to 100 for batch jobs.
  reserved_concurrent_executions = var.lambda_reserved_concurrency

  environment {
    variables = {
      TOKENS_TABLE           = var.tokens_table_name
      USAGE_TABLE            = var.usage_table_name
      RATE_LIMIT_TABLE       = var.rate_limit_table_name
      BEDROCK_REGION         = data.aws_region.current.name
      ALLOWED_MODELS_DEFAULT = var.allowed_models_default
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]
}
