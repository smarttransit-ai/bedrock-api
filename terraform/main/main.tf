# =============================================================================
# DEPLOYMENT RUNBOOK — two live deployments: primary (roged10) + ccc (roged10_ccc)
# =============================================================================
#
# Per-deployment sequence (run TWICE: once per AWS profile):
#   AWS_PROFILE=roged10       → account 343084147688 (primary)
#   AWS_PROFILE=roged10_ccc   → account 066949051849 (ccc / CVISION billing)
#
# Step 1 — Bootstrap ECR (one-time per deployment, safe without a real image):
#   terraform init  # if first time or provider changes
#   terraform apply -target=module.proxy.aws_ecr_repository.proxy \
#     -var='image_uri=placeholder'
#   # Note: validation block allows placeholder only for -target ECR bootstrap;
#   # full apply requires a real image_uri.
#
# Step 2 — Build and push the proxy image:
#   ECR_URL=$(terraform output -raw ecr_repository_url)
#   aws ecr get-login-password --region us-east-1 | \
#     docker login --username AWS --password-stdin "$ECR_URL"
#   docker buildx build --platform linux/amd64 --provenance=false \
#     -t "$ECR_URL:latest" lambda/proxy/
#   docker push "$ECR_URL:latest"
#
# Step 3 — Apply:
#   terraform apply -var="image_uri=$ECR_URL:latest"
#
# Step 4 — Validate the REST API endpoint:
#   API_URL=$(terraform output -raw api_url)
#   curl "$API_URL/health"                           # expect {"status":"ok"}
#   curl -H "Authorization: Bearer <token>" "$API_URL/usage"
#
# Architecture notes:
#   - REST API: REGIONAL endpoint (5-min idle timeout, required for streaming)
#   - Integration: AWS_PROXY + response_transfer_mode=STREAM + 900s timeout
#   - Alias "live" with provisioned_concurrency=1 eliminates cold-start 500s
#   - Stage "v1" with 20 rps / 40 burst throttle
#
# State migration from the old Function URL + HTTP API v2 (already-applied deployments):
#   Terraform `removed` blocks cannot address count-indexed module instances (module.api[0])
#   in the current Terraform version. Destroy the old resources explicitly before applying:
#
#   terraform destroy -target='module.proxy.aws_lambda_function_url.proxy' \
#     -var="image_uri=$ECR_URL:latest"
#   terraform destroy -target='module.api[0].aws_apigatewayv2_api.main' \
#     -var="image_uri=$ECR_URL:latest"
#   # The apigatewayv2_api destroy cascades to routes, integrations, and the stage.
#   # If the CW log group persists:
#   terraform destroy -target='module.api[0].aws_cloudwatch_log_group.api_access' \
#     -var="image_uri=$ECR_URL:latest"
#   # Lambda permission for old APIGW (if still in state):
#   terraform destroy -target='module.api[0].aws_lambda_permission.apigw' \
#     -var="image_uri=$ECR_URL:latest"
#
#   Then run the full apply:
#   terraform apply -var="image_uri=$ECR_URL:latest"
#
# =============================================================================

module "data" {
  source      = "./modules/data"
  name_prefix = var.name_prefix
}

module "proxy" {
  source = "./modules/proxy"

  name_prefix                 = var.name_prefix
  image_uri                   = var.image_uri
  lambda_memory_mb            = var.lambda_memory_mb
  lambda_timeout_s            = var.lambda_timeout_s
  lambda_reserved_concurrency = var.lambda_reserved_concurrency
  log_retention_days          = var.log_retention_days
  allowed_models_default      = var.default_models

  tokens_table_name     = module.data.tokens_table_name
  tokens_table_arn      = module.data.tokens_table_arn
  usage_table_name      = module.data.usage_table_name
  usage_table_arn       = module.data.usage_table_arn
  rate_limit_table_name = module.data.rate_limit_table_name
  rate_limit_table_arn  = module.data.rate_limit_table_arn
}
