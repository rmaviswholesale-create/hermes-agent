# Hermes on a free always-on VM (Telegram, 24/7)

Run the Hermes gateway on a free cloud VM so the Telegram bot answers around
the clock, with nothing running on your own computer or phone.

The gateway uses long polling. It only makes **outbound** connections to
Telegram and your model provider, so you need no domain, open port, firewall
rule or TLS certificate.

## Which free host

| Host | Free resources | Card needed | Fits Hermes? | Gotchas |
|---|---|---|---|---|
| **Google Cloud e2-micro** (recommended) | 2 shared vCPU, **1 GB RAM**, 30 GB standard disk, us-west1/us-central1/us-east1 only | Yes (identity check, $300/90-day trial) | Yes for the gateway. It idles at ~250 MB RSS; the script adds 2 GB swap. No browser tool | The in-use external IPv4 is **not** in the free tier: about $0.005/h (~$3.65/mo) after trial credit runs out. Disk must be *standard* (console defaults to billed "balanced") |
| **Oracle Cloud Always Free (Ampere A1)** | 2 OCPU, 12 GB RAM (cut from 4/24 on 2026-06-15), 200 GB disk | Yes | Best specs; can also run the browser tool | A1 capacity is often "out of host capacity" in popular regions. Idle free-tier instances can be reclaimed (keep it busy or upgrade to Pay-As-You-Go, which stays $0 inside the limits) |
| **Modal** | Monthly free credit on the Starter plan | No for the credit | Yes (see `hermes-restore/modal`) | Always-on 0.5 CPU/2 GB is roughly the whole monthly credit; not a guaranteed $0 |
| **Claude cloud sandbox** | n/a | n/a | No | Ephemeral container, reclaimed when idle, and its network policy blocks api.telegram.org |

Pick GCP if you want the simplest path on the Google account you already
have. Pick Oracle if you want headroom (browser automation, local tools) and
can get A1 capacity. `bootstrap.sh` works on both.

## Model cost

Hosting is free; the model API is separate. Free options:

- **OpenRouter** models ending in `:free` (rate-limited; a one-time $10 credit raises the daily cap).
- **Google AI Studio** (Gemini) free tier: small, fine for light personal use.

A fresh install sets a paid default model. `bootstrap.sh` offers the model
picker; choose a free model there, or later with `hermes model`.

## Before you start (on your phone or laptop)

1. **Telegram bot token:** open [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token.
2. **Your Telegram user id:** message [@userinfobot](https://t.me/userinfobot). Only this id will be allowed to use the bot.
3. **Model key:** [openrouter.ai/keys](https://openrouter.ai/keys) or [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

You type these on the VM when the script asks. They are stored only in
`~/.hermes/.env` on the VM (mode 600). Don't paste them into chats or commit them.

## Deploy on Google Cloud (about 15 minutes)

```bash
# 1. Open Cloud Shell (free, gcloud preinstalled): https://shell.cloud.google.com
gcloud config set project YOUR_PROJECT_ID
curl -fsSL https://raw.githubusercontent.com/rmaviswholesale-create/hermes-agent/main/deploy/free-vps/gcp-create-vm.sh | bash

# 2. SSH into the VM
gcloud compute ssh hermes --zone us-central1-a

# 3. On the VM: install Hermes, enter keys, pick a model, start the service
curl -fsSL https://raw.githubusercontent.com/rmaviswholesale-create/hermes-agent/main/deploy/free-vps/bootstrap.sh -o bootstrap.sh
bash bootstrap.sh
```

Then send "hi" to your bot in Telegram.

Set a $1 budget alert at <https://console.cloud.google.com/billing/budgets> so
any charge outside the free tier emails you.

## Deploy on Oracle Cloud

1. Sign up at <https://signup.oracle.com>, pick a home region with A1 capacity (it can't be changed later).
2. Create an instance: shape **VM.Standard.A1.Flex**, 2 OCPU / 12 GB, image Ubuntu 24.04, add your SSH key. No ingress rules needed.
3. `ssh ubuntu@<public-ip>`, then step 3 above.

## Restore the old VPS backup (optional)

Copy the redacted backup to the VM and pass it to the script. It never
overwrites existing files, renames the old `config.yaml` to
`config.vps-old.yaml` (it pointed at a dead local proxy), and never restores
old `.env` secrets.

```bash
# from Cloud Shell (upload the tarball there first via the ⋮ menu → Upload)
gcloud compute scp hermes-vps-2026-10-05-redacted.tar.gz hermes:~ --zone us-central1-a
# on the VM
bash bootstrap.sh --restore hermes-vps-2026-10-05-redacted.tar.gz
```

## Non-interactive / automated

```bash
export TELEGRAM_BOT_TOKEN=... TELEGRAM_ALLOWED_USERS=... OPENROUTER_API_KEY=...
export HERMES_PROVIDER=openrouter HERMES_MODEL=<a :free model id>
bash bootstrap.sh --non-interactive
```

## Operate

```bash
hermes gateway status                    # up?
journalctl --user -u hermes-gateway -f   # live logs
hermes gateway restart                   # after editing ~/.hermes/.env or config.yaml
hermes model                             # switch model
hermes update                            # pull latest code from this fork
```

The gateway is a systemd **user** service with linger enabled, so it starts at
boot without anyone logged in and restarts on crash.

## What the script does

1. Adds a 2 GB swapfile when RAM is under 2 GB.
2. Installs `git`, `curl`, `xz`.
3. Runs this fork's `scripts/install.sh --skip-setup --skip-browser --skip-computer-use` (Chromium needs more RAM than an e2-micro has; add it later on a bigger box with `hermes pm install chromium`).
4. Optionally restores a backup tarball without overwriting anything.
5. Writes Telegram and model keys to `~/.hermes/.env` (prompts hidden, mode 600).
6. Offers `hermes model`.
7. `loginctl enable-linger` + `hermes gateway install --start-now --start-on-login`.

Disk use after install is about 2.6 GB.
