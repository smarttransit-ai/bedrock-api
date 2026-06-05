# =============================================================================
# CUTOVER RUNBOOK — R1 (two live deployments: primary roged10 + ccc roged10_ccc)
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
#   ECR_URL=$(terraform output -raw ecr_repository_url 2>/dev/null || \
#             terraform output -json | jq -r '.ecr_repository_url.value')
#   aws ecr get-login-password --region us-east-1 | \
#     docker login --username AWS --password-stdin "$ECR_URL"
#   docker buildx build --platform linux/amd64 --provenance=false \
#     -t "$ECR_URL:latest" lambda/proxy/
#   docker push "$ECR_URL:latest"
#
# Step 3 — Apply with coexistence (APIGW + Function URL live simultaneously):
#   terraform apply -var="image_uri=$ECR_URL:latest"
#   # enable_http_api defaults to true: APIGW is kept alive, Function URL is
#   # also created. No resources are destroyed in this apply.
#
# Step 4 — Validate the Function URL endpoint:
#   FN_URL=$(terraform output -raw function_url)  # always the Function URL
#   curl "$FN_URL/health"                          # expect {"status":"ok"}
#   curl -H "Authorization: Bearer <token>" "$FN_URL/usage"
#   # Run non-streaming parity checks against both /converse and /invoke routes.
#   # api_url still points to the APIGW URL while enable_http_api=true.
#
# Step 5 — Cut clients over:
#   Communicate the Function URL (function_url output) to all API consumers
#   (update client configs, docs, env vars). APIGW endpoint remains live.
#
# Step 6 — Retire APIGW (opt-in, AFTER client cutover):
#   terraform apply -var="image_uri=$ECR_URL:latest" -var="enable_http_api=false"
#   This destroys the APIGW HTTP API, routes, stage, and CloudWatch log group.
#   api_url output will now return the Function URL.
#   New deployments may set enable_http_api=false from the start.
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

# Legacy API Gateway HTTP API — count-gated by enable_http_api (default true).
# Kept alive during the Function URL coexistence window so that existing clients
# are not disrupted by a single apply. Set enable_http_api=false and re-apply
# ONLY after all clients have been cut over to the Function URL (see runbook).
module "api" {
  count  = var.enable_http_api ? 1 : 0
  source = "./modules/api"

  name_prefix          = var.name_prefix
  lambda_invoke_arn    = module.proxy.invoke_arn
  lambda_function_name = module.proxy.function_name
  log_retention_days   = var.log_retention_days
}
