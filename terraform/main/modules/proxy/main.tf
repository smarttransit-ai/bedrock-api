data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# ECR repository for the proxy container image
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "proxy" {
  name                 = "${var.name_prefix}-proxy"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
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
# CloudWatch alarms for Function URL guardrails (amendment R3)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${var.name_prefix}-proxy-throttles"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  metric_name         = "Throttles"
  namespace           = "AWS/Lambda"
  period              = 60
  statistic           = "Sum"
  dimensions = {
    FunctionName = aws_lambda_function.proxy.function_name
  }
  alarm_description  = "Lambda throttles detected — reserved concurrency may be exhausted."
  treat_missing_data = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.name_prefix}-proxy-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 60
  statistic           = "Sum"
  dimensions = {
    FunctionName = aws_lambda_function.proxy.function_name
  }
  alarm_description  = "Lambda errors (unhandled exceptions / timeouts)."
  treat_missing_data = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "lambda_concurrent_executions" {
  alarm_name          = "${var.name_prefix}-proxy-concurrency-high"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  # Alert at 80% of reserved concurrency (default 50 → alarm at 40).
  # Adjust threshold via var.lambda_reserved_concurrency if changed.
  threshold   = floor(var.lambda_reserved_concurrency * 0.8)
  metric_name = "ConcurrentExecutions"
  namespace   = "AWS/Lambda"
  period      = 60
  statistic   = "Maximum"
  dimensions = {
    FunctionName = aws_lambda_function.proxy.function_name
  }
  alarm_description  = "Concurrent executions approaching reserved concurrency cap."
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
        # bedrock:InvokeModelWithResponseStream is added for Phase B streaming routes.
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
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
# Lambda function — container image
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "proxy" {
  function_name = "${var.name_prefix}-proxy"
  image_uri     = var.image_uri # passed in from terraform/main/main.tf after docker push
  package_type  = "Image"
  role          = aws_iam_role.lambda.arn
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_s

  # Caps blast radius from any high-rate flood. Account default is 1000;
  # 50 is generous for lab-scale interactive use. Bump to 100 for batch jobs.
  # NOTE: Function URLs have no built-in throttling (unlike APIGW); this is
  # the primary global rate cap. Enforce limit_rps on all production tokens.
  reserved_concurrent_executions = var.lambda_reserved_concurrency

  environment {
    variables = {
      TOKENS_TABLE                 = var.tokens_table_name
      USAGE_TABLE                  = var.usage_table_name
      RATE_LIMIT_TABLE             = var.rate_limit_table_name
      BEDROCK_REGION               = data.aws_region.current.name
      ALLOWED_MODELS_DEFAULT       = var.allowed_models_default
      PORT                         = "8080"
      AWS_LWA_PORT                 = "8080"
      AWS_LWA_INVOKE_MODE          = "RESPONSE_STREAM"
      AWS_LWA_READINESS_CHECK_PATH = "/health"
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]
}

# ---------------------------------------------------------------------------
# Lambda Function URL — RESPONSE_STREAM, no IAM auth (app handles bearer auth)
# ---------------------------------------------------------------------------
resource "aws_lambda_function_url" "proxy" {
  function_name      = aws_lambda_function.proxy.function_name
  authorization_type = "NONE"
  invoke_mode        = "RESPONSE_STREAM"
}
