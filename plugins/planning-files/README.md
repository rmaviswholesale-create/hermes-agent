# planning-files

Keeps the plan on disk instead of in the chat window. Three files live in the session's working
directory, and a `pre_llm_call` hook appends them to every turn so the agent cannot drift:

| File | Holds |
|---|---|
| `task_plan.md` | goal, phased micro-tasks with checkboxes, decisions, error table |
| `findings.md` | bugs hit, root causes, fixes, learnings (never repeat a failed approach) |
| `progress.md` | timestamped log of what finished, test results, next step |

```bash
hermes plugins enable planning-files
# in a session, inside your project:
/planfiles init     # writes the three templates (never overwrites)
/planfiles status
/planfiles off|on   # pause/resume injection for this process
```

- Nothing is injected until `task_plan.md` exists, so normal chats cost zero tokens.
- Only the current user message is extended; the system prompt is untouched, so prompt caching holds.
- Each file is capped at 6,000 chars (head + tail kept) so a long findings log cannot flood context.
- Pattern from [OthmanAdi/planning-with-files](https://github.com/OthmanAdi/planning-with-files) (MIT).

Tests: `scripts/run_tests.sh tests/plugins/test_planning_files_plugin.py`
