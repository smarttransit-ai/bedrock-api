# When the HTTP API was placed behind the `enable_http_api` feature flag, the
# `module "api"` call gained `count`, changing every resource address from
# `module.api.<res>` to `module.api[0].<res>`. Without this `moved` block,
# Terraform would destroy and recreate the live API Gateway (new URL + downtime).
# Relocating the whole module instance keeps the existing APIGW in place, so a
# deploy with enable_http_api=true (default) is truly additive.
moved {
  from = module.api
  to   = module.api[0]
}
