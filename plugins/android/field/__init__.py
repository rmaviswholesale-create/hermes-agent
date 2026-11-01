"""android/field — location, photos and files, clipboard.

* ``android_location``: ``termux-location`` (GPS/network) with a Google Maps link, for tagging a
  property while driving for dollars.
* ``android_files_list`` / ``android_file_pull``: newest photos (``/sdcard/DCIM/Camera`` by default)
  or any shared-storage folder, pulled into ``$HERMES_HOME/android/files`` so Hermes can read,
  attach or analyse them.
* ``android_file_push``: send a contract / offer PDF to ``/sdcard/Download/Hermes`` on the phone.
* ``android_clipboard``: read or set the phone clipboard (paste an address, a script, a link).

Paths on the phone are confined to shared storage (``/sdcard``); app-private dirs are refused.
"""

from __future__ import annotations

import posixpath
import shlex
from pathlib import Path

from plugins.android import _common as c

MAX_TRANSFER_BYTES = 200 * 1024 * 1024
DEFAULT_PHOTO_DIR = "/sdcard/DCIM/Camera"
DEFAULT_PUSH_DIR = "/sdcard/Download/Hermes"


def files_dir() -> Path:
    path = c.state_dir() / "files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(1, 10000):
        cand = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not cand.exists():
            return cand
    raise c.AndroidError(f"too many copies of {path.name} in {path.parent}")


@c.tool_handler
def handle_location(args: dict) -> str:
    provider = args.get("provider") or "gps"
    if provider not in ("gps", "network", "passive"):
        raise c.AndroidError("provider must be gps, network or passive")
    request = "last" if args.get("last_known") else "once"
    data = c.termux_json(["termux-location", "-p", provider, "-r", request], timeout=90) or {}
    lat, lon = data.get("latitude"), data.get("longitude")
    if lat is None or lon is None:
        raise c.AndroidError("no fix yet (GPS off, indoors, or location permission missing); try provider=network")
    return c.ok(latitude=lat, longitude=lon, accuracy_m=data.get("accuracy"), altitude_m=data.get("altitude"),
                speed_mps=data.get("speed"), provider=data.get("provider", provider),
                maps_url=f"https://www.google.com/maps/search/?api=1&query={lat},{lon}")


@c.tool_handler
def handle_files_list(args: dict) -> str:
    s = c.Settings.load()
    backend = c.pick_backend(args.get("backend") or "auto", settings=s)
    folder = c.remote_storage_path(args.get("folder") or DEFAULT_PHOTO_DIR)
    limit = c.clamp_int(args.get("limit"), 20, 1, 500)
    d = shlex.quote(folder)
    script = (f"[ -d {d} ] || {{ echo 'NO_SUCH_DIR' >&2; exit 3; }}; "
              f"ls -t {d} 2>/dev/null | head -n {limit} | while IFS= read -r f; do "
              f"[ -f {d}/\"$f\" ] && stat -c '%Y|%s|%n' {d}/\"$f\"; done")
    try:
        out = c.shell(script, backend=backend, settings=s)
    except c.AndroidError as exc:
        if "NO_SUCH_DIR" in str(exc):
            raise c.AndroidError(f"no such folder on the phone: {folder}") from exc
        raise
    files = []
    for line in out.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) == 3 and parts[0].isdigit():
            files.append({"path": parts[2], "name": posixpath.basename(parts[2]),
                          "modified": int(parts[0]), "bytes": int(parts[1]) if parts[1].isdigit() else None})
    return c.ok(folder=folder, backend=backend, count=len(files), files=files)


def _remote_size(path: str, backend: str, s: c.Settings) -> int:
    out = c.shell(f"stat -c %s {shlex.quote(path)}", backend=backend, settings=s).strip()
    if not out.isdigit():
        raise c.AndroidError(f"not a file on the phone: {path}")
    return int(out)


@c.tool_handler
def handle_file_pull(args: dict) -> str:
    s = c.Settings.load()
    backend = c.pick_backend(args.get("backend") or "auto", settings=s)
    remote = c.remote_storage_path(args.get("path"), default_dir=DEFAULT_PHOTO_DIR)
    size = _remote_size(remote, backend, s)
    if size > MAX_TRANSFER_BYTES:
        raise c.AndroidError(f"{remote} is {size} bytes; the limit is {MAX_TRANSFER_BYTES}")
    local = _unique(files_dir() / (posixpath.basename(remote) or "file"))
    timeout = max(s.timeout, 120.0)
    if backend == c.BACKEND_ADB:
        c.adb(["pull", remote, str(local)], settings=s, timeout=timeout)
    else:
        local.write_bytes(c.shell(f"cat -- {shlex.quote(remote)}", backend=backend, settings=s,
                                  binary=True, timeout=timeout))
    if not local.exists():
        raise c.AndroidError(f"pull reported success but {local} is missing")
    got = local.stat().st_size
    if got != size:
        local.unlink(missing_ok=True)
        raise c.AndroidError(f"transfer incomplete: got {got} of {size} bytes")
    is_image = local.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}
    return c.ok(remote=remote, path=str(local), bytes=got, backend=backend,
                media=f"MEDIA:{local}" if is_image else None)


@c.tool_handler
def handle_file_push(args: dict) -> str:
    s = c.Settings.load()
    backend = c.pick_backend(args.get("backend") or "auto", settings=s)
    local = Path(str(args.get("local_path") or "")).expanduser()
    if not local.is_file():
        raise c.AndroidError(f"local file not found: {local}")
    size = local.stat().st_size
    if size > MAX_TRANSFER_BYTES:
        raise c.AndroidError(f"{local} is {size} bytes; the limit is {MAX_TRANSFER_BYTES}")
    dest = c.remote_storage_path(args.get("dest") or posixpath.join(DEFAULT_PUSH_DIR, local.name),
                                 default_dir=DEFAULT_PUSH_DIR)
    timeout = max(s.timeout, 120.0)
    parent = shlex.quote(posixpath.dirname(dest))
    if backend == c.BACKEND_ADB:
        c.shell(f"mkdir -p {parent}", backend=backend, settings=s)
        c.adb(["push", str(local), dest], settings=s, timeout=timeout)
        # Make it show up in Files / Gallery right away.
        c.shell(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {shlex.quote('file://' + dest)}"
                " >/dev/null 2>&1 || true", backend=backend, settings=s)
    else:
        c.shell(f"mkdir -p {parent} && cat > {shlex.quote(dest)}", backend=backend, settings=s,
                input_bytes=local.read_bytes(), timeout=timeout)
    got = _remote_size(dest, backend, s)
    if got != size:
        raise c.AndroidError(f"transfer incomplete on the phone: {got} of {size} bytes at {dest}")
    c.audit("file_push", dest=dest, bytes=size)
    return c.ok(pushed=str(local), dest=dest, bytes=size, backend=backend)


@c.tool_handler
def handle_clipboard(args: dict) -> str:
    action = args.get("action") or "get"
    if action == "get":
        text = c.termux(["termux-clipboard-get"]).decode("utf-8", "replace")
        return c.ok(text=text, chars=len(text))
    if action != "set":
        raise c.AndroidError("action must be get or set")
    text = str(args.get("text") or "")
    if not text:
        raise c.AndroidError("text is required for action=set")
    if len(text) > 20000:
        raise c.AndroidError("clipboard text is limited to 20000 chars")
    c.termux(["termux-clipboard-set"], input_bytes=text.encode("utf-8"))
    c.audit("clipboard_set", chars=len(text))
    return c.ok(set=True, chars=len(text))


LOCATION_SCHEMA = c.schema(
    "android_location",
    "Current location of the user's Android phone (lat/lon, accuracy, Google Maps link). Use it to "
    "tag a property the user is parked at, or to plan a route.",
    {"provider": {"type": "string", "enum": ["gps", "network", "passive"], "description": "Default gps."},
     "last_known": {"type": "boolean", "description": "Return the last fix instantly instead of a fresh one."}})

FILES_LIST_SCHEMA = c.schema(
    "android_files_list",
    "List the newest files in a phone folder (default /sdcard/DCIM/Camera, i.e. recent photos).",
    {"folder": {"type": "string", "description": "Folder in shared storage, e.g. /sdcard/Download."},
     "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Max files (default 20)."},
     "backend": c.BACKEND_PROP})

FILE_PULL_SCHEMA = c.schema(
    "android_file_pull",
    "Copy a file from the phone (e.g. a property photo or a signed PDF) to Hermes. Returns the local path.",
    {"path": {"type": "string", "description": "Phone path; a bare name is looked up in DCIM/Camera."},
     "backend": c.BACKEND_PROP},
    required=["path"])

FILE_PUSH_SCHEMA = c.schema(
    "android_file_push",
    "Copy a local file (contract, offer letter, comps sheet) to the phone, by default into /sdcard/Download/Hermes.",
    {"local_path": {"type": "string", "description": "File on the Hermes machine."},
     "dest": {"type": "string", "description": "Phone path or name (default Download/Hermes/<name>)."},
     "backend": c.BACKEND_PROP},
    required=["local_path"])

CLIPBOARD_SCHEMA = c.schema(
    "android_clipboard",
    "Read (get) or replace (set) the text on the phone's clipboard.",
    {"action": {"type": "string", "enum": ["get", "set"]},
     "text": {"type": "string", "description": "Text to put on the clipboard (action=set)."}},
    required=["action"])


def register(ctx) -> None:
    for name, sch, handler, check, emoji in (
            ("android_location", LOCATION_SCHEMA, handle_location, c.termux_available, "📍"),
            ("android_files_list", FILES_LIST_SCHEMA, handle_files_list, c.any_available, "🗂️"),
            ("android_file_pull", FILE_PULL_SCHEMA, handle_file_pull, c.any_available, "⬇️"),
            ("android_file_push", FILE_PUSH_SCHEMA, handle_file_push, c.any_available, "⬆️"),
            ("android_clipboard", CLIPBOARD_SCHEMA, handle_clipboard, c.termux_available, "📋")):
        ctx.register_tool(name=name, toolset="android_field", schema=sch, handler=handler,
                          check_fn=check, emoji=emoji)
