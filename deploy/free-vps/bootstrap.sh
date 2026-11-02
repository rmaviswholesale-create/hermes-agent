#!/usr/bin/env bash
# Install Hermes Agent on a small always-on Linux VM and run the messaging
# gateway (Telegram) as a systemd service that survives reboots.
#
# Tested target: Debian 12 / Ubuntu 22.04+ on a 1 GB Google Cloud e2-micro or an
# Oracle Cloud Always Free instance. Run as your normal (sudo-capable) user,
# not as root:
#
#   bash bootstrap.sh                                  # install + prompt for keys
#   bash bootstrap.sh --restore hermes-backup.tar.gz   # also restore an old HERMES_HOME backup
#   bash bootstrap.sh --non-interactive                # no prompts; keys must already be
#                                                      # in $HERMES_HOME/.env or exported
#
# Environment overrides:
#   HERMES_REPO_URL   git source (default: this fork)
#   HERMES_BRANCH     branch (default: main)
#   HERMES_HOME       data dir (default: ~/.hermes)
#   SWAP_SIZE_MB      swap to add when RAM < 2 GB and no swap exists (default: 2048)
#   TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USERS / OPENROUTER_API_KEY / GOOGLE_API_KEY
#                     if exported, written to .env without prompting
#   HERMES_MODEL / HERMES_PROVIDER
#                     if exported, set model.default / model.provider without the picker
#
# Re-running is safe: every step checks before it changes anything, and
# existing .env values are kept unless you type a new one.
set -euo pipefail

HERMES_REPO_URL="${HERMES_REPO_URL:-https://github.com/rmaviswholesale-create/hermes-agent.git}"
HERMES_BRANCH="${HERMES_BRANCH:-main}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-2048}"
RESTORE_TARBALL=""
INTERACTIVE=true
[ -t 0 ] || INTERACTIVE=false

while [ $# -gt 0 ]; do
  case "$1" in
    --restore) RESTORE_TARBALL="${2:?--restore needs a path}"; shift 2 ;;
    --non-interactive) INTERACTIVE=false; shift ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

export HERMES_HOME
export PATH="$HOME/.local/bin:$PATH"
ENV_FILE="$HERMES_HOME/.env"

step() { printf '\n\033[0;36m→ %s\033[0m\n' "$*"; }
ok()   { printf '\033[0;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[0;33m⚠ %s\033[0m\n' "$*"; }
die()  { printf '\033[0;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run as your normal user (the gateway runs as a user service), not root"
command -v sudo >/dev/null || die "sudo is required"
command -v systemctl >/dev/null || die "systemd is required (use 'hermes gateway run' under Docker instead)"

# --- 1. swap: the gateway idles at ~250 MB, but installs and tool runs spike --
step "Checking memory and swap"
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
swap_mb=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 2000 ] && [ "$swap_mb" -lt 512 ]; then
  if [ ! -f /swapfile ]; then
    sudo fallocate -l "${SWAP_SIZE_MB}M" /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_SIZE_MB" status=none
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
  fi
  sudo swapon /swapfile 2>/dev/null || true
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-hermes-swap.conf >/dev/null
  sudo sysctl -q -p /etc/sysctl.d/99-hermes-swap.conf || true
  ok "added ${SWAP_SIZE_MB} MB swap (RAM ${mem_mb} MB)"
else
  ok "RAM ${mem_mb} MB, swap ${swap_mb} MB, nothing to do"
fi

# --- 2. OS packages ------------------------------------------------------------
step "Installing OS packages"
if command -v apt-get >/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 install -y -qq \
    git curl ca-certificates xz-utils tar >/dev/null
elif command -v dnf >/dev/null; then
  sudo dnf install -y -q git curl ca-certificates xz tar
else
  warn "unknown package manager; make sure git, curl and xz are installed"
fi
ok "git $(git --version | awk '{print $3}'), curl present"

# --- 3. Hermes -----------------------------------------------------------------
step "Installing Hermes from $HERMES_REPO_URL ($HERMES_BRANCH)"
installer="$(mktemp)"
trap 'rm -f "$installer"' EXIT
raw_base="${HERMES_REPO_URL%.git}"
raw_base="${raw_base/github.com/raw.githubusercontent.com}"
curl -fsSL --retry 3 "$raw_base/$HERMES_BRANCH/scripts/install.sh" -o "$installer" \
  || die "could not download install.sh from $raw_base"
# Browser + computer-use are skipped: Chromium alone needs more RAM than an
# e2-micro has. Add later with `hermes pm install chromium` on a bigger box.
HERMES_REPO_URL="$HERMES_REPO_URL" bash "$installer" \
  --branch "$HERMES_BRANCH" --skip-setup --skip-browser --skip-computer-use
command -v hermes >/dev/null || die "hermes not on PATH after install (expected ~/.local/bin/hermes)"
ok "$(hermes --version 2>/dev/null | head -1)"

# --- 4. optional: restore an old HERMES_HOME backup ----------------------------
if [ -n "$RESTORE_TARBALL" ]; then
  step "Restoring $RESTORE_TARBALL into $HERMES_HOME (never overwrites)"
  [ -f "$RESTORE_TARBALL" ] || die "no such file: $RESTORE_TARBALL"
  tmp="$(mktemp -d)"
  tar -xzf "$RESTORE_TARBALL" -C "$tmp" --no-same-owner
  # The old config pointed at a dead local proxy: keep it for reference only.
  if [ -f "$tmp/.hermes/config.yaml" ]; then
    mv "$tmp/.hermes/config.yaml" "$tmp/.hermes/config.vps-old.yaml"
  fi
  # Secrets from the old box are never restored; set them fresh below.
  rm -f "$tmp/.env" "$tmp/.hermes/.env"
  mkdir -p "$HERMES_HOME"
  before=$(find "$HERMES_HOME" -type f | wc -l)
  if cp --help 2>&1 | grep -q -- '--update\[=UPDATE\]'; then
    cp -r --update=none "$tmp/." "$HERMES_HOME/"
  else
    cp -rn "$tmp/." "$HERMES_HOME/"
  fi
  after=$(find "$HERMES_HOME" -type f | wc -l)
  rm -rf "$tmp"
  ok "restored $((after - before)) new files; existing files left untouched"
fi

# --- 5. secrets -> $HERMES_HOME/.env (mode 600) -------------------------------
step "Configuring secrets in $ENV_FILE"
mkdir -p "$HERMES_HOME"
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

env_get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
env_set() {
  local key="$1" val="$2" tmpf
  tmpf="$(mktemp "$HERMES_HOME/.env.XXXXXX")"
  grep -vE "^$key=" "$ENV_FILE" > "$tmpf" || true
  printf '%s=%s\n' "$key" "$val" >> "$tmpf"
  chmod 600 "$tmpf"
  mv "$tmpf" "$ENV_FILE"
}
# ask KEY "prompt" secret|plain
ask() {
  local key="$1" prompt="$2" mode="$3" current exported val
  current="$(env_get "$key")"
  exported="${!key:-}"
  if [ -n "$exported" ]; then env_set "$key" "$exported"; ok "$key set from environment"; return; fi
  if ! $INTERACTIVE; then
    [ -n "$current" ] && ok "$key already set" || warn "$key not set"
    return
  fi
  local hint="empty to skip"
  [ -n "$current" ] && hint="Enter keeps the current value"
  if [ "$mode" = secret ]; then
    read -rsp "$prompt ($hint): " val; echo
  else
    read -rp "$prompt ($hint): " val
  fi
  if [ -n "$val" ]; then env_set "$key" "$val"; ok "$key saved"
  elif [ -n "$current" ]; then ok "$key kept"
  fi
}

ask TELEGRAM_BOT_TOKEN     "Telegram bot token from @BotFather" secret
ask TELEGRAM_ALLOWED_USERS "Your numeric Telegram user id (from @userinfobot)" plain
ask OPENROUTER_API_KEY     "OpenRouter API key (openrouter.ai/keys)" secret
ask GOOGLE_API_KEY         "Google AI Studio key (aistudio.google.com/apikey)" secret

[ -n "$(env_get TELEGRAM_BOT_TOKEN)" ] || die "TELEGRAM_BOT_TOKEN is required. Re-run, or add it to $ENV_FILE"
[ -n "$(env_get TELEGRAM_ALLOWED_USERS)" ] \
  || warn "TELEGRAM_ALLOWED_USERS is empty: anyone who finds the bot can use your agent"
if [ -z "$(env_get OPENROUTER_API_KEY)" ] && [ -z "$(env_get GOOGLE_API_KEY)" ]; then
  warn "no model key set; the bot will connect but cannot answer until you add one"
fi

# --- 6. model ------------------------------------------------------------------
step "Choosing the model"
current_model="$(hermes config get model.default 2>/dev/null | tail -1 || true)"
case "$current_model" in *"not set"*|None) current_model="" ;; esac
if [ -n "${HERMES_MODEL:-}" ]; then
  [ -n "${HERMES_PROVIDER:-}" ] && hermes config set model.provider "$HERMES_PROVIDER" >/dev/null
  hermes config set model.default "$HERMES_MODEL" >/dev/null
  ok "model set from environment: ${HERMES_PROVIDER:+$HERMES_PROVIDER / }$HERMES_MODEL"
elif $INTERACTIVE; then
  # A fresh install ships a paid default model; always offer the picker.
  echo "Current model: ${current_model:-none}"
  echo "For zero cost pick OpenRouter and a model ending in ':free' (or Google AI Studio)."
  read -rp "Open the model picker now? [Y/n]: " yn
  case "${yn:-y}" in
    [nN]*) ok "keeping ${current_model:-none}" ;;
    *) hermes model || warn "model picker exited; run 'hermes model' later" ;;
  esac
else
  warn "model is '${current_model:-none}' (install default, may be paid). Run 'hermes model' or set HERMES_MODEL"
fi

# --- 7. gateway as a boot-time service -----------------------------------------
step "Installing the gateway service"
sudo loginctl enable-linger "$USER"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
for _ in $(seq 1 10); do [ -S "$XDG_RUNTIME_DIR/bus" ] && break; sleep 1; done
hermes gateway install --force --start-now --start-on-login
sleep 8
hermes gateway status || true

cat <<EOF

$(ok "Done. Hermes gateway is installed as a systemd user service with linger, so it starts at boot.")

Test it: open Telegram, find your bot, send "hi".

Useful commands on this VM:
  hermes gateway status            # is it up?
  journalctl --user -u hermes-gateway -f   # live logs
  hermes gateway restart           # after editing $ENV_FILE or config
  hermes model                     # switch model/provider
  hermes update                    # pull the latest code from $HERMES_BRANCH
EOF
