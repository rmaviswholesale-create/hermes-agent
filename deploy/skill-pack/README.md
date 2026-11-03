# Skill pack: the six "10x" Hermes skills

One installer for the six skills from the video *Hermes Agent Skills That Make It 10x More
Powerful*. Each was traced to its real open-source project; what Hermes already had is reused,
and the two gaps (planning re-injection, multi-CLI delegation) are new code in this repo.

```bash
./deploy/skill-pack/install.sh --dry-run           # show every command
./deploy/skill-pack/install.sh                     # 5 default steps
./deploy/skill-pack/install.sh --with-agent-reach  # + Agent Reach (read the risks below first)
./deploy/skill-pack/install.sh --only rtk,skill-retrieval
cat ~/.hermes/skill-pack.json                      # {"results":[{"step","status","note"}]}
```

Steps are idempotent and fail-soft (3 attempts with backoff on network steps); exit code 1 if any
step failed. Start a new session afterwards: skills and prompt changes apply at session start.

## What each step installs

| # | Skill | Real project (license) | How it lands in Hermes | Cost |
|---|---|---|---|---|
| 1 | Planning with Files | [OthmanAdi/planning-with-files](https://github.com/OthmanAdi/planning-with-files) (MIT) | **New** `plugins/planning-files`: `pre_llm_call` re-injects `task_plan.md` / `findings.md` / `progress.md` every turn; `/planfiles init` | free |
| 2 | Delegate Setup | [amElnagdy/delegate-skills](https://github.com/amElnagdy/delegate-skills) (MIT) | **New** `optional-skills/autonomous-ai-agents/delegate-setup`: `detect_agents.py` finds Claude Code / Codex / Cursor / OpenCode / Gemini / Aider / ...; one worktree per agent; merged `delegation_summary.md` | free (each CLI bills its own account) |
| 3 | RTK | [rtk-ai/rtk](https://github.com/rtk-ai/rtk) (CLI Apache-2.0; engine BUSL-1.1, free for own use) | existing catalog plugin `rtk-rewrite` (`pre_tool_call`, fail-open) + rtk binary | free |
| 4 | Mantis | [google/mantis](https://github.com/google/mantis) (Apache-2.0) | upstream skills via `hermes skills install google/mantis/skills/...` + **new** `optional-skills/security/mantis-audit` (weekly cron to Slack) | free + model tokens |
| 5 | Agent Reach | [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) (MIT) | opt-in `--with-agent-reach`: `uv tool install` + `agent-reach setup` | free (server use may need a ~$1/mo proxy) |
| 6 | Skill Retriever | [moonlight-lupin/agent-skills](https://github.com/moonlight-lupin/agent-skills) `skill-retrieval` | existing catalog plugin `skill-retrieval`: skills list cut to names, top-K descriptions per turn | free (rerank off, no network) |

## Verified vs. the video

- **Skill Retriever is BM25 (keyword) ranking, not vector embeddings.** Same effect on prompt size;
  no embedding model needed. The video's 11k → 2.3k token figure was not measured here.
- **"Mantis Reflect" was not found** as a skill name. Verified upstream skills: `mantis-plan`,
  `mantis-reproduce`, `mantis-patch`, `mantis-advise`. Google labels Mantis a demonstration
  project, not a supported product. The installer reports `partial` if names have moved.
- **RTK in Hermes** goes through the community `rtk-rewrite` plugin, not upstream rtk-ai.
  `rtk init` targets Claude Code/Codex/Cursor, not Hermes.

## Risks to know

- **Permissionless delegation.** Delegate Setup defaults to safe flags (`--permission-mode acceptEdits`,
  `codex --sandbox workspace-write`). The video's "permissionless mode"
  (`--dangerously-skip-permissions`, `--yolo`, ...) lets a downstream agent run any command with your
  credentials. The skill allows it only when you ask in-session, inside a throwaway worktree or
  container, with no production keys in the environment.
- **Agent Reach and site terms.** Reading Reddit/X/Instagram/Facebook through logged-in cookies or
  scrapers is against most of those sites' terms and can get the account rate-limited or banned. Use a
  secondary account, keep volume low, prefer official/free APIs (Reddit's JSON endpoints, GitHub API)
  where they exist, and never resell scraped personal data. That is why it is opt-in.
- **RTK rewrites every terminal command** before approval. It is fail-open (2 s timeout), but a
  filtered output can hide a line you needed: `rtk bypass <cmd>` runs one command raw.
- **Mantis executes generated PoC code.** Run audits on a separate clone or container, only on code
  you own.
- **Skill retrieval can miss a skill** whose description shares no words with the request. Ask for it by
  name when that happens.

## Running Hermes on an NVIDIA T4 (16 GB)

Yes, with a mid-size quantized model. The hard constraint is Hermes' **64k-token minimum context**
(`agent/model_metadata.py`), so most of the VRAM goes to KV cache, not weights.

| Model (Ollama tag) | Weights (Q4_K_M) | 64k KV cache (q8_0) | Fits 16 GB? | Use |
|---|---|---|---|---|
| `qwen3:8b` | ~5 GB | ~5 GB | yes, comfortable | best default: solid tool calling |
| `qwen3:14b` | ~9 GB | ~5.5 GB | yes, tight | stronger reasoning, slower |
| `gpt-oss:20b` | ~13 GB (MXFP4) | small (sliding window) | borderline | try it; drop context if OOM |
| 30B+ dense | 17 GB+ | n/a | no | needs 24 GB+ |

```bash
# Ollama on the T4
OLLAMA_CONTEXT_LENGTH=65536 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve &
ollama pull qwen3:8b
hermes config set model.provider custom
hermes config set model.base_url http://localhost:11434/v1
hermes config set model.default qwen3:8b
hermes config set model.context_length 65536

# or vLLM (T4 = Turing sm_75: fp16 + AWQ/GPTQ, no bf16, no FP8)
vllm serve Qwen/Qwen3-8B-AWQ --dtype half --max-model-len 65536 \
  --gpu-memory-utilization 0.92 --enable-auto-tool-choice --tool-call-parser hermes
```

Expect roughly 20-40 tok/s on an 8B Q4 model. Local 8-14B models handle cron jobs, triage and
simple tool loops well, but long multi-step coding is noticeably weaker than frontier APIs. A good
split: the T4 model for `cron.model` and auxiliary tasks, a hosted model for the main chat.
