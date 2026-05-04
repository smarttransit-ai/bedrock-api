# bedrock-api

A Terraform-deployed AWS API Gateway → Lambda proxy that fronts AWS Bedrock
with per-token rate limits, monthly request quotas, monthly $ budgets,
input/output token caps, and per-model allowlists.

See [GOALS.md](./GOALS.md) for the full design intent and decisions.

## Status

Early development. See [bedrock-api_TASKS.md](./bedrock-api_TASKS.md) for the
ordered task breakdown and [ORCHESTRATION.md](./ORCHESTRATION.md) for the
build log.
