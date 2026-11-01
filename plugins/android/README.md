# Android plugins

Five opt-in plugins that let Hermes read from and act on your Android phone **while Hermes runs somewhere else**
(Modal, a VPS, a laptop) and you chat with it over Telegram. Nothing runs Hermes on the phone (the Termux build is
broken upstream); the phone is only a device Hermes reaches over the network.

| Plugin | Tools | Transport | Why it matters for wholesaling |
|---|---|---|---|
| `android/sms` | `android_sms_list`, `android_sms_send` | Termux | Triage seller replies, pull one lead's thread, send follow-ups from your real number |
| `android/calls` | `android_call_log`, `android_contacts_search`, `android_call_dial` | Termux | Find missed seller calls, match a number to a contact, click-to-dial a lead |
| `android/notifications` | `android_notifications_list`, `android_notify` | Termux | See what lead/CRM/email apps are pinging; push "call back now" reminders to the phone |
| `android/screen` | `android_screenshot`, `android_screen_read`, `android_input` | ADB | Read and drive apps with no API (PropStream, MLS, bank apps) |
| `android/field` | `android_location`, `android_files_list`, `android_file_pull`, `android_file_push`, `android_clipboard` | Termux / ADB | Driving for dollars: GPS + newest photos; send contracts to the phone; share the clipboard |

Everything is free and open source: [Termux](https://github.com/termux/termux-app) +
[Termux:API](https://github.com/termux/termux-api) (install both from F-Droid or GitHub, **not** Play Store),
OpenSSH, Android platform-tools `adb`, and [Tailscale](https://tailscale.com) or self-hosted
[Headscale](https://github.com/juanfont/headscale) for the private network.

## Safety defaults

- Every plugin is off until you `hermes plugins enable android/<name>`, and its tools stay hidden until the
  transport it needs is configured.
- **Sending SMS and dialing** need `HERMES_ANDROID_ALLOW_SEND=1`. SMS is capped at
  `HERMES_ANDROID_SMS_DAILY_LIMIT` (default 50) per rolling 24h.
- **Tapping/typing/launching** needs `HERMES_ANDROID_ALLOW_CONTROL=1`.
- Sends, dials, screen input, file pushes and clipboard writes are appended to `$HERMES_HOME/android/audit.jsonl`.
- Phone paths are confined to shared storage (`/sdcard/...`); `Android/data` and `Android/obb` are refused.
- No credential is stored in the repo. Keys live in your Hermes `.env` / Modal secret.
- Marketing texts and calls are regulated (TCPA, DNC lists, STOP handling). The caps keep Hermes from blasting;
  compliance is still yours.

## 1. Phone setup (once)

1. Install **Termux** and **Termux:API** (same source for both). In Termux:
   ```bash
   pkg update && pkg install -y openssh termux-api
   termux-setup-storage            # lets Termux read /sdcard (photos, Downloads)
   passwd                          # optional; key auth is used below
   sshd                            # listens on port 8022
   whoami                          # e.g. u0_a123  -> HERMES_ANDROID_SSH_USER
   ```
   Grant Termux:API the permissions you'll use: SMS, Phone, Call log, Contacts, Location, and
   *Notification access* (Android Settings → Notifications → Device & app notifications → Termux:API).
   To keep `sshd` alive: Termux notification → *Acquire wakelock*, and set battery usage for Termux to
   *Unrestricted*. Optional: [Termux:Boot](https://github.com/termux/termux-boot) with `~/.termux/boot/sshd`
   containing `termux-wake-lock; sshd`.
2. Install Tailscale on the phone and log in. Note its `100.x.y.z` address.
3. For the screen plugin (ADB): Developer options → *Wireless debugging* on, or once over USB from a computer:
   `adb tcpip 5555`. (Android forgets `tcpip` mode on reboot; re-run it, or use Wireless debugging pairing.)

## 2. Hermes setup

Generate a key on the Hermes side and authorize it in Termux:
```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/hermes_android
# copy ~/.ssh/hermes_android.pub into Termux's ~/.ssh/authorized_keys
```

Add to the Hermes `.env` (or, on Modal, to the `hermes-env` secret):
```bash
# Termux over SSH (sms, calls, notifications, location, clipboard, files)
HERMES_ANDROID_SSH_HOST=100.64.0.7
HERMES_ANDROID_SSH_PORT=8022
HERMES_ANDROID_SSH_USER=u0_a123
HERMES_ANDROID_SSH_KEY=~/.ssh/hermes_android          # or HERMES_ANDROID_SSH_PRIVATE_KEY="<key contents>"
# HERMES_ANDROID_SSH_PROXY_COMMAND=tailscale nc %h %p  # userspace Tailscale (Modal), see below

# ADB (screen, files)
HERMES_ANDROID_ADB_SERIAL=100.64.0.7:5555             # or 127.0.0.1:5555 behind the Modal bridge

# Outward actions (off by default)
HERMES_ANDROID_ALLOW_SEND=0
HERMES_ANDROID_ALLOW_CONTROL=0
HERMES_ANDROID_SMS_DAILY_LIMIT=50
# HERMES_ANDROID_TIMEOUT=30
```

Enable what you want and check the link:
```bash
for p in sms calls notifications screen field; do hermes plugins enable "android/$p"; done
set -a; . "${HERMES_HOME:-$HOME/.hermes}/.env"; set +a   # the doctor reads plain env vars
python -m plugins.android.doctor          # adb OK Pixel 8 15 / termux OK battery 81%
python -m plugins.android.doctor --json   # exit 0 ok, 1 failing, 2 unconfigured
```

## 3. Running on Modal

Modal containers can't create a TUN device, so run Tailscale in userspace mode and bridge to it:

```python
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("git", "curl", "openssh-client", "adb", "socat")
    .run_commands("curl -fsSL https://tailscale.com/install.sh | sh")
    # ... existing Hermes install lines ...
)
```

At container start (before the gateway), with `TS_AUTHKEY` in the Modal secret:
```bash
tailscaled --tun=userspace-networking --state=mem: &
tailscale up --authkey="$TS_AUTHKEY" --hostname=hermes-modal
# ADB bridge: adb can't use a proxy, so expose the phone's 5555 on localhost
socat TCP-LISTEN:5555,bind=127.0.0.1,fork,reuseaddr \
  EXEC:"tailscale nc 100.64.0.7 5555" &
```
then set `HERMES_ANDROID_ADB_SERIAL=127.0.0.1:5555` and
`HERMES_ANDROID_SSH_PROXY_COMMAND=tailscale nc %h %p`.

## Example asks (Telegram)

- "Any new seller texts? Summarize the unread ones and draft replies." → `android_sms_list unread_only`
- "Who called me this morning that I missed?" → `android_call_log type=missed`
- "I'm parked at a vacant house, log it with photos." → `android_location` + `android_files_list` + `android_file_pull`
- "Send the Oak St offer PDF to my phone." → `android_file_push`
- "Open PropStream and read the owner info on screen." → `android_input launch` + `android_screen_read`

## Tests

`tests/plugins/test_android_plugins.py` drives every tool against a scripted fake phone (no device needed):
```bash
scripts/run_tests.sh tests/plugins/test_android_plugins.py
```
