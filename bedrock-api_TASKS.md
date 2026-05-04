# bedrock-api — Task Decomposition

Source of truth: [GOALS.md](./GOALS.md). Build log: [ORCHESTRATION.md](./ORCHESTRATION.md).

Tasks are ordered to minimize blocking. Each task has acceptance criteria,
dependencies, and an intelligence tier (`low` = mechanical/scaffold,
`medium` = standard implementation work, `high` = design-sensitive). Per
[orchestration instructions](~/.llm/memory/instructions_orchestrate.md), each
task is planned (Plan agent) → reviewed → implemented (Implement agent).

---

## T01 — Repo scaffolding & layout

**Tier:** low

**Description:** Create the top-level directory layout and minimal config
files so every later task lands in a known place.

**Deliverables:**
- `terraform/bootstrap/` (state bucket + lock table module — applied once,
  before main).
- `terraform/main/` (the deployable stack).
- `lambda/proxy/` (the Bedrock proxy Lambda source).
- `lambda/authorizer/` (optional separate dir if we split auth; otherwise
  fold into `proxy/`).
- `bin/` (admin CLI entrypoint).
- `bin/lib/` or `cli/` (Python package backing the CLI).
- `tests/` (pytest tree mirroring `lambda/` and `cli/`).
- `.editorconfig`, `pyproject.toml` with `ruff` + `pytest` + `boto3`,
  `Makefile` (or `justfile`) with `fmt`, `lint`, `test`, `package`,
  `tf-init`, `tf-plan`, `tf-apply` targets.
- Pre-commit config running `ruff format`, `ruff check`, `terraform fmt`.

**Acceptance:**
- `make lint test` exits 0 on a fresh checkout (no real tests yet — a single
  smoke test is fine).
- `terraform fmt -check -recursive` exits 0.

**Depends on:** none.

---

## T02 — Terraform state backend bootstrap

**Tier:** medium

**Description:** Module that creates the S3 bucket (versioning, encryption,
public-access-blocked) and DynamoDB lock table used as the backend for the
main stack. Applied once with local state, then we never touch it.

**Deliverables:**
- `terraform/bootstrap/main.tf`, `variables.tf`, `outputs.tf`,
  `versions.tf`.
- `terraform/bootstrap/README.md` with apply instructions.
- Outputs printed for the bucket name, region, and lock table name so they
  can be pasted into `terraform/main/backend.tf`.

**Acceptance:**
- `terraform init && terraform validate` pass.
- Resource diff is exactly: 1 S3 bucket (+ versioning, SSE, public access
  block, lifecycle for noncurrent versions), 1 DynamoDB table (PAY_PER_REQUEST,
  hash key `LockID`).

**Depends on:** T01.

---

## T03 — DynamoDB schema & Terraform module for token / usage tables

**Tier:** high

**Description:** Design and implement the DynamoDB schema that backs auth +
limits + usage. Two-table design proposed (validated in planning):

- `tokens` — primary key on `token_id` (the public key prefix). Item holds
  hashed-secret, owner, created_at, status (active/revoked), monthly
  request quota, monthly $ budget, per-request rate limit (req/sec), per-
  request input/output token caps, model allowlist (set of inference
  profile / model IDs), notes.
- `usage` — primary key (`token_id`, `period`) where `period` is e.g.
  `2026-05` for monthly aggregates. Counters: requests, input_tokens,
  output_tokens, dollars_micro (USD * 1e6, integer for atomic
  `ADD`).

**Deliverables:**
- `terraform/main/modules/data/` with both tables, point-in-time recovery
  on, server-side encryption on, billing PAY_PER_REQUEST.
- Documented item shapes in `terraform/main/modules/data/README.md`.

**Acceptance:**
- `terraform validate` passes.
- README includes the exact item shape used by both Lambda and CLI.

**Depends on:** T02.

---

## T04 — Bedrock proxy Lambda (Python, `boto3`)

**Tier:** high

**Description:** The core handler. Pseudocode:

1. Parse bearer token from `Authorization` header → look up `tokens` row
   by `token_id` prefix → constant-time compare hashed secret. Reject if
   missing/revoked.
2. Enforce per-second rate limit via DynamoDB conditional `ADD` on a
   second-bucketed counter (token_id, second-epoch) with TTL — or via
   APIGW usage plan (decided in plan review).
3. Read the `tokens` row's monthly request quota and $ budget; read the
   `usage` row for the current month; reject if either is exhausted.
4. Validate the requested Bedrock model ID is in this token's allowlist.
   Apply input-token cap if the request body exceeds it (count tokens via
   the Bedrock model's tokenizer if cheap, otherwise rough char heuristic).
5. Forward the request to Bedrock (`InvokeModel` and/or `Converse`).
6. On the response, parse usage from Bedrock's response metadata
   (`inputTokens`, `outputTokens`) and compute USD using a static
   per-model price map. Atomic `ADD` to the `usage` row.
7. Return the Bedrock response unmodified.

**Deliverables:**
- `lambda/proxy/handler.py` (single entry point).
- `lambda/proxy/auth.py`, `limits.py`, `pricing.py`, `bedrock.py` —
  cleanly separated.
- `lambda/proxy/requirements.txt` (probably empty — boto3 ships with the
  Lambda runtime).
- `lambda/proxy/README.md`.
- Unit tests in `tests/lambda/proxy/` covering: valid token / revoked /
  unknown / over-quota / over-budget / model not allowed / happy path.
  Use `moto` for DynamoDB and a stub `bedrock-runtime` client.

**Acceptance:**
- All listed tests pass.
- `ruff check` and `ruff format --check` clean.
- Cold-start zip ≤ 5 MB (boto3 excluded).

**Depends on:** T03.

---

## T05 — Terraform: API Gateway + Lambda + IAM wiring

**Tier:** medium

**Description:** Stand up the deployable stack:

- IAM role for the proxy Lambda with least-privilege policy for: read/write
  to `tokens` and `usage` DynamoDB tables, `bedrock:InvokeModel` on the
  allowlisted models, CloudWatch Logs.
- The Lambda itself, packaged from `lambda/proxy/`.
- API Gateway HTTP API (cheaper than REST API; we don't need API keys
  here since auth is in-Lambda) with a single `ANY /{proxy+}` route or a
  small set of explicit routes.
- A Route 53 record + ACM cert if `var.domain_name` is set; otherwise just
  the default APIGW URL.
- CloudWatch log group with a sane retention (14 days).

**Deliverables:**
- `terraform/main/main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`,
  `backend.tf` (filled in from T02 outputs).
- `terraform/main/modules/proxy/` for the Lambda + IAM.
- `terraform/main/modules/api/` for the HTTP API.
- `terraform/main/README.md` with apply instructions.

**Acceptance:**
- `terraform validate` and `terraform plan` (with sensible default vars +
  empty backend creds) succeed.
- Outputs include the public API URL.
- Variables: `region`, `default_models`, `log_retention_days`, `domain_name`,
  `hosted_zone_id`, `lambda_memory_mb`, `lambda_timeout_s`. (`model_allowlist`
  was removed post-deploy when IAM was opened to `*`; the Lambda is now the
  authoritative model gate.)

**Depends on:** T03, T04.

---

## T06 — Admin CLI (`bin/`)

**Tier:** medium

**Description:** Operator-facing CLI written in Python, distributed via
`pyproject.toml` console_scripts. Uses local AWS creds (via boto3 default
chain) to read/write DynamoDB and emit the secret token to stdout exactly
once on issue.

**Subcommands:**
- `issue OWNER --budget USD --rps N --monthly-requests N --max-input-tokens N --max-output-tokens N --models M1,M2 [--note ...]`
  → creates a row in `tokens`, prints the secret bearer token to stdout
  (only place it's ever shown).
- `revoke TOKEN_ID` → flips `status` to `revoked`.
- `list [--status active|revoked|all]` → table view.
- `show TOKEN_ID` → all metadata + current month usage from `usage` table.
- `set-limit TOKEN_ID --budget|--rps|--monthly-requests|--max-input-tokens|--max-output-tokens|--models ...`
- `usage TOKEN_ID [--period YYYY-MM]` → aggregate counts and dollars.

**Deliverables:**
- `cli/bedrock_api/__init__.py`, `cli.py`, `tokens.py`, `formatting.py`.
- `bin/bedrock-api` entrypoint (or rely on `pyproject` console_script
  `bedrock-api = bedrock_api.cli:main`).
- Unit tests with `moto` for DynamoDB.
- `cli/README.md` with examples.

**Acceptance:**
- `bedrock-api --help` lists every subcommand.
- All subcommands have tests for success and at least one failure path.

**Depends on:** T03.

---

## T07 — End-to-end smoke test + docs

**Tier:** low

**Description:** A repeatable `make e2e` flow — issue a token, hit the API
with it via `curl`/`httpx`, check usage rows updated, revoke, hit again
expecting 401. Document the operator workflow in the top-level README.

**Deliverables:**
- `tests/e2e/test_smoke.py` (skipped unless `E2E=1` and AWS creds).
- Updated top-level `README.md` with the deploy + first-token flow.
- A `docs/clients.md` showing how to hit the proxy from `boto3` with a
  custom signer (since clients now use bearer tokens, not SigV4).

**Acceptance:**
- `make e2e` passes against a deployed stack when `E2E=1` is set.
- README has copy-pasteable commands.

**Depends on:** T05, T06.

---

## Dependency graph

```
T01 ──► T02 ──► T03 ──► T04 ──► T05 ──► T07
                  │       │       ▲
                  └──► T06 ───────┘
```

## Intelligence tier summary

| Task | Tier   | Notes                                                       |
| ---- | ------ | ----------------------------------------------------------- |
| T01  | low    | scaffolding                                                  |
| T02  | medium | TF module, careful around bucket naming + lifecycle         |
| T03  | high   | schema choices ripple through proxy and CLI                 |
| T04  | high   | core business logic (auth + limits + Bedrock call)          |
| T05  | medium | TF wiring, no novel logic                                   |
| T06  | medium | CRUD CLI, boto3 + click/typer                               |
| T07  | low    | e2e smoke + docs                                            |

## Agent assignment policy

Per the user's "use mostly copilot" directive and current usage (Copilot
2.4% used / 97.6% remaining; Codex maxed; Claude tight on 5h window),
default agent is **Copilot** for both planning and implementation, model
chosen by tier:

- `low` and `medium` → `claude-sonnet-4.6` on Copilot.
- `high` → `claude-sonnet-4.6` on Copilot with `--effort high`.

Claude Code orchestrator (this agent) reviews each plan and supervises.
