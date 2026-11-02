"""Shared transport for the ``android/*`` plugins: reach Rico's phone from wherever Hermes runs.

Hermes runs off the phone (Modal, a VPS, a laptop); the phone is reached over the network through
one or both free, open-source channels:

* **ADB** (``adb -s <serial> ...``): screen capture, UI dump, input, files. The serial is a
  ``host:port`` reachable from Hermes (Tailscale/Headscale IP, or a local socat bridge on Modal).
* **Termux over SSH** (``ssh -p 8022 <user>@<host> termux-...``): Termux:API commands for SMS,
  calls, contacts, notifications, location and clipboard, plus files when ADB is down.

Every setting is read through ``agent.secret_scope.get_secret`` so per-profile ``.env`` files work
and nothing is read from ``os.environ`` directly. No credential ships in the repo: the SSH key is a
path (``HERMES_ANDROID_SSH_KEY``) or its contents (``HERMES_ANDROID_SSH_PRIVATE_KEY``, written to
``$HERMES_HOME/android/id_android`` with mode 0600).

Outward or device-changing actions (sending SMS, dialing, tapping) are off unless the operator sets
``HERMES_ANDROID_ALLOW_SEND=1`` / ``HERMES_ANDROID_ALLOW_CONTROL=1``; each one is appended to
``$HERMES_HOME/android/audit.jsonl``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

BACKEND_ADB = "adb"
BACKEND_TERMUX = "termux"

DEFAULT_TIMEOUT_S = 30.0
_TRUE = {"1", "true", "yes", "on"}

# Shared phone storage as both ADB (``/sdcard``) and Termux (``/storage/emulated/0``) see it.
STORAGE_ROOTS = ("/sdcard", "/storage/emulated/0", "/storage/self/primary")


class AndroidError(Exception):
    """A phone call failed in a way the agent should see (bad config, device offline, bad arg)."""


# --- settings ---------------------------------------------------------------------------------

def _secret(name: str, default: str = "") -> str:
    from agent.secret_scope import get_secret
    value = get_secret(name, None)
    return default if value is None else str(value).strip()


def _flag(name: str) -> bool:
    return _secret(name).lower() in _TRUE


def hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def state_dir() -> Path:
    path = hermes_home() / "android"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Settings:
    adb_serial: str = ""
    adb_bin: str = "adb"
    ssh_host: str = ""
    ssh_port: int = 8022
    ssh_user: str = ""
    ssh_key: str = ""
    ssh_private_key: str = ""
    ssh_proxy_command: str = ""
    allow_send: bool = False
    allow_control: bool = False
    sms_daily_limit: int = 50
    timeout: float = DEFAULT_TIMEOUT_S

    @classmethod
    def load(cls) -> Settings:
        def _int(name: str, default: int) -> int:
            try:
                return int(_secret(name) or default)
            except ValueError:
                return default

        try:
            timeout = float(_secret("HERMES_ANDROID_TIMEOUT") or DEFAULT_TIMEOUT_S)
        except ValueError:
            timeout = DEFAULT_TIMEOUT_S
        return cls(
            adb_serial=_secret("HERMES_ANDROID_ADB_SERIAL"),
            adb_bin=_secret("HERMES_ANDROID_ADB_BIN", "adb") or "adb",
            ssh_host=_secret("HERMES_ANDROID_SSH_HOST"),
            ssh_port=_int("HERMES_ANDROID_SSH_PORT", 8022),
            ssh_user=_secret("HERMES_ANDROID_SSH_USER"),
            ssh_key=_secret("HERMES_ANDROID_SSH_KEY"),
            ssh_private_key=_secret("HERMES_ANDROID_SSH_PRIVATE_KEY"),
            ssh_proxy_command=_secret("HERMES_ANDROID_SSH_PROXY_COMMAND"),
            allow_send=_flag("HERMES_ANDROID_ALLOW_SEND"),
            allow_control=_flag("HERMES_ANDROID_ALLOW_CONTROL"),
            sms_daily_limit=max(0, _int("HERMES_ANDROID_SMS_DAILY_LIMIT", 50)),
            timeout=max(1.0, min(timeout, 300.0)),
        )


def _safe_settings() -> Optional[Settings]:
    try:
        return Settings.load()
    except Exception:  # an unscoped secret read must hide the tools, not crash discovery
        logger.debug("android: settings unavailable", exc_info=True)
        return None


def adb_available() -> bool:
    """check_fn for ADB tools: a serial is configured and the adb binary is installed."""
    s = _safe_settings()
    return bool(s and s.adb_serial and shutil.which(s.adb_bin))


def termux_available() -> bool:
    """check_fn for Termux:API tools: an SSH host is configured and ssh is installed."""
    s = _safe_settings()
    return bool(s and s.ssh_host and shutil.which("ssh"))


def any_available() -> bool:
    return adb_available() or termux_available()


# --- process runner (tests replace ``_run``) --------------------------------------------------

def _run(cmd: list[str], *, input_bytes: Optional[bytes], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout, check=False)


def _exec(cmd: list[str], *, input_bytes: Optional[bytes] = None, timeout: float, what: str) -> bytes:
    try:
        proc = _run(cmd, input_bytes=input_bytes, timeout=timeout)
    except FileNotFoundError as exc:
        raise AndroidError(f"{what}: {cmd[0]} is not installed ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise AndroidError(f"{what}: timed out after {timeout:g}s (is the phone online?)") from exc
    except OSError as exc:
        raise AndroidError(f"{what}: {exc}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip() or \
              (proc.stdout or b"").decode("utf-8", "replace").strip()
        raise AndroidError(f"{what} failed (exit {proc.returncode}): {err[-500:] or 'no output'}")
    return proc.stdout or b""


# --- ADB --------------------------------------------------------------------------------------

_connected: set[str] = set()
_connect_lock = threading.Lock()


def _adb_base(s: Settings) -> list[str]:
    if not s.adb_serial:
        raise AndroidError("ADB is not configured: set HERMES_ANDROID_ADB_SERIAL (e.g. 100.64.0.7:5555)")
    return [s.adb_bin, "-s", s.adb_serial]


def _adb_connect(s: Settings) -> None:
    """``adb connect`` once per process for network serials (``host:port``); USB serials skip it."""
    if ":" not in s.adb_serial:
        return
    with _connect_lock:
        if s.adb_serial in _connected:
            return
        out = _exec([s.adb_bin, "connect", s.adb_serial], timeout=min(s.timeout, 20.0), what="adb connect")
        text = out.decode("utf-8", "replace").lower()
        if "connected" not in text or "cannot" in text or "failed" in text:
            raise AndroidError(f"adb connect {s.adb_serial}: {text.strip() or 'no answer'}")
        _connected.add(s.adb_serial)


def adb(args: list[str], *, settings: Optional[Settings] = None, input_bytes: Optional[bytes] = None,
        timeout: Optional[float] = None) -> bytes:
    """Run ``adb -s <serial> <args...>``; one reconnect + retry if the device dropped."""
    s = settings or Settings.load()
    base = _adb_base(s)
    _adb_connect(s)
    t = timeout or s.timeout
    try:
        return _exec(base + args, input_bytes=input_bytes, timeout=t, what="adb " + (args[0] if args else ""))
    except AndroidError as exc:
        msg = str(exc).lower()
        if ":" in s.adb_serial and ("offline" in msg or "not found" in msg or "no devices" in msg):
            with _connect_lock:
                _connected.discard(s.adb_serial)
            _adb_connect(s)
            return _exec(base + args, input_bytes=input_bytes, timeout=t, what="adb " + args[0])
        raise


def adb_shell(script: str, *, settings: Optional[Settings] = None, timeout: Optional[float] = None) -> str:
    """Run a POSIX shell snippet on the phone through ``adb shell`` (text output)."""
    return adb(["shell", script], settings=settings, timeout=timeout).decode("utf-8", "replace")


def adb_exec_out(script: str, *, settings: Optional[Settings] = None, timeout: Optional[float] = None) -> bytes:
    """Run a snippet through ``adb exec-out`` (raw binary stdout, no CRLF mangling)."""
    return adb(["exec-out", script], settings=settings, timeout=timeout)


# --- Termux over SSH --------------------------------------------------------------------------

def _ssh_key_path(s: Settings) -> str:
    if s.ssh_key:
        return os.path.expanduser(s.ssh_key)
    if not s.ssh_private_key:
        return ""
    path = state_dir() / "id_android"
    body = s.ssh_private_key.replace("\\n", "\n").strip() + "\n"
    if not path.exists() or path.read_text(encoding="utf-8-sig") != body:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
    os.chmod(path, 0o600)
    return str(path)


def ssh_command(s: Settings, remote: str) -> list[str]:
    if not s.ssh_host:
        raise AndroidError("Termux is not configured: set HERMES_ANDROID_SSH_HOST (and _USER, _KEY)")
    cmd = ["ssh", "-p", str(s.ssh_port), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           "-o", "StrictHostKeyChecking=accept-new",
           "-o", f"UserKnownHostsFile={state_dir() / 'known_hosts'}",
           "-o", "ServerAliveInterval=15", "-o", "LogLevel=ERROR"]
    key = _ssh_key_path(s)
    if key:
        cmd += ["-i", key, "-o", "IdentitiesOnly=yes"]
    if s.ssh_proxy_command:
        cmd += ["-o", f"ProxyCommand={s.ssh_proxy_command}"]
    target = f"{s.ssh_user}@{s.ssh_host}" if s.ssh_user else s.ssh_host
    return cmd + [target, remote]


def termux(argv: list[str], *, settings: Optional[Settings] = None, input_bytes: Optional[bytes] = None,
           timeout: Optional[float] = None) -> bytes:
    """Run one command in Termux (argv is shell-quoted, so message text cannot inject commands)."""
    s = settings or Settings.load()
    return _exec(ssh_command(s, shlex.join(argv)), input_bytes=input_bytes, timeout=timeout or s.timeout,
                 what=argv[0] if argv else "ssh")


def termux_shell(script: str, *, settings: Optional[Settings] = None, input_bytes: Optional[bytes] = None,
                 timeout: Optional[float] = None) -> bytes:
    """Run a trusted, plugin-built shell snippet in Termux."""
    s = settings or Settings.load()
    return _exec(ssh_command(s, script), input_bytes=input_bytes, timeout=timeout or s.timeout, what="termux shell")


def termux_json(argv: list[str], *, settings: Optional[Settings] = None, timeout: Optional[float] = None) -> Any:
    """Run a Termux:API command and parse its JSON. Termux:API reports failures as ``{"error": ...}``
    or as plain text (e.g. a missing permission), both surfaced as :class:`AndroidError`."""
    raw = termux(argv, settings=settings, timeout=timeout).decode("utf-8", "replace").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AndroidError(f"{argv[0]}: unexpected output (is the Termux:API app installed and "
                           f"permission granted?): {raw[:300]}") from exc
    if isinstance(data, dict) and data.get("error") and len(data) <= 2:
        raise AndroidError(f"{argv[0]}: {data['error']}")
    return data


# --- backend choice + generic shell ------------------------------------------------------------

def pick_backend(requested: str = "auto", *, settings: Optional[Settings] = None) -> str:
    s = settings or Settings.load()
    if requested in (BACKEND_ADB, BACKEND_TERMUX):
        if requested == BACKEND_ADB and not s.adb_serial:
            raise AndroidError("backend=adb requested but HERMES_ANDROID_ADB_SERIAL is not set")
        if requested == BACKEND_TERMUX and not s.ssh_host:
            raise AndroidError("backend=termux requested but HERMES_ANDROID_SSH_HOST is not set")
        return requested
    if s.adb_serial:
        return BACKEND_ADB
    if s.ssh_host:
        return BACKEND_TERMUX
    raise AndroidError("No phone transport configured: set HERMES_ANDROID_ADB_SERIAL and/or HERMES_ANDROID_SSH_HOST")


def shell(script: str, *, backend: str, settings: Optional[Settings] = None, binary: bool = False,
          input_bytes: Optional[bytes] = None, timeout: Optional[float] = None) -> Any:
    if backend == BACKEND_ADB:
        # exec-out keeps binary stdout intact; plain shell is what accepts stdin.
        mode = "exec-out" if binary and input_bytes is None else "shell"
        out = adb([mode, script], settings=settings, input_bytes=input_bytes, timeout=timeout)
    else:
        out = termux_shell(script, settings=settings, input_bytes=input_bytes, timeout=timeout)
    return out if binary else out.decode("utf-8", "replace")


# --- validation helpers -----------------------------------------------------------------------

_PHONE_RE = re.compile(r"^\+?[0-9]{3,15}$")


def normalize_phone(value: Any) -> str:
    """``(555) 123-4567`` -> ``5551234567``; ``+1 555 ...`` keeps the ``+``. Rejects anything else."""
    raw = str(value or "").strip()
    digits = re.sub(r"[\s().\-]", "", raw)
    if not _PHONE_RE.match(digits):
        raise AndroidError(f"not a phone number: {raw!r}")
    return digits


def phone_key(value: Any) -> str:
    """Last 10 digits, for matching ``+15551234567`` against ``(555) 123-4567``."""
    return re.sub(r"\D", "", str(value or ""))[-10:]


def clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def remote_storage_path(path: Any, *, default_dir: str = "/sdcard/Download") -> str:
    """Resolve a phone path inside shared storage; refuse ``..`` escapes and app-private dirs."""
    raw = str(path or "").strip()
    if not raw:
        raise AndroidError("a phone path is required")
    if not raw.startswith("/"):
        raw = posixpath.join(default_dir, raw)
    norm = posixpath.normpath(raw)
    if "\x00" in norm or not any(norm == r or norm.startswith(r + "/") for r in STORAGE_ROOTS):
        raise AndroidError(f"phone path must be inside shared storage (/sdcard/...): {raw!r}")
    if norm.startswith(tuple(f"{r}/Android/data" for r in STORAGE_ROOTS)) or \
       norm.startswith(tuple(f"{r}/Android/obb" for r in STORAGE_ROOTS)):
        raise AndroidError("app-private storage (Android/data, Android/obb) is off limits")
    return norm


def require(flag: bool, env_name: str, action: str) -> None:
    if not flag:
        raise AndroidError(f"{action} is disabled. The operator must set {env_name}=1 in the Hermes .env "
                           f"to allow it.")


# --- audit + JSON results ---------------------------------------------------------------------

_audit_lock = threading.Lock()


def audit(action: str, **fields: Any) -> None:
    """Append one outward/device-changing action to ``$HERMES_HOME/android/audit.jsonl``."""
    row = {"ts": time.time(), "action": action, **fields}
    with _audit_lock:
        with open(state_dir() / "audit.jsonl", "a", encoding="utf-8") as fh:  # windows-footgun: ok -- append write
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def audit_count(action: str, *, since: float) -> int:
    path = state_dir() / "audit.jsonl"
    if not path.exists():
        return 0
    n = 0
    with _audit_lock, open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("action") == action and float(row.get("ts", 0)) >= since:
                n += 1
    return n


def ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False, default=str)


def fail(message: str, **extra: Any) -> str:
    from tools.registry import tool_error
    return tool_error(message, ok=False, **extra)


def tool_handler(fn: Callable[[dict], str]) -> Callable[..., str]:
    """Wrap a handler so bad args / device failures become ``{"error": ...}`` results, never raises."""

    @functools.wraps(fn)
    def wrapper(args: Optional[dict] = None, **_kw: Any) -> str:
        try:
            return fn(dict(args or {}))
        except AndroidError as exc:
            return fail(str(exc))
        except Exception as exc:  # deliberate boundary: a tool must answer, not crash the turn
            logger.exception("android tool %s failed", fn.__name__)
            return fail(f"{type(exc).__name__}: {exc}")

    return wrapper


def schema(name: str, description: str, properties: dict[str, Any], required: Optional[list[str]] = None) -> dict:
    params: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


BACKEND_PROP = {"type": "string", "enum": ["auto", BACKEND_ADB, BACKEND_TERMUX],
                "description": "Transport. auto = ADB when configured, else Termux over SSH."}
