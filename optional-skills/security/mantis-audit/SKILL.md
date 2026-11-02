---
name: mantis-audit
description: Recurring security reviews with Google's Mantis skills plus Hermes cron.
version: 0.1.0
author: Hermes Agent (wraps google/mantis, Apache-2.0)
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Security, Code-Review, Threat-Model, Cron, Mantis, AppSec]
    related_skills: [web-pentest, oss-forensics]
---

# Mantis audit — scheduled security review

[google/mantis](https://github.com/google/mantis) (Apache-2.0) is a set of agent skills for security
review: threat model and plan (`mantis-plan`), research and review, reproduce
(`mantis-reproduce`), patch (`mantis-patch`) and report, plus `/mantis-advise` for secure-coding
guidance. Google marks it a demonstration project, not an officially supported product.

## Install the upstream skills (once)

```
hermes skills install google/mantis/skills/mantis-plan
hermes skills install google/mantis/skills/mantis-reproduce
hermes skills install google/mantis/skills/mantis-patch
hermes skills install google/mantis/skills/mantis-advise
```

If a name is rejected, upstream renamed it: list `skills/` in the repo and install what is there.
The video calls the review step "Mantis Reflect"; the verified skill names are the ones above.

## Run one review

1. Load `mantis-plan` on the target repo. It maps the attack surface and writes `workspace/plan.json`.
2. Review each planned area; for every candidate finding, load `mantis-reproduce` to confirm it with
   a failing test or PoC. Drop anything that does not reproduce.
3. Load `mantis-patch` for confirmed findings; run the project's tests after each patch.
4. Write `security-report.md`: finding, severity, file:line, reproduction, patch status.

Safety: reproduction executes generated code. Run reviews in a container or throwaway clone, never
against production hosts, and only on code you own or are authorized to test.

## Schedule it (weekly audit to Slack)

```
hermes cron create "every 1w at 03:00" \
  "Run a Mantis security review of this repo: plan, review, reproduce, patch. Report only confirmed findings with severity and file:line; say 'no confirmed findings' if none." \
  --skill mantis-audit --skill mantis-plan --skill mantis-reproduce --skill mantis-patch \
  --workdir ~/audits/my-app --deliver slack --name "Weekly Mantis audit"
```

Keep `~/audits/my-app` a separate clone that a pre-run `git pull` refreshes, so patches never land
on your working checkout. Patches stay local for you to review; the cron job must not push.
