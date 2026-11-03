#!/usr/bin/env bash
# Install the "six skills" pack into Hermes. Every step is idempotent and fail-soft: one failure
# never stops the rest. A JSON summary prints at the end (and to $HERMES_HOME/skill-pack.json).
#
#   ./deploy/skill-pack/install.sh                    # all free/default steps
#   ./deploy/skill-pack/install.sh --with-agent-reach # also install Agent Reach (scraping; read README)
#   ./deploy/skill-pack/install.sh --only rtk,skill-retrieval
#   ./deploy/skill-pack/install.sh --dry-run
set -uo pipefail

STEPS_DEFAULT="planning-files,delegate-setup,rtk,mantis,skill-retrieval"
ONLY=""; DRY=0; WITH_REACH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --with-agent-reach) WITH_REACH=1; shift ;;
    -h|--help) sed -n 2,9p "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
STEPS="${ONLY:-$STEPS_DEFAULT}"
[ "$WITH_REACH" = 1 ] && [ -z "$ONLY" ] && STEPS="$STEPS,agent-reach"

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
RESULTS=()

run() {  # run <cmd...>  — echo, honour --dry-run, return the command's status
  echo "+ $*"
  [ "$DRY" = 1 ] && return 0
  "$@"
}

retry() {  # retry <cmd...> — 3 attempts, 2s/4s backoff, for network-bound steps
  local n=0 delay=2
  until run "$@"; do
    n=$((n + 1)); [ $n -ge 3 ] && return 1
    echo "  retry $n in ${delay}s"; sleep "$delay"; delay=$((delay * 2))
  done
}

record() { RESULTS+=("{\"step\":\"$1\",\"status\":\"$2\",\"note\":\"$3\"}"); }

need_hermes() {
  command -v hermes >/dev/null 2>&1 || { echo "hermes not on PATH; run setup-hermes.sh first" >&2; exit 1; }
}

step_planning_files() {
  run hermes plugins enable planning-files && record planning-files ok "use /planfiles init in a project" \
    || record planning-files failed "hermes plugins enable planning-files"
}

step_delegate_setup() {
  retry hermes skills install official/autonomous-ai-agents/delegate-setup --yes \
    && record delegate-setup ok "safe flags by default; see SKILL.md before permissionless mode" \
    || record delegate-setup failed "hermes skills install official/autonomous-ai-agents/delegate-setup"
}

step_rtk() {
  if ! command -v rtk >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      retry brew install rtk
    else
      # Upstream installer (rtk-ai/rtk). Inspect first if you prefer: curl -fsSL <url> | less
      retry sh -c 'curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh'
    fi
  fi
  if [ "$DRY" = 0 ] && ! rtk gain >/dev/null 2>&1; then
    record rtk failed "rtk binary missing or wrong 'rtk' (crates.io name clash); see https://github.com/rtk-ai/rtk"
    return
  fi
  retry hermes plugins install rtk-rewrite --enable \
    && record rtk ok "terminal commands now rewritten through rtk; 'rtk gain' shows savings" \
    || record rtk failed "hermes plugins install rtk-rewrite --enable"
}

step_mantis() {
  local ok=1
  retry hermes skills install official/security/mantis-audit --yes || ok=0
  for s in mantis-plan mantis-reproduce mantis-patch mantis-advise; do
    retry hermes skills install "google/mantis/skills/$s" --yes || { ok=0; echo "  could not install $s"; }
  done
  [ $ok = 1 ] && record mantis ok "schedule with the cron example in mantis-audit SKILL.md" \
    || record mantis partial "some upstream skill names may have changed; check github.com/google/mantis/tree/main/skills"
}

step_skill_retrieval() {
  retry hermes plugins install skill-retrieval --enable \
    && record skill-retrieval ok "BM25 top-K skill descriptions per turn; restart session to apply" \
    || record skill-retrieval failed "hermes plugins install skill-retrieval --enable"
}

step_agent_reach() {
  command -v uv >/dev/null 2>&1 || retry sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  export PATH="$HOME/.local/bin:$PATH"
  retry uv tool install --force https://github.com/Panniantong/Agent-Reach/releases/latest/download/agent-reach.zip \
    && run agent-reach setup \
    && record agent-reach ok "run 'agent-reach doctor'; logins use your own cookies (see README risks)" \
    || record agent-reach failed "see https://github.com/Panniantong/Agent-Reach"
}

need_hermes
IFS=',' read -r -a WANT <<< "$STEPS"
for s in "${WANT[@]}"; do
  echo "== $s"
  case "$s" in
    planning-files) step_planning_files ;;
    delegate-setup) step_delegate_setup ;;
    rtk) step_rtk ;;
    mantis) step_mantis ;;
    skill-retrieval) step_skill_retrieval ;;
    agent-reach) step_agent_reach ;;
    *) record "$s" failed "unknown step" ;;
  esac
done

JSON="{\"dry_run\":$([ $DRY = 1 ] && echo true || echo false),\"results\":[$(IFS=,; echo "${RESULTS[*]}")]}"
echo "$JSON"
[ "$DRY" = 0 ] && mkdir -p "$HERMES_HOME" && echo "$JSON" > "$HERMES_HOME/skill-pack.json"
echo "$JSON" | grep -q '"status":"failed"' && exit 1 || exit 0
