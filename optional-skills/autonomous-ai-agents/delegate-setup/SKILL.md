---
name: delegate-setup
description: Orchestrate Claude Code, Codex, Cursor and other coding CLIs as one fleet.
version: 0.1.0
author: Hermes Agent (pattern after amElnagdy/delegate-skills, MIT)
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Coding-Agent, Orchestration, Multi-Agent, Claude-Code, Codex, Cursor, Delegation]
    related_skills: [claude-code, codex, opencode, openhands]
---

# Delegate Setup — multi-agent coding orchestrator

Hermes acts as the planner. It detects which coding-agent CLIs are installed, splits the task into
sub-problems, gives each one to the best agent in its own git worktree, then merges every agent's
report into one summary. Hermes reviews diffs and runs tests itself; downstream agents never push.

## When to use

- A task splits cleanly into 2+ independent parts (backend + tests + docs, several bug fixes).
- You want a second opinion: the same review run by two different agents.
- Not for a one-file change: do that directly or load `claude-code` / `codex`.

## Step 1 — detect the fleet

```
terminal(command="python3 ~/.hermes/skills/autonomous-ai-agents/delegate-setup/scripts/detect_agents.py --pretty")
```

The JSON lists each installed agent with its headless command, safe flags, permissionless flags and
strengths. If only one agent is installed, fall back to `delegate_task` subagents plus that agent.

## Step 2 — write the routing plan

Write `delegation_plan.md` in the repo root before launching anything:

| # | Sub-task | Agent | Model / effort | Worktree | Done when |
|---|---|---|---|---|---|
| 1 | Add `/export` endpoint | claude-code | default, high effort | `../wt-export` | tests in `tests/api` pass |
| 2 | Write tests for parser | codex | `-c model_reasoning_effort=medium` | `../wt-parser-tests` | `pytest tests/parser` green |

Routing defaults: refactors and design → claude-code; tests and narrow bug fixes → codex;
UI → cursor; anything that should run on a local/free model → opencode or aider. Prefer higher
effort only for the hardest sub-task; cheap models for mechanical work.

## Step 3 — launch, one worktree per agent

```
terminal(command="git worktree add ../wt-export -b delegate/export")
terminal(command="claude -p 'Add /export endpoint. Write REPORT.md with what you changed and test results. Do not push.' --permission-mode acceptEdits --max-turns 30", workdir="../wt-export", background=true, pty=true, timeout=1800)
terminal(command="git worktree add ../wt-parser-tests -b delegate/parser-tests")
terminal(command="codex exec --sandbox workspace-write 'Write tests for src/parser. Write REPORT.md with results. Do not push.'", workdir="../wt-parser-tests", background=true, pty=true, timeout=1800)
```

Every prompt must end with: write `REPORT.md` (changes, commands run, test results, open issues)
and do not push. Poll background jobs with `process(action="poll")`; do not sleep-loop.

### Permission modes — read before using permissionless flags

Default to the **safe flags** from Step 1 (`acceptEdits`, `--sandbox workspace-write`). They cover
most work and stall only on shell commands outside the sandbox.

Permissionless flags (`--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`,
`--yolo`, `--force`, `--yes-always`) remove every approval: the agent can delete files, run any
command and use any credential in the environment. Use them only when ALL of these hold:

1. The user explicitly asked for unattended/permissionless mode in this session.
2. The agent runs in a throwaway worktree or container, not the user's main checkout or `$HOME`.
3. No production credentials are in the environment (unset cloud keys, use a scoped `GH_TOKEN`).

## Step 4 — collect and summarize

```
terminal(command="for d in ../wt-*; do echo \"=== $d\"; git -C $d diff --stat main; cat $d/REPORT.md 2>/dev/null; done")
```

Run the tests yourself in each worktree, then write one `delegation_summary.md`: per sub-task the
agent, result (pass/fail), diff stat, open issues, and your merge recommendation. Merge only green
branches; remove worktrees with `git worktree remove` when done.

## Pitfalls

- Two agents editing the same files in parallel produce conflicts: split by file ownership.
- CLIs change flags often; if a flag is rejected, run `<binary> --help` and adapt.
- Each agent bills its own provider account; tell the user which ones a plan will use.
