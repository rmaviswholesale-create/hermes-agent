"""android/calls — who called, who is this number, and dial a lead.

``termux-call-log`` and ``termux-contact-list`` back the read tools (missed seller calls, matching a
number to a saved contact); ``termux-telephony-call`` places a call on the phone so the user just
picks it up. Call-log rows are enriched with the contact name when the phone has one.
"""

from __future__ import annotations

from typing import Any

from plugins.android import _common as c

_CALL_TYPES = ("all", "incoming", "outgoing", "missed", "rejected", "blocked", "voicemail")


def _contacts(s: c.Settings) -> list[dict[str, Any]]:
    data = c.termux_json(["termux-contact-list"], settings=s) or []
    return [{"name": d.get("name"), "number": d.get("number")} for d in data if isinstance(d, dict)]


@c.tool_handler
def handle_call_log(args: dict) -> str:
    s = c.Settings.load()
    limit = c.clamp_int(args.get("limit"), 25, 1, 500)
    kind = (args.get("type") or "all").lower()
    if kind not in _CALL_TYPES:
        raise c.AndroidError(f"type must be one of {', '.join(_CALL_TYPES)}")
    number = args.get("number")
    key = c.phone_key(c.normalize_phone(number)) if number else ""
    fetch = limit if kind == "all" and not key else min(2000, limit * 10)
    data = c.termux_json(["termux-call-log", "-l", str(fetch)], settings=s) or []
    rows = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        row = {"name": d.get("name") or None, "number": d.get("phone_number"),
               "type": str(d.get("type", "")).lower(), "date": d.get("date"), "duration": d.get("duration")}
        if kind != "all" and row["type"] != kind:
            continue
        if key and c.phone_key(row["number"]) != key:
            continue
        rows.append(row)
    rows = rows[:limit]
    if args.get("resolve_names", True) and any(not r["name"] for r in rows):
        try:
            book = {c.phone_key(x["number"]): x["name"] for x in _contacts(s) if x.get("number")}
            for r in rows:
                r["name"] = r["name"] or book.get(c.phone_key(r["number"]))
        except c.AndroidError:
            pass  # contacts permission missing: the log is still useful without names
    return c.ok(count=len(rows), calls=rows)


@c.tool_handler
def handle_contacts_search(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise c.AndroidError("query is required (a name fragment or a phone number)")
    limit = c.clamp_int(args.get("limit"), 20, 1, 200)
    contacts = _contacts(c.Settings.load())
    digits = c.phone_key(query)
    if len(digits) >= 4 and not any(ch.isalpha() for ch in query):
        hits = [x for x in contacts if digits in c.phone_key(x.get("number"))]
    else:
        q = query.lower()
        hits = [x for x in contacts if q in str(x.get("name") or "").lower()]
    return c.ok(query=query, count=len(hits[:limit]), total_contacts=len(contacts), contacts=hits[:limit])


@c.tool_handler
def handle_call_dial(args: dict) -> str:
    s = c.Settings.load()
    c.require(s.allow_send, "HERMES_ANDROID_ALLOW_SEND", "Dialing calls")
    number = c.normalize_phone(args.get("number"))
    c.termux(["termux-telephony-call", number], settings=s)
    c.audit("call_dial", number=number)
    return c.ok(dialing=True, number=number, note="The phone is placing the call now; the user talks on the phone.")


CALL_LOG_SCHEMA = c.schema(
    "android_call_log",
    "Recent calls on the user's Android phone (newest first), with contact names. Filter type=missed "
    "to find seller calls to return, or number= for one lead's history.",
    {"limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Max rows (default 25)."},
     "type": {"type": "string", "enum": list(_CALL_TYPES), "description": "Call type filter (default all)."},
     "number": {"type": "string", "description": "Only calls with this number."},
     "resolve_names": {"type": "boolean", "description": "Fill names from contacts (default true)."}})

CONTACTS_SCHEMA = c.schema(
    "android_contacts_search",
    "Search the phone's contacts by name fragment or by (partial) phone number.",
    {"query": {"type": "string", "description": "Name fragment or digits."},
     "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Max results (default 20)."}},
    required=["query"])

DIAL_SCHEMA = c.schema(
    "android_call_dial",
    "Place a phone call from the user's Android phone (they then talk on the phone). Only dial "
    "when the user asked. Fails unless the operator enabled sending.",
    {"number": {"type": "string", "description": "Number to call."}},
    required=["number"])


def register(ctx) -> None:
    for name, sch, handler, emoji in (
            ("android_call_log", CALL_LOG_SCHEMA, handle_call_log, "📞"),
            ("android_contacts_search", CONTACTS_SCHEMA, handle_contacts_search, "📇"),
            ("android_call_dial", DIAL_SCHEMA, handle_call_dial, "☎️")):
        ctx.register_tool(name=name, toolset="android_calls", schema=sch, handler=handler,
                          check_fn=c.termux_available, emoji=emoji)
