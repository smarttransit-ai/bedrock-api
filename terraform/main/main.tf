module "data" {
  source      = "./modules/data"
  name_prefix = var.name_prefix
}

module "proxy" {
  source = "./modules/proxy"

  name_prefix            = var.name_prefix
  lambda_source_dir      = "${path.module}/../../lambda/proxy"
  lambda_memory_mb       = var.lambda_memory_mb
  lambda_timeout_s       = var.lambda_timeout_s
  log_retention_days     = var.log_retention_days
  allowed_models_default = var.default_models

  tokens_table_name     = module.data.tokens_table_name
  tokens_table_arn      = module.data.tokens_table_arn
  usage_table_name      = module.data.usage_table_name
  usage_table_arn       = module.data.usage_table_arn
  rate_limit_table_name = module.data.rate_limit_table_name
  rate_limit_table_arn  = module.data.rate_limit_table_arn
}

module "api" {
  source = "./modules/api"

  name_prefix          = var.name_prefix
  lambda_invoke_arn    = module.proxy.invoke_arn
  lambda_function_name = module.proxy.function_name
  log_retention_days   = var.log_retention_days
  domain_name          = var.domain_name
  hosted_zone_id       = var.hosted_zone_id
}
