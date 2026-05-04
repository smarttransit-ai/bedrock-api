# T01 — Repo Scaffolding & Layout

## Problem Summary

Stand up the skeleton that every later task depends on: directory layout,
Python toolchain config, developer Makefile, pre-commit hooks, and a smoke
test so `make lint test` exits 0 on a fresh checkout.

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Python version | 3.12 | Latest Lambda-supported runtime |
| Build tool | `make` | Simpler, no extra install; requested explicitly |
| Linter/formatter | `ruff` | Fast, replaces flake8+isort+black in one tool |
| Test runner | `pytest` | Standard; used in later tasks |
| AWS mocking | `moto` | Needed for T04/T06 unit tests; declare early |
| Pre-commit | ruff + terraform fmt | Matches task spec |
| Placeholder dirs | `.gitkeep` | Ensures dirs tracked by git without stubs |

## Files to Create

1. `T01_PLAN.md` — this file
2. `pyproject.toml` — Python 3.12, ruff + pytest + boto3 + moto
3. `Makefile` — `fmt`, `lint`, `test`, `package`, `tf-init`, `tf-plan`, `tf-apply`
4. `.editorconfig` — basic editor consistency
5. `.pre-commit-config.yaml` — ruff format, ruff check, terraform fmt -recursive
6. `terraform/bootstrap/.gitkeep`
7. `terraform/main/.gitkeep`
8. `lambda/proxy/.gitkeep`
9. `cli/bedrock_api/.gitkeep`
10. `bin/.gitkeep`
11. `tests/test_smoke.py` — `def test_truthy(): assert True`

## Acceptance Validation

- `make lint test` → exits 0
- `terraform fmt -check -recursive` → exits 0 (no .tf files yet)
