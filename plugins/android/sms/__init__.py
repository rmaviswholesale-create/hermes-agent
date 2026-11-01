"""android/sms — read inbox/threads and send texts through Termux:API (``termux-sms-list``/``-send``).

Built for seller follow-up: pull the latest replies, filter one lead's thread by number, and send a
reply once the operator has allowed sending. Sends are capped per rolling 24h
(``HERMES_ANDROID_SMS_DAILY_LIMIT``, default 50) and logged to ``$HERMES_HOME/android/audit.jsonl``.
"""

from __future__ import annotations

import time
from typing import Any

from plugins.android import _common as c

_TYPES = ("all", "inbox", "sent", "draft", "outbox")
MAX_SMS_CHARS = 1600


def _row(m: dict[str, Any]) -> dict[str, Any]:
    return {"id": m.get("_id"), "thread_id": m.get("threadid"), "type": m.get("type"),
            "number": m.get("number"), "name": m.get("sender") or m.get("name"),
            "received": m.get("received"), "read": m.get("read"), "body": m.get("body", "")}


@c.tool_handler
def handle_sms_list(args: dict) -> str:
    limit = c.clamp_int(args.get("limit"), 20, 1, 200)
    box = args.get("box") or "inbox"
    if box not in _TYPES:
        raise c.AndroidError(f"box must be one of {', '.join(_TYPES)}")
    number = args.get("number")
    key = c.phone_key(c.normalize_phone(number)) if number else ""
    unread_only = bool(args.get("unread_only"))
    # Filter on our side (older Termux:API has no -f), so over-fetch when filtering.
    fetch = min(1000, limit * 10) if (key or unread_only) else limit
    data = c.termux_json(["termux-sms-list", "-d", "-n", "-l", str(fetch), "-t", box]) or []
    if not isinstance(data, list):
        raise c.AndroidError("termux-sms-list returned an unexpected shape")
    rows = [_row(m) for m in data if isinstance(m, dict)]
    if key:
        rows = [r for r in rows if c.phone_key(r["number"]) == key]
    if unread_only:
        rows = [r for r in rows if not r["read"]]
    return c.ok(box=box, count=len(rows[:limit]), messages=rows[:limit])


@c.tool_handler
def handle_sms_send(args: dict) -> str:
    s = c.Settings.load()
    c.require(s.allow_send, "HERMES_ANDROID_ALLOW_SEND", "Sending SMS")
    number = c.normalize_phone(args.get("number"))
    text = str(args.get("text") or "").strip()
    if not text:
        raise c.AndroidError("text is required")
    if len(text) > MAX_SMS_CHARS:
        raise c.AndroidError(f"text is {len(text)} chars; the limit is {MAX_SMS_CHARS}")
    sent_24h = c.audit_count("sms_send", since=time.time() - 86400)
    if s.sms_daily_limit and sent_24h >= s.sms_daily_limit:
        raise c.AndroidError(f"daily SMS cap reached ({sent_24h}/{s.sms_daily_limit} in 24h); "
                             f"raise HERMES_ANDROID_SMS_DAILY_LIMIT to send more")
    argv = ["termux-sms-send", "-n", number]
    sim = args.get("sim_slot")
    if sim is not None:
        argv += ["-s", str(c.clamp_int(sim, 0, 0, 1))]
    c.termux(argv + [text], settings=s)
    c.audit("sms_send", number=number, chars=len(text))
    return c.ok(sent=True, number=number, chars=len(text), sent_last_24h=sent_24h + 1,
                daily_limit=s.sms_daily_limit or None)


SMS_LIST_SCHEMA = c.schema(
    "android_sms_list",
    "Read text messages from the user's Android phone (newest first). Use number= to read one "
    "contact's thread (any format; matched on the last 10 digits) or unread_only to triage replies.",
    {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Max messages (default 20)."},
     "box": {"type": "string", "enum": list(_TYPES), "description": "Folder (default inbox)."},
     "number": {"type": "string", "description": "Only messages with this phone number."},
     "unread_only": {"type": "boolean", "description": "Only unread messages."}})

SMS_SEND_SCHEMA = c.schema(
    "android_sms_send",
    "Send a text message from the user's own Android phone. Only send what the user asked for or "
    "approved; this reaches a real person. Fails unless the operator enabled sending. Obey opt-outs "
    "(STOP) and texting laws (TCPA) for marketing messages.",
    {"number": {"type": "string", "description": "Recipient phone number."},
     "text": {"type": "string", "description": f"Message body (max {MAX_SMS_CHARS} chars)."},
     "sim_slot": {"type": "integer", "enum": [0, 1], "description": "SIM slot on dual-SIM phones."}},
    required=["number", "text"])


def register(ctx) -> None:
    ctx.register_tool(name="android_sms_list", toolset="android_sms", schema=SMS_LIST_SCHEMA,
                      handler=handle_sms_list, check_fn=c.termux_available, emoji="💬")
    ctx.register_tool(name="android_sms_send", toolset="android_sms", schema=SMS_SEND_SCHEMA,
                      handler=handle_sms_send, check_fn=c.termux_available, emoji="📤")
