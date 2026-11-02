"""android/notifications — read what is pinging the phone and ping it back.

``termux-notification-list`` needs Termux:API's notification access (Settings → Notifications →
Device & app notifications → Termux:API). ``termux-notification`` posts a notification on the phone,
handy for "call this seller back now" nudges when Telegram is muted. Posting to the user's own phone
is not outward-facing, so it needs no send flag.
"""

from __future__ import annotations

import shlex
from typing import Any

from plugins.android import _common as c

# System noise that crowds out real alerts unless asked for.
_NOISE_PACKAGES = frozenset({"android", "com.android.systemui", "com.termux", "com.termux.api",
                             "com.google.android.gms", "com.android.providers.downloads"})


def _row(n: dict[str, Any]) -> dict[str, Any]:
    return {"id": n.get("id"), "app": n.get("packageName"), "title": n.get("title") or "",
            "text": n.get("content") or "", "when": n.get("when"), "key": n.get("key"), "tag": n.get("tag")}


@c.tool_handler
def handle_notifications_list(args: dict) -> str:
    data = c.termux_json(["termux-notification-list"]) or []
    rows = [_row(n) for n in data if isinstance(n, dict)] if isinstance(data, list) else []
    total = len(rows)
    if not args.get("include_system"):
        rows = [r for r in rows if r["app"] not in _NOISE_PACKAGES]
    app = str(args.get("app") or "").strip().lower()
    if app:
        rows = [r for r in rows if app in str(r["app"] or "").lower()]
    query = str(args.get("query") or "").strip().lower()
    if query:
        rows = [r for r in rows if query in (r["title"] + " " + r["text"]).lower()]
    limit = c.clamp_int(args.get("limit"), 30, 1, 300)
    rows.sort(key=lambda r: str(r["when"] or ""), reverse=True)
    apps: dict[str, int] = {}
    for r in rows:
        apps[r["app"] or "?"] = apps.get(r["app"] or "?", 0) + 1
    return c.ok(count=len(rows[:limit]), total_on_phone=total, by_app=apps, notifications=rows[:limit])


@c.tool_handler
def handle_notify(args: dict) -> str:
    title = str(args.get("title") or "").strip()[:200]
    text = str(args.get("text") or "").strip()[:2000]
    if not title and not text:
        raise c.AndroidError("title or text is required")
    priority = args.get("priority") or "default"
    if priority not in ("min", "low", "default", "high", "max"):
        raise c.AndroidError("priority must be min, low, default, high or max")
    argv = ["termux-notification", "--priority", priority, "--group", "hermes"]
    if title:
        argv += ["-t", title]
    if text:
        argv += ["-c", text]
    nid = str(args.get("id") or "").strip()
    if nid:
        if not nid.replace("-", "").replace("_", "").isalnum() or len(nid) > 64:
            raise c.AndroidError("id must be letters, digits, - or _ (max 64)")
        argv += ["--id", nid]
    if args.get("vibrate"):
        argv += ["--vibrate", "300,150,300"]
    url = str(args.get("open_url") or "").strip()
    if url:
        if not url.startswith(("https://", "http://")):
            raise c.AndroidError("open_url must be an http(s) URL")
        argv += ["--action", shlex.join(["termux-open-url", url])]
    c.termux(argv)
    return c.ok(posted=True, title=title, id=nid or None)


LIST_SCHEMA = c.schema(
    "android_notifications_list",
    "List notifications currently on the user's Android phone (newest first): lead/CRM apps, "
    "missed calls, messages, email. System noise is hidden unless include_system.",
    {"app": {"type": "string", "description": "Package name fragment, e.g. 'whatsapp', 'gmail', 'zillow'."},
     "query": {"type": "string", "description": "Only notifications whose title/text contains this."},
     "limit": {"type": "integer", "minimum": 1, "maximum": 300, "description": "Max rows (default 30)."},
     "include_system": {"type": "boolean", "description": "Include Android/Termux system notifications."}})

NOTIFY_SCHEMA = c.schema(
    "android_notify",
    "Post a notification on the user's own Android phone (a reminder or alert from Hermes). "
    "Reuse id to update/replace an earlier one.",
    {"title": {"type": "string", "description": "Notification title."},
     "text": {"type": "string", "description": "Notification body."},
     "priority": {"type": "string", "enum": ["min", "low", "default", "high", "max"]},
     "id": {"type": "string", "description": "Stable id to replace a previous notification."},
     "vibrate": {"type": "boolean", "description": "Vibrate the phone."},
     "open_url": {"type": "string", "description": "http(s) URL to open when tapped."}})


def register(ctx) -> None:
    ctx.register_tool(name="android_notifications_list", toolset="android_notifications", schema=LIST_SCHEMA,
                      handler=handle_notifications_list, check_fn=c.termux_available, emoji="🔔")
    ctx.register_tool(name="android_notify", toolset="android_notifications", schema=NOTIFY_SCHEMA,
                      handler=handle_notify, check_fn=c.termux_available, emoji="📲")
