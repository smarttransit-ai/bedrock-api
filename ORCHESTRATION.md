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
