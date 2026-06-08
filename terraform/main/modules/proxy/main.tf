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
# CloudWatch alarms for Lambda guardrails
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
        # bedrock:InvokeModelWithResponseStream is added for streaming routes.
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
# publish = true so we can create a versioned alias for provisioned concurrency
# ---------------------------------------------------------------------------
resource "aws_lambda_function" "proxy" {
  function_name = "${var.name_prefix}-proxy"
  image_uri     = var.image_uri # passed in from terraform/main/main.tf after docker push
  package_type  = "Image"
  role          = aws_iam_role.lambda.arn
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_s
  publish       = true

  # Caps blast radius from any high-rate flood. Account default is 1000;
  # 50 is generous for lab-scale interactive use. Bump to 100 for batch jobs.
  reserved_concurrent_executions = var.lambda_reserved_concurrency

  environment {
    variables = {
      TOKENS_TABLE                 = var.tokens_table_name
      USAGE_TABLE                  = var.usage_table_name
      RATE_LIMIT_TABLE             = var.rate_limit_table_name
      BEDROCK_REGION               = data.aws_region.current.region
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
# Lambda alias "live" pointing at the published version
# The REST API integration targets the alias so provisioned concurrency applies
# ---------------------------------------------------------------------------
resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.proxy.function_name
  function_version = aws_lambda_function.proxy.version
}

# ---------------------------------------------------------------------------
# Provisioned concurrency on the "live" alias (eliminates cold-start 500s)
# ---------------------------------------------------------------------------
resource "aws_lambda_provisioned_concurrency_config" "live" {
  function_name                     = aws_lambda_function.proxy.function_name
  qualifier                         = aws_lambda_alias.live.name
  provisioned_concurrent_executions = var.provisioned_concurrency
}

# ---------------------------------------------------------------------------
# API Gateway REST API (REGIONAL — 5-min idle timeout, required for streaming)
# ---------------------------------------------------------------------------
resource "aws_api_gateway_rest_api" "proxy" {
  name = "${var.name_prefix}-proxy"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# Greedy proxy resource captures all paths under /
resource "aws_api_gateway_resource" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.proxy.id
  parent_id   = aws_api_gateway_rest_api.proxy.root_resource_id
  path_part   = "{proxy+}"
}

# ANY method on /{proxy+} — forwards all methods and paths to the Lambda
resource "aws_api_gateway_method" "proxy" {
  rest_api_id   = aws_api_gateway_rest_api.proxy.id
  resource_id   = aws_api_gateway_resource.proxy.id
  http_method   = "ANY"
  authorization = "NONE"
}

# ANY method on / (root) — needed for paths without a trailing segment (e.g. GET /health)
resource "aws_api_gateway_method" "root" {
  rest_api_id   = aws_api_gateway_rest_api.proxy.id
  resource_id   = aws_api_gateway_rest_api.proxy.root_resource_id
  http_method   = "ANY"
  authorization = "NONE"
}

# Streaming invoke ARN for the "live" alias:
# aws_lambda_alias does not expose response_streaming_invoke_arn, so construct it
# from the alias ARN. Format from provider source (function.go responseStreamingInvokeARN):
#   arn:aws:apigateway:{region}:lambda:path/2021-11-15/functions/{alias_arn}/response-streaming-invocations
# Note: 2021-11-15 differs from the regular invoke_arn path (2015-03-31); it is the
# date of the Lambda Invoke URL API version that introduced response streaming.
locals {
  alias_streaming_invoke_arn = "arn:aws:apigateway:${data.aws_region.current.region}:lambda:path/2021-11-15/functions/${aws_lambda_alias.live.arn}/response-streaming-invocations"
}

# Streaming AWS_PROXY integration on /{proxy+}
# uri targets the alias streaming ARN so provisioned concurrency applies
resource "aws_api_gateway_integration" "proxy" {
  rest_api_id             = aws_api_gateway_rest_api.proxy.id
  resource_id             = aws_api_gateway_resource.proxy.id
  http_method             = aws_api_gateway_method.proxy.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = local.alias_streaming_invoke_arn
  timeout_milliseconds    = 900000
  response_transfer_mode  = "STREAM"
}

# Streaming AWS_PROXY integration on / (root)
resource "aws_api_gateway_integration" "root" {
  rest_api_id             = aws_api_gateway_rest_api.proxy.id
  resource_id             = aws_api_gateway_rest_api.proxy.root_resource_id
  http_method             = aws_api_gateway_method.root.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = local.alias_streaming_invoke_arn
  timeout_milliseconds    = 900000
  response_transfer_mode  = "STREAM"
}

# Deployment — triggered by hash of integration/method config values (not just IDs)
# so any change to URI, response_transfer_mode, timeout, auth, or method causes a redeploy.
resource "aws_api_gateway_deployment" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.proxy.id

  triggers = {
    redeployment = sha256(jsonencode([
      # Resource structure
      aws_api_gateway_resource.proxy.path_part,
      # Method config
      aws_api_gateway_method.proxy.http_method,
      aws_api_gateway_method.proxy.authorization,
      aws_api_gateway_method.root.http_method,
      aws_api_gateway_method.root.authorization,
      # Integration config — these change when URI, streaming mode, or timeout changes
      aws_api_gateway_integration.proxy.uri,
      aws_api_gateway_integration.proxy.type,
      aws_api_gateway_integration.proxy.response_transfer_mode,
      aws_api_gateway_integration.proxy.timeout_milliseconds,
      aws_api_gateway_integration.root.uri,
      aws_api_gateway_integration.root.type,
      aws_api_gateway_integration.root.response_transfer_mode,
      aws_api_gateway_integration.root.timeout_milliseconds,
      # Alias ARN changes when a new Lambda version is published and alias is updated
      aws_lambda_alias.live.arn,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_method.proxy,
    aws_api_gateway_method.root,
    aws_api_gateway_integration.proxy,
    aws_api_gateway_integration.root,
  ]
}

# ---------------------------------------------------------------------------
# CloudWatch access log group for REST API
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${var.name_prefix}-proxy"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# CloudWatch account-level role for API Gateway logging
# Guard: skip if account role is already configured via variable
# ---------------------------------------------------------------------------
resource "aws_iam_role" "apigw_cloudwatch" {
  count = var.apigw_cloudwatch_role_already_set ? 0 : 1
  name  = "${var.name_prefix}-apigw-cloudwatch-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "apigateway.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "apigw_cloudwatch" {
  count      = var.apigw_cloudwatch_role_already_set ? 0 : 1
  role       = aws_iam_role.apigw_cloudwatch[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "main" {
  count               = var.apigw_cloudwatch_role_already_set ? 0 : 1
  cloudwatch_role_arn = aws_iam_role.apigw_cloudwatch[0].arn

  depends_on = [aws_iam_role_policy_attachment.apigw_cloudwatch]
}

# Stage — explicit stage required for REST API
# depends_on aws_api_gateway_account ensures the account-level CloudWatch role is set
# before stage creation so access_log_settings does not fail on first apply.
resource "aws_api_gateway_stage" "v1" {
  rest_api_id   = aws_api_gateway_rest_api.proxy.id
  deployment_id = aws_api_gateway_deployment.proxy.id
  stage_name    = "v1"

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId               = "$context.requestId"
      requestTime             = "$context.requestTime"
      httpMethod              = "$context.httpMethod"
      resourcePath            = "$context.resourcePath"
      status                  = "$context.status"
      protocol                = "$context.protocol"
      responseLength          = "$context.responseLength"
      integrationStatus       = "$context.integration.integrationStatus"
      integrationErrorMessage = "$context.integrationErrorMessage"
    })
  }

  depends_on = [aws_api_gateway_account.main]
}

# Stage-level throttle (20 rps steady, 40 burst — replaces lost HTTP API throttle)
resource "aws_api_gateway_method_settings" "proxy" {
  rest_api_id = aws_api_gateway_rest_api.proxy.id
  stage_name  = aws_api_gateway_stage.v1.stage_name
  method_path = "*/*"

  settings {
    throttling_rate_limit  = var.throttling_rate_limit
    throttling_burst_limit = var.throttling_burst_limit
    logging_level          = "OFF"
  }

  depends_on = [
    aws_api_gateway_account.main,
  ]
}

# ---------------------------------------------------------------------------
# Lambda permission — allow REST API to invoke the alias
# ---------------------------------------------------------------------------
resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.proxy.function_name
  qualifier     = aws_lambda_alias.live.name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.proxy.execution_arn}/*/*"
}
