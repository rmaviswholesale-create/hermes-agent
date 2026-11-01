"""android/screen — screenshot, read and operate apps that have no API (PropStream, MLS apps, banks).

* ``android_screenshot``: ``adb exec-out screencap -p`` saved under ``$HERMES_HOME/android/screens``
  and returned as a ``MEDIA:`` path, so ``vision_analyze`` (or a native-vision model) can look at it.
* ``android_screen_read``: ``uiautomator dump`` parsed into the visible text/labels with their tap
  centre, so the agent can read a screen and tap the right element without vision.
* ``android_input``: tap / swipe / type / key / launch app / open URL — gated by
  ``HERMES_ANDROID_ALLOW_CONTROL=1`` and audited.
"""

from __future__ import annotations

import re
import shlex
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

from plugins.android import _common as c

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_KEEP_SCREENSHOTS = 50
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_KEYS = {"home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "enter": "KEYCODE_ENTER", "recents": "KEYCODE_APP_SWITCH",
         "delete": "KEYCODE_DEL", "tab": "KEYCODE_TAB", "search": "KEYCODE_SEARCH", "menu": "KEYCODE_MENU",
         "volume_up": "KEYCODE_VOLUME_UP", "volume_down": "KEYCODE_VOLUME_DOWN", "power": "KEYCODE_POWER",
         "wakeup": "KEYCODE_WAKEUP", "sleep": "KEYCODE_SLEEP", "camera": "KEYCODE_CAMERA"}
_ACTIONS = ("tap", "long_press", "swipe", "text", "key", "launch", "open_url")


def screens_dir() -> Path:
    path = c.state_dir() / "screens"
    path.mkdir(parents=True, exist_ok=True)
    return path


def png_size(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) >= 24 and data.startswith(_PNG_MAGIC) and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _prune(directory: Path, keep: int) -> None:
    shots = sorted(directory.glob("screen-*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in shots[keep:]:
        old.unlink(missing_ok=True)


@c.tool_handler
def handle_screenshot(args: dict) -> str:
    data = c.adb_exec_out("screencap -p")
    start = data.find(_PNG_MAGIC)  # some ROMs print a warning line before the PNG
    if start < 0:
        raise c.AndroidError("screencap returned no PNG (screen locked with secure content, or adb too old)")
    data = data[start:]
    size = png_size(data)
    out = screens_dir() / f"screen-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
    out.write_bytes(data)
    _prune(out.parent, _KEEP_SCREENSHOTS)
    return c.ok(path=str(out), media=f"MEDIA:{out}", bytes=len(data),
                width=size[0] if size else None, height=size[1] if size else None,
                hint="Use vision_analyze on this path to read it.")


def parse_ui_dump(xml_text: str, *, include_empty: bool = False) -> list[dict[str, Any]]:
    start = xml_text.find("<?xml")
    if start < 0:
        start = xml_text.find("<hierarchy")
    if start < 0:
        raise c.AndroidError(f"uiautomator returned no XML: {xml_text[:200]}")
    end = xml_text.rfind("</hierarchy>")
    body = xml_text[start:end + len("</hierarchy>")] if end >= 0 else xml_text[start:]
    if "<!DOCTYPE" in body or "<!ENTITY" in body:
        raise c.AndroidError("refusing UI dump with DTD/entities")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise c.AndroidError(f"could not parse UI dump: {exc}") from exc
    nodes = []
    for node in root.iter("node"):
        a = node.attrib
        text, desc = a.get("text", "").strip(), a.get("content-desc", "").strip()
        clickable = a.get("clickable") == "true"
        editable = a.get("class", "").endswith("EditText")
        if not (text or desc or include_empty and (clickable or editable)):
            continue
        m = _BOUNDS_RE.match(a.get("bounds", ""))
        x1, y1, x2, y2 = (int(v) for v in m.groups()) if m else (0, 0, 0, 0)
        if m and (x2 <= x1 or y2 <= y1):
            continue  # off-screen / zero-size
        nodes.append({"text": text, "label": desc, "id": a.get("resource-id", "").split("/")[-1] or None,
                      "class": a.get("class", "").rsplit(".", 1)[-1], "package": a.get("package"),
                      "clickable": clickable, "editable": editable,
                      "checked": a.get("checked") == "true" if a.get("checkable") == "true" else None,
                      "tap": [(x1 + x2) // 2, (y1 + y2) // 2], "bounds": [x1, y1, x2, y2]})
    return nodes


@c.tool_handler
def handle_screen_read(args: dict) -> str:
    raw = c.adb_exec_out("uiautomator dump /sdcard/hermes_ui.xml >/dev/null 2>&1; "
                         "cat /sdcard/hermes_ui.xml; rm -f /sdcard/hermes_ui.xml",
                         timeout=45).decode("utf-8", "replace")
    nodes = parse_ui_dump(raw, include_empty=bool(args.get("include_empty")))
    query = str(args.get("query") or "").strip().lower()
    if query:
        nodes = [n for n in nodes if query in (n["text"] + " " + n["label"]).lower()]
    limit = c.clamp_int(args.get("limit"), 150, 1, 1000)
    apps = sorted({n["package"] for n in nodes if n["package"]})
    return c.ok(count=len(nodes[:limit]), truncated=len(nodes) > limit, packages=apps,
                elements=nodes[:limit], hint="Tap an element with android_input action=tap x,y = its 'tap'.")


def _coord(args: dict, key: str) -> int:
    if args.get(key) is None:
        raise c.AndroidError(f"{key} is required for this action")
    return c.clamp_int(args.get(key), 0, 0, 10000)


def _input_text(text: str) -> str:
    """``input text`` cannot take spaces or non-ASCII; %s is its space escape."""
    if not text:
        raise c.AndroidError("text is required")
    if len(text) > 1000:
        raise c.AndroidError("text is limited to 1000 chars per call")
    if any(ord(ch) > 126 or (ord(ch) < 32) for ch in text):
        raise c.AndroidError("input text supports printable ASCII only (no emoji/accents/newlines); "
                             "use action=key enter for new lines")
    return text.replace(" ", "%s")


Built = tuple[list[str], dict[str, Any]]


def _tap(args: dict) -> Built:
    x, y = _coord(args, "x"), _coord(args, "y")
    return ["input", "tap", str(x), str(y)], {"x": x, "y": y}


def _long_press(args: dict) -> Built:
    x, y = _coord(args, "x"), _coord(args, "y")
    return ["input", "swipe", str(x), str(y), str(x), str(y), "800"], {"x": x, "y": y}


def _swipe(args: dict) -> Built:
    x, y, x2, y2 = (_coord(args, k) for k in ("x", "y", "x2", "y2"))
    ms = c.clamp_int(args.get("duration_ms"), 300, 50, 5000)
    return ["input", "swipe", str(x), str(y), str(x2), str(y2), str(ms)], {"from": [x, y], "to": [x2, y2], "ms": ms}


def _text(args: dict) -> Built:
    text = str(args.get("text") or "")
    return ["input", "text", _input_text(text)], {"chars": len(text)}


def _key(args: dict) -> Built:
    key = str(args.get("key") or "").lower()
    if key not in _KEYS:
        raise c.AndroidError(f"key must be one of {', '.join(sorted(_KEYS))}")
    return ["input", "keyevent", _KEYS[key]], {"key": key}


def _launch(args: dict) -> Built:
    pkg = str(args.get("package") or "").strip()
    if not _PACKAGE_RE.match(pkg):
        raise c.AndroidError("package must be an Android package name, e.g. com.zillow.android.zillowmap")
    return ["monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1"], {"package": pkg}


def _open_url(args: dict) -> Built:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("https://", "http://", "geo:")):
        raise c.AndroidError("url must start with https://, http:// or geo:")
    return ["am", "start", "-a", "android.intent.action.VIEW", "-d", url], {"url": url}


_INPUT_BUILDERS = {"tap": _tap, "long_press": _long_press, "swipe": _swipe, "text": _text, "key": _key,
                   "launch": _launch, "open_url": _open_url}


@c.tool_handler
def handle_input(args: dict) -> str:
    s = c.Settings.load()
    c.require(s.allow_control, "HERMES_ANDROID_ALLOW_CONTROL", "Controlling the phone screen")
    action = args.get("action")
    builder = _INPUT_BUILDERS.get(action) if isinstance(action, str) else None
    if builder is None:
        raise c.AndroidError(f"action must be one of {', '.join(_ACTIONS)}")
    argv, detail = builder(args)
    out = c.adb_shell(shlex.join(argv), settings=s)
    if action == "launch" and "No activities found" in out:
        raise c.AndroidError(f"{detail['package']} is not installed or has no launcher activity")
    c.audit("screen_input", input=action, **detail)
    return c.ok(done=action, **detail)

SCREENSHOT_SCHEMA = c.schema(
    "android_screenshot",
    "Take a screenshot of the user's Android phone and save it locally. Returns a path for vision_analyze.",
    {})

SCREEN_READ_SCHEMA = c.schema(
    "android_screen_read",
    "Read what is on the phone screen as text: every visible label/text with its tap coordinates and "
    "whether it is clickable or editable. Cheaper and more exact than a screenshot for reading and tapping.",
    {"query": {"type": "string", "description": "Only elements whose text/label contains this."},
     "include_empty": {"type": "boolean", "description": "Also list unlabeled clickable/editable elements."},
     "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "Max elements (default 150)."}})

INPUT_SCHEMA = c.schema(
    "android_input",
    "Operate the phone: tap/long_press (x,y), swipe (x,y -> x2,y2), text (types into the focused field), "
    "key (home/back/enter/...), launch (package), open_url (https/geo). Read the screen first to get "
    "coordinates. Fails unless the operator enabled phone control.",
    {"action": {"type": "string", "enum": list(_ACTIONS)},
     "x": {"type": "integer"}, "y": {"type": "integer"}, "x2": {"type": "integer"}, "y2": {"type": "integer"},
     "duration_ms": {"type": "integer", "minimum": 50, "maximum": 5000},
     "text": {"type": "string", "description": "ASCII text to type."},
     "key": {"type": "string", "enum": sorted(_KEYS)},
     "package": {"type": "string", "description": "App package to launch."},
     "url": {"type": "string", "description": "https://, http:// or geo: URL to open."}},
    required=["action"])


def register(ctx) -> None:
    for name, sch, handler, emoji in (
            ("android_screenshot", SCREENSHOT_SCHEMA, handle_screenshot, "📸"),
            ("android_screen_read", SCREEN_READ_SCHEMA, handle_screen_read, "👀"),
            ("android_input", INPUT_SCHEMA, handle_input, "👆")):
        ctx.register_tool(name=name, toolset="android_screen", schema=sch, handler=handler,
                          check_fn=c.adb_available, emoji=emoji)
