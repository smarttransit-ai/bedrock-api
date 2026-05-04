# bedrock-api — Goals

A Terraform project for deploying and managing access to AWS Bedrock using a
Gateway Pattern: route all client requests through a central API Gateway →
Lambda proxy that authenticates, authorizes, meters, and forwards to Bedrock.

## High-level requirements (from user)

- Generate / revoke / limit API tokens to hand out to lab members.
- Deploy with Terraform for easy reproducible deployment.

## Decision Q&A (verbatim)

The following are the questions asked before starting work and the user's
answers. These are the source of truth for the design.

---

### 1. API surface — what do clients actually call?

Lab members typically want a drop-in for existing tools. Options:

- **(a) OpenAI-compatible** (`/v1/chat/completions`) — works with LibreChat,
  Cursor, LangChain, OpenWebUI, etc. Most flexible for lab use.
- **(b) Anthropic-compatible** (`/v1/messages`) — works with Claude Code,
  Anthropic SDK.
- **(c) Native Bedrock passthrough** — thinnest proxy, but clients must speak
  Bedrock.
- **(d) Both (a) + (b)** — most useful, more code.

**Answer: (c)** — Native Bedrock passthrough.

---

### 2. Token / auth model

- **(a) API Gateway native API keys + usage plans** — built-in rate limits +
  quotas, no Lambda overhead for auth, but quotas are request-count only (not
  $ or tokens).
- **(b) Custom keys in DynamoDB checked by Lambda** — lets you enforce
  $/month budgets, per-model allowlists, input/output token quotas, easy
  revocation. More code.
- **(c) Hybrid** — APIGW key for the rate-limit edge, Lambda checks DynamoDB
  for budget/model policy.

**Answer: (b)** — Custom keys in DynamoDB checked by Lambda.

---

### 3. What does "limit" mean? Pick any combo:

- requests/sec (rate)
- requests/month (quota)
- **$/month budget per token** (most useful, requires Lambda + DynamoDB)
- input/output token caps
- model allowlist per key (e.g. grad student gets Haiku only)

**Answer: all listed** — every limit type must be enforceable per API token:
requests/sec, requests/month, $/month budget, input/output token caps, and
per-model allowlist.

---

### 4. Streaming?

SSE streaming for chat responses (`stream: true`)? Required for most chat UIs.
Adds Lambda Function URL or APIGW WebSocket complexity.

**Answer: no** — streaming not needed.

---

### 5. AWS region + models?

Which region (e.g. `us-east-1`, `us-west-2`)? Specific models to expose
(Claude Sonnet 4.6, Haiku 4.5, Llama, Titan)? Use cross-region inference
profiles?

**Answer: based in Nashville, TN — use whatever region is recommended for
Nashville.**

Resolved by orchestrator: **`us-east-1` (N. Virginia)** as the default region.
`us-east-2` (Ohio) is geographically closer to Nashville but `us-east-1` has
the broadest Bedrock model availability and lowest practical inference
latency from Tennessee. Region must be a Terraform variable so it can be
overridden.

Default model allowlist: Claude Sonnet 4.6 and Claude Haiku 4.5 via
cross-region inference profiles. Additional models may be added by editing
the model allowlist variable.

---

### 6. Terraform state backend?

S3 + DynamoDB lock, Terraform Cloud, or local-only for now?

**Answer: should all be cloud based** — no local state.

Resolved by orchestrator: **S3 + DynamoDB** backend (pure AWS, no extra
vendor). A bootstrap module creates the state bucket and lock table; the
main stack uses them.

---

### 7. Org / repo creation?

The `smarttransit-ai` org repo doesn't exist. Options:

- (a) Create it under `smarttransit-ai`.
- (b) Create it under personal account.
- (c) Just init local + create remote later.

**Answer: (a)** — create under `smarttransit-ai`.

---

### 8. Admin interface for managing tokens?

CLI script (`./bin/issue-key alice --budget 50 --models claude-sonnet-4-6`),
small admin Lambda + a static page, or just `terraform apply` with a tokens
map?

**Answer: yes, CLI is good.**

Resolved by orchestrator: a CLI in `bin/` that talks directly to AWS
(DynamoDB + Secrets Manager / SSM) using the operator's existing AWS
credentials. Subcommands: `issue`, `revoke`, `list`, `show`, `set-limit`,
`usage`.

---

## Resolved design summary

| Area                  | Decision                                                                 |
| --------------------- | ------------------------------------------------------------------------ |
| Client API            | Native Bedrock passthrough (`InvokeModel`, `Converse`)                   |
| Auth                  | Custom bearer tokens stored in DynamoDB, validated by Lambda             |
| Limits per token      | rate (req/sec), monthly request quota, $/month budget, in/out token caps, model allowlist |
| Streaming             | Not supported in v1                                                      |
| Region                | `us-east-1` (variable, overridable)                                      |
| Default models        | Claude Sonnet 4.6, Claude Haiku 4.5 (via cross-region inference profiles)|
| IaC                   | Terraform                                                                |
| TF state              | S3 + DynamoDB lock (cloud)                                               |
| Repo                  | `github.com/smarttransit-ai/bedrock-api`                                 |
| Admin UX              | CLI (`bin/`) using operator AWS credentials                              |

## Out of scope for v1

- Streaming responses.
- OpenAI- or Anthropic-shaped API surfaces.
- Web admin UI.
- Multi-account / cross-account Bedrock access.
- Self-service token rotation for lab members.

---

## Post-deploy amendments

Captured here so future readers know the live design diverged from the
original Q&A above. The Q&A is a faithful record of the up-front design
decisions; the items below are decisions made after the first end-to-end
deploy succeeded and revealed sharper requirements.

- **IAM is wide open on Bedrock** (`bedrock:InvokeModel` on `*`). The
  original plan scoped IAM to a specific inference-profile allowlist.
  Cross-region inference profiles dispatch to underlying foundation models,
  so the IAM scope had to widen anyway — at which point the Lambda became
  the natural model gate. `var.model_allowlist` was deleted; the per-token
  `allowed_models` attribute and the optional system-wide `var.default_models`
  (Lambda env `ALLOWED_MODELS_DEFAULT`) are the real model gates.
- **Default `--budget` on `bedrock-api issue` is $200/month.** Originally
  every limit defaulted to absent (= unlimited). $200 is the only limit
  that now defaults to a value; the rest still default to absent. To issue
  a token with no monthly USD cap, the operator must remove
  `limit_monthly_usd_micros` directly with `aws dynamodb update-item ...
  REMOVE` (intentionally not exposed via CLI).
