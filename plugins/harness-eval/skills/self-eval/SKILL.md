---
name: self-eval
description: Revise or retire skills that measurably fail.
version: 0.1.0
author: Rico (@rmaviswholesale-create)
license: MIT
metadata:
  hermes:
    category: autonomous-ai-agents
    tags: [self-improvement, skills, evaluation, curator]
---

# Harness self-eval

Hermes records every tool result and every skill load (the `harness-eval` plugin). This procedure
turns that evidence into changes: keep what works, revise what fails, archive what still fails after
a revision. Evidence beats opinion: only act on rows the report marks `revise` or `archive`.

## Procedure

1. Run `hermes harness-eval report --json` with `terminal` and parse the JSON.
2. For each skill whose `action` is `revise`:
   - Load it with `skill_view` and read its `top_errors` and `reason` from the report.
   - Find the step that leads to those errors (wrong command, stale path, missing precondition,
     a tool the skill names that is `flaky` in the report's `tools` list).
   - Fix that step with `skill_manage` (`patch`). Keep the change small and say in the skill what
     changed and why. A patch starts a fresh trial: older failures stop counting against it.
   - If the skill cannot be fixed (its whole approach is wrong), do not patch it; it will be
     archived on the next failing verdict.
3. For skills whose `action` is `archive`, run `hermes harness-eval prune --json` first (dry run),
   then `hermes harness-eval prune --apply --json`. Pinned, bundled and hub skills are refused by the
   curator; leave them and mention them in the summary.
4. For `tools` with `verdict: flaky`, do not change configuration. List them with their top errors so
   the user can decide (credentials, rate limits and network failures usually show up here).
5. Reply with a short summary: skills revised (one line each, what changed), skills archived,
   flaky tools, and how many skills are still `wait` (not enough evidence yet).

## Rules

- Never delete a skill; archive is the maximum and `hermes curator restore <name>` undoes it.
- Never patch a skill whose action is `keep` or `wait`.
- One patch per skill per run, so the next window measures one change.
