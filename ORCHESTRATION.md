# bedrock-api — Orchestration log

Per-task entries follow the format from
[instructions_orchestrate.md](~/.llm/memory/instructions_orchestrate.md):
task, tier, agent chosen, usage snapshot driving the choice, plan/review
rounds, implementation rounds, outcome.

---

## Starting snapshot — 2026-05-04 18:44 UTC

```json
{
  "claude":   { "five_hour": { "used": 79, "remaining": 21, "resets_in_s":  4542 } },
  "codex":    { "five_hour": { "used": 98, "remaining":  2, "resets_in_s": 10671 } },
  "copilot":  { "month":     { "used":  2, "remaining": 98, "resets_in_s": 2351742 } }
}
```

Default agent: **Copilot** (`gh copilot -- -p ... --allow-all --autopilot
--model claude-sonnet-4.6`). Codex is effectively maxed, Claude (orchestrator)
is tight, Copilot is wide open and the user requested it.

---

## T01 — Repo scaffolding & layout

- **Tier:** low
- **Agent:** Copilot, `claude-sonnet-4.6`
- **Plan/review rounds:** 1 (combined plan+impl for low-tier task)
- **Implementation rounds:** 1
- **Cost:** 1 premium request, 2m 45s, ↑722.9k / ↓7.0k tokens (681.6k cached)
- **Outcome:** ✅ pass — `make lint test` exits 0, `terraform fmt -check -recursive`
  exits 0, commit clean (no attribution).
- **Files added:** [pyproject.toml](./pyproject.toml), [Makefile](./Makefile),
  [.editorconfig](./.editorconfig), [.pre-commit-config.yaml](./.pre-commit-config.yaml),
  [tests/test_smoke.py](./tests/test_smoke.py), [T01_PLAN.md](./T01_PLAN.md),
  placeholder `.gitkeep` files in `terraform/{bootstrap,main}/`,
  `lambda/proxy/`, `cli/bedrock_api/`, `bin/`.

## T02 — Terraform state backend bootstrap

- **Tier:** medium
- **Agent:** Copilot, `claude-sonnet-4.6`, `--effort medium`
- **Plan/review rounds:** plan converged in 1 round; impl review ran 5 rounds (force_destroy debate, lifecycle filter, deletion_protection)
- **Cost:** 1 premium request, 21m 27s, ↑4.7m / ↓86.3k tokens (4.5m cached)
- **Outcome:** ✅ pass — `terraform fmt -check -recursive` and
  `terraform -chdir=terraform/bootstrap validate` both exit 0 (with TF 1.9).
- **Notable additions beyond spec:** TLS-only bucket policy, `prevent_destroy`
  lifecycle on bucket, AWS-level `deletion_protection_enabled` on lock table,
  `force_destroy` deliberately omitted.
- **Files added:** [T02_PLAN.md](./T02_PLAN.md), [terraform/bootstrap/main.tf](./terraform/bootstrap/main.tf),
  [terraform/bootstrap/variables.tf](./terraform/bootstrap/variables.tf),
  [terraform/bootstrap/outputs.tf](./terraform/bootstrap/outputs.tf),
  [terraform/bootstrap/versions.tf](./terraform/bootstrap/versions.tf),
  [terraform/bootstrap/README.md](./terraform/bootstrap/README.md).

## T03 — DynamoDB schema & TF module for tokens / usage / rate-limit

- **Tier:** high
- **Agent:** Copilot, `claude-sonnet-4.6`, `--effort high`
- **Plan/review rounds:** plan converged in 1 round; impl review converged in 2 rounds (no actionable findings round 2)
- **Cost:** 1 premium request, 23m 16s, ↑6.0m / ↓97.1k tokens (5.6m cached)
- **Outcome:** ✅ pass — `terraform fmt -check -recursive` and
  `terraform validate` both 0 (with TF 1.11). Three tables provisioned.
- **Key design decisions** (from commit message):
  - Token format: `bk_<32hex>.<64hex>`; dot splits public `token_id` from secret.
  - Secret hashing: SHA-256 + 16-byte per-token salt; `hmac.compare_digest`.
  - Usage counters: single `UpdateItem` `ADD` (atomic, no transaction needed).
  - Rate limiting: separate table, second-bucketed, TTL auto-cleanup,
    `attribute_not_exists` guard + explicit `limit=0` pre-check.
  - Absent attribute = unlimited; `0` = block all.
  - Period = UTC `YYYY-MM` string (natural monthly rollover).
  - Status: `active` / `revoked` with `revoked_at` timestamp; no hard-delete.
  - GSI on `owner` (range `created_at`) for CLI `list` efficiency.
  - PITR + SSE on `tokens` and `usage`; `rate_limit` is ephemeral
    (no PITR, no deletion protection — so `terraform destroy` works).
- **Files added:** [T03_PLAN.md](./T03_PLAN.md), [terraform/main/modules/data/main.tf](./terraform/main/modules/data/main.tf),
  [variables.tf](./terraform/main/modules/data/variables.tf),
  [outputs.tf](./terraform/main/modules/data/outputs.tf),
  [versions.tf](./terraform/main/modules/data/versions.tf),
  [README.md](./terraform/main/modules/data/README.md) (365 lines —
  authoritative for T04 + T06 implementation).
