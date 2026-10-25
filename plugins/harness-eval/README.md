# harness-eval

Outcome scoring for Hermes's own harness. Hermes already learns skills (background review) and
prunes them by **inactivity** (curator). This plugin adds the missing signal: **did loading the skill
help?** It records results, scores them against the profile's baseline, revises what fails and
archives what fails twice.

```bash
hermes plugins enable harness-eval
hermes harness-eval report            # human summary, last 30 days
hermes harness-eval report --json     # for agents / cron
hermes harness-eval prune             # dry run: skills that failed again after a revision
hermes harness-eval prune --apply     # archive them via `hermes curator archive`
hermes harness-eval vacuum --days 90  # drop old events
```

In a session: `/harness-eval`, `/harness-eval good <skill>`, `/harness-eval bad <skill> [why]`.
Schedule the loop with `/blueprint` → **Weekly harness self-eval** (runs the `harness-eval:self-eval` skill).

## How it scores

| Signal | Source |
|---|---|
| Tool call ok/error, error text, duration | `post_tool_call` |
| Skill loaded | `on_skill_lifecycle` action `loaded` |
| Skill revised (created / patched / edited) | `on_skill_lifecycle` |
| Explicit verdicts | `/harness-eval good\|bad <skill>` |

- Each non-meta tool call made after a skill load, by the same task, is credited to that skill
  (up to 25 calls per load). Skill bookkeeping tools (`skill_view`, `skill_manage`, `memory`, ...) are
  never credited.
- Only evidence newer than the skill's last revision counts, so a patch is a fresh trial.
- A skill is `underperforming` when the 95% Wilson **upper** bound of its post-load success rate is more
  than 15 points under the baseline (≥ 10 calls over ≥ 3 loads), or it has ≥ 2 bad verdicts and more bad
  than good. First time: `revise`. After a revision: `archive`.
- A tool is `flaky` when the Wilson **lower** bound of its error rate is ≥ 30% over ≥ 10 calls.

## Data

`$HERMES_HOME/harness-eval/outcomes.db` (SQLite, WAL), one per profile. Error text is clipped to 240
characters. Nothing is sent anywhere.
