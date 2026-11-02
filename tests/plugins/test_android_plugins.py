"""android/* plugins: tools build the right adb / ssh+termux commands, parse what the phone returns,
keep outward actions (SMS, dialing, screen input) behind operator flags with an audit trail and an
SMS daily cap, confine phone paths to shared storage, and load through real discovery only when
enabled. No phone is needed: the process runner is replaced with a scripted fake."""

from __future__ import annotations

import json
import shlex
import subprocess
import struct
import zlib

import pytest

from plugins.android import _common as c
from plugins.android import doctor
from plugins.android import calls, field, notifications, screen, sms

_ENV = ("HERMES_ANDROID_ADB_SERIAL", "HERMES_ANDROID_ADB_BIN", "HERMES_ANDROID_SSH_HOST", "HERMES_ANDROID_SSH_PORT",
        "HERMES_ANDROID_SSH_USER", "HERMES_ANDROID_SSH_KEY", "HERMES_ANDROID_SSH_PRIVATE_KEY",
        "HERMES_ANDROID_SSH_PROXY_COMMAND", "HERMES_ANDROID_ALLOW_SEND", "HERMES_ANDROID_ALLOW_CONTROL",
        "HERMES_ANDROID_SMS_DAILY_LIMIT", "HERMES_ANDROID_TIMEOUT")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    c._connected.clear()
    return hermes_home


class FakePhone:
    """Records every command; answers via (predicate, stdout, returncode) rules, first match wins."""

    def __init__(self):
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.rules: list[tuple] = []

    def on(self, needle, stdout=b"", rc=0, stderr=b""):
        if isinstance(stdout, (dict, list)):
            stdout = json.dumps(stdout)
        if isinstance(stdout, str):
            stdout = stdout.encode()
        self.rules.append((needle, stdout, rc, stderr))
        return self

    def __call__(self, cmd, *, input_bytes, timeout):
        self.calls.append((cmd, input_bytes))
        joined = " ".join(cmd)
        for needle, out, rc, err in self.rules:
            if needle(cmd) if callable(needle) else needle in joined:
                return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    def remote(self, i=-1) -> str:
        """The remote command string of an ssh/adb call (last argv element)."""
        return self.calls[i][0][-1]


@pytest.fixture()
def phone(monkeypatch):
    fake = FakePhone()
    monkeypatch.setattr(c, "_run", fake)
    return fake


@pytest.fixture()
def termux_env(monkeypatch):
    monkeypatch.setenv("HERMES_ANDROID_SSH_HOST", "100.64.0.7")
    monkeypatch.setenv("HERMES_ANDROID_SSH_USER", "u0_a123")


@pytest.fixture()
def adb_env(monkeypatch):
    monkeypatch.setenv("HERMES_ANDROID_ADB_SERIAL", "100.64.0.7:5555")


def _j(result: str) -> dict:
    return json.loads(result)


# --- transport ---------------------------------------------------------------------------------

def test_ssh_command_shape_and_quoting(termux_env, phone, home, monkeypatch):
    monkeypatch.setenv("HERMES_ANDROID_SSH_PRIVATE_KEY", "-----BEGIN KEY-----\\nabc\\n-----END KEY-----")
    monkeypatch.setenv("HERMES_ANDROID_SSH_PROXY_COMMAND", "tailscale nc %h %p")
    c.termux(["termux-sms-send", "-n", "5551234567", "hi; rm -rf ~ $(whoami)"])
    cmd = phone.calls[0][0]
    assert cmd[0] == "ssh" and cmd[cmd.index("-p") + 1] == "8022" and "BatchMode=yes" in cmd
    assert "ProxyCommand=tailscale nc %h %p" in cmd and cmd[-2] == "u0_a123@100.64.0.7"
    assert shlex.split(cmd[-1]) == ["termux-sms-send", "-n", "5551234567", "hi; rm -rf ~ $(whoami)"]
    key = home / "android" / "id_android"
    assert cmd[cmd.index("-i") + 1] == str(key)
    assert key.read_text() == "-----BEGIN KEY-----\nabc\n-----END KEY-----\n"
    assert key.stat().st_mode & 0o777 == 0o600


def test_adb_connects_once_and_reconnects_when_offline(adb_env, phone):
    phone.on("connect", "connected to 100.64.0.7:5555")
    state = {"n": 0}

    def flaky(cmd):
        if "getprop" not in " ".join(cmd):
            return False
        state["n"] += 1
        return state["n"] == 1

    phone.on(flaky, b"", rc=1, stderr=b"error: device offline")
    phone.on("getprop", "Pixel 8")
    assert c.adb_shell("getprop ro.product.model").strip() == "Pixel 8"
    kinds = [cmd[1] if cmd[1] == "connect" else cmd[3] for cmd, _ in phone.calls]
    assert kinds == ["connect", "shell", "connect", "shell"]
    c.adb_shell("getprop x")
    assert [cmd[1] for cmd, _ in phone.calls].count("connect") == 2  # no extra connect once healthy


def test_failures_become_tool_errors(termux_env, phone):
    phone.on("termux-sms-list", "Permission denial: SMS", rc=1)
    out = _j(sms.handle_sms_list({}))
    assert out["ok"] is False and "Permission denial" in out["error"]
    phone.rules.clear()
    phone.on("termux-sms-list", {"error": "Termux:API not installed"})
    assert "Termux:API not installed" in _j(sms.handle_sms_list({}))["error"]


def test_nothing_configured_hides_tools_and_doctor_says_so():
    assert not c.termux_available() and not c.adb_available()
    assert doctor.run()["status"] == "unconfigured"
    assert "No phone transport" in _j(field.handle_files_list({}))["error"]


def test_remote_paths_are_confined_to_shared_storage():
    assert c.remote_storage_path("x.jpg", default_dir="/sdcard/DCIM/Camera") == "/sdcard/DCIM/Camera/x.jpg"
    for bad in ("/data/data/com.termux/files/home/.ssh/id", "/sdcard/../data/x", "/sdcard/Android/data/app/x",
                "/storage/emulated/0/Android/obb/x", ""):
        with pytest.raises(c.AndroidError):
            c.remote_storage_path(bad)


def test_phone_normalisation():
    assert c.normalize_phone("(555) 123-4567") == "5551234567"
    assert c.normalize_phone("+1 555.123.4567") == "+15551234567"
    assert c.phone_key("+15551234567") == c.phone_key("555-123-4567")
    for bad in ("555-CALL-NOW", "12", "5551234567; reboot"):
        with pytest.raises(c.AndroidError):
            c.normalize_phone(bad)


# --- sms ---------------------------------------------------------------------------------------

_INBOX = [
    {"_id": 3, "threadid": 9, "type": "inbox", "read": False, "number": "+15551234567", "received": "2026-11-01 10:00",
     "body": "Yes I'd sell for the right price"},
    {"_id": 2, "threadid": 4, "type": "inbox", "read": True, "number": "5559990000", "received": "2026-11-01 09:00",
     "body": "STOP"},
    {"_id": 1, "threadid": 9, "type": "inbox", "read": True, "number": "(555) 123-4567", "received": "2026-10-31",
     "body": "Who is this?"},
]


def test_sms_list_filters_by_number_and_unread(termux_env, phone):
    phone.on("termux-sms-list", _INBOX)
    out = _j(sms.handle_sms_list({"number": "555-123-4567", "limit": 5}))
    assert [m["id"] for m in out["messages"]] == [3, 1]
    argv = shlex.split(phone.remote())
    assert argv[:1] == ["termux-sms-list"] and argv[argv.index("-l") + 1] == "50" and "-t" in argv
    unread = _j(sms.handle_sms_list({"unread_only": True}))
    assert [m["id"] for m in unread["messages"]] == [3]
    assert "box must be" in _j(sms.handle_sms_list({"box": "spam"}))["error"]


def test_sms_send_is_gated_capped_and_audited(termux_env, phone, home, monkeypatch):
    blocked = _j(sms.handle_sms_send({"number": "5551234567", "text": "hi"}))
    assert "HERMES_ANDROID_ALLOW_SEND=1" in blocked["error"] and phone.calls == []

    monkeypatch.setenv("HERMES_ANDROID_ALLOW_SEND", "1")
    monkeypatch.setenv("HERMES_ANDROID_SMS_DAILY_LIMIT", "2")
    first = _j(sms.handle_sms_send({"number": "(555) 123-4567", "text": "Hi, this is Rico", "sim_slot": 1}))
    assert first["sent"] and first["sent_last_24h"] == 1
    assert shlex.split(phone.remote()) == ["termux-sms-send", "-n", "5551234567", "-s", "1", "Hi, this is Rico"]
    assert _j(sms.handle_sms_send({"number": "5551234567", "text": "again"}))["ok"]
    capped = _j(sms.handle_sms_send({"number": "5551234567", "text": "third"}))
    assert "daily SMS cap" in capped["error"] and len(phone.calls) == 2
    rows = [json.loads(l) for l in (home / "android" / "audit.jsonl").read_text().splitlines()]
    assert [r["action"] for r in rows] == ["sms_send", "sms_send"] and rows[0]["number"] == "5551234567"
    assert "text is required" in _j(sms.handle_sms_send({"number": "5551234567", "text": " "}))["error"]


# --- calls -------------------------------------------------------------------------------------

def test_call_log_filters_missed_and_fills_names(termux_env, phone):
    phone.on("termux-call-log", [
        {"name": "", "phone_number": "+15551234567", "type": "MISSED", "date": "2026-11-01 08:00", "duration": "0"},
        {"name": "Mom", "phone_number": "5550001111", "type": "INCOMING", "date": "2026-10-31", "duration": "120"},
    ])
    phone.on("termux-contact-list", [{"name": "Seller - 12 Oak St", "number": "(555) 123-4567"}])
    out = _j(calls.handle_call_log({"type": "missed"}))
    assert out["calls"] == [{"name": "Seller - 12 Oak St", "number": "+15551234567", "type": "missed",
                             "date": "2026-11-01 08:00", "duration": "0"}]


def test_contacts_search_by_name_and_digits(termux_env, phone):
    phone.on("termux-contact-list", [{"name": "Jane Seller", "number": "555-123-4567"},
                                     {"name": "Title Co", "number": "555-777-8888"}])
    assert [x["name"] for x in _j(calls.handle_contacts_search({"query": "seller"}))["contacts"]] == ["Jane Seller"]
    assert [x["name"] for x in _j(calls.handle_contacts_search({"query": "7778888"}))["contacts"]] == ["Title Co"]


def test_dial_requires_send_flag(termux_env, phone, monkeypatch):
    assert "ALLOW_SEND" in _j(calls.handle_call_dial({"number": "5551234567"}))["error"]
    monkeypatch.setenv("HERMES_ANDROID_ALLOW_SEND", "true")
    assert _j(calls.handle_call_dial({"number": "555 123 4567"}))["dialing"]
    assert shlex.split(phone.remote()) == ["termux-telephony-call", "5551234567"]


# --- notifications -----------------------------------------------------------------------------

def test_notifications_hide_system_noise_and_filter(termux_env, phone):
    phone.on("termux-notification-list", [
        {"id": 1, "packageName": "com.android.systemui", "title": "USB debugging", "content": "", "when": "1"},
        {"id": 2, "packageName": "com.google.android.gm", "title": "Offer accepted", "content": "12 Oak St",
         "when": "2026-11-01 10:00"},
        {"id": 3, "packageName": "com.whatsapp", "title": "Buyer", "content": "Still available?", "when": "2026-11-01 11:00"},
    ])
    out = _j(notifications.handle_notifications_list({}))
    assert [n["id"] for n in out["notifications"]] == [3, 2] and out["total_on_phone"] == 3
    assert [n["id"] for n in _j(notifications.handle_notifications_list({"query": "oak"}))["notifications"]] == [2]
    assert _j(notifications.handle_notifications_list({"app": "whatsapp"}))["by_app"] == {"com.whatsapp": 1}


def test_notify_builds_safe_command(termux_env, phone):
    out = _j(notifications.handle_notify({"title": "Call back", "text": "Jane re: 12 Oak", "priority": "high",
                                          "id": "lead-12", "open_url": "https://maps.google.com/?q=12+Oak"}))
    assert out["posted"]
    argv = shlex.split(phone.remote())
    assert argv[:5] == ["termux-notification", "--priority", "high", "--group", "hermes"]
    assert argv[argv.index("-t") + 1] == "Call back" and argv[argv.index("--id") + 1] == "lead-12"
    assert shlex.split(argv[argv.index("--action") + 1]) == ["termux-open-url", "https://maps.google.com/?q=12+Oak"]
    assert "open_url" in _j(notifications.handle_notify({"title": "x", "open_url": "javascript:1"}))["error"]
    assert "id must be" in _j(notifications.handle_notify({"title": "x", "id": "a b"}))["error"]


# --- screen ------------------------------------------------------------------------------------

def _png(w=1080, h=2400) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", 13) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    return screen._PNG_MAGIC + chunk


def test_screenshot_saves_png_and_returns_media(adb_env, phone, home):
    phone.on("connect", "connected to 100.64.0.7:5555")
    phone.on("screencap", b"WARNING: linker noise\n" + _png())
    out = _j(screen.handle_screenshot({}))
    assert (out["width"], out["height"]) == (1080, 2400) and out["media"] == f"MEDIA:{out['path']}"
    saved = open(out["path"], "rb").read()
    assert saved.startswith(screen._PNG_MAGIC) and out["path"].startswith(str(home / "android" / "screens"))
    assert phone.calls[-1][0][:4] == ["adb", "-s", "100.64.0.7:5555", "exec-out"]
    phone.rules.clear()
    phone.on("connect", "connected")
    phone.on("screencap", b"")
    assert "no PNG" in _j(screen.handle_screenshot({}))["error"]


_UI = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.propstream" content-desc="" clickable="false" bounds="[0,0][1080,2400]">
<node text="Skip trace" resource-id="com.propstream:id/skip" class="android.widget.Button" package="com.propstream" content-desc="" clickable="true" bounds="[100,200][300,260]"/>
<node text="" resource-id="com.propstream:id/search" class="android.widget.EditText" package="com.propstream" content-desc="" clickable="true" bounds="[0,0][1080,100]"/>
<node text="" class="android.widget.ImageButton" package="com.propstream" content-desc="Menu" clickable="true" bounds="[0,0][0,0]"/>
<node text="Owner: Jane Doe" class="android.widget.TextView" package="com.propstream" content-desc="" clickable="false" bounds="[10,300][500,340]"/>
</node></hierarchy>"""


def test_screen_read_parses_tap_targets(adb_env, phone):
    phone.on("connect", "connected")
    phone.on("uiautomator", "UI hierchary dumped to: /sdcard/hermes_ui.xml\n" + _UI)
    out = _j(screen.handle_screen_read({}))
    assert [(e["text"], e["tap"], e["clickable"]) for e in out["elements"]] == [
        ("Skip trace", [200, 230], True), ("Owner: Jane Doe", [255, 320], False)]
    assert out["packages"] == ["com.propstream"] and out["elements"][0]["id"] == "skip"
    with_empty = _j(screen.handle_screen_read({"include_empty": True}))
    assert any(e["editable"] for e in with_empty["elements"])  # zero-size "Menu" node dropped
    assert [e["text"] for e in _j(screen.handle_screen_read({"query": "owner"}))["elements"]] == ["Owner: Jane Doe"]


def test_screen_read_refuses_entities():
    with pytest.raises(c.AndroidError):
        screen.parse_ui_dump('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><hierarchy/>')


def test_input_gated_validated_and_audited(adb_env, phone, home, monkeypatch):
    phone.on("connect", "connected")
    assert "ALLOW_CONTROL" in _j(screen.handle_input({"action": "tap", "x": 1, "y": 2}))["error"]
    monkeypatch.setenv("HERMES_ANDROID_ALLOW_CONTROL", "1")
    assert _j(screen.handle_input({"action": "tap", "x": 200, "y": 230}))["done"] == "tap"
    assert phone.remote() == "input tap 200 230"
    _j(screen.handle_input({"action": "text", "text": "12 Oak St"}))
    assert shlex.split(phone.remote()) == ["input", "text", "12%sOak%sSt"]
    _j(screen.handle_input({"action": "key", "key": "back"}))
    assert phone.remote() == "input keyevent KEYCODE_BACK"
    _j(screen.handle_input({"action": "launch", "package": "com.zillow.android.zillowmap"}))
    assert shlex.split(phone.remote())[:3] == ["monkey", "-p", "com.zillow.android.zillowmap"]
    for bad in ({"action": "text", "text": "café"}, {"action": "launch", "package": "x; reboot"},
                {"action": "open_url", "url": "tel:5551234567"}, {"action": "tap"}, {"action": "shell"}):
        assert _j(screen.handle_input(bad))["ok"] is False
    phone.on("monkey", "** No activities found to run, monkey aborted.")
    assert "not installed" in _j(screen.handle_input({"action": "launch", "package": "com.nope.app"}))["error"]
    actions = [json.loads(l)["input"] for l in (home / "android" / "audit.jsonl").read_text().splitlines()]
    assert actions == ["tap", "text", "key", "launch"]


# --- field -------------------------------------------------------------------------------------

def test_location_returns_maps_link(termux_env, phone):
    phone.on("termux-location", {"latitude": 33.749, "longitude": -84.388, "accuracy": 8.5, "provider": "gps"})
    out = _j(field.handle_location({}))
    assert out["maps_url"].endswith("query=33.749,-84.388") and out["accuracy_m"] == 8.5
    assert shlex.split(phone.remote()) == ["termux-location", "-p", "gps", "-r", "once"]
    phone.rules.clear()
    phone.on("termux-location", {})
    assert "no fix" in _j(field.handle_location({"last_known": True}))["error"]


def test_files_list_parses_stat_lines(adb_env, phone):
    phone.on("connect", "connected")
    phone.on("stat -c", "1730450000|2048000|/sdcard/DCIM/Camera/IMG_1.jpg\n"
                        "1730440000|1024|/sdcard/DCIM/Camera/IMG 2.jpg\n")
    out = _j(field.handle_files_list({"limit": 2}))
    assert [f["name"] for f in out["files"]] == ["IMG_1.jpg", "IMG 2.jpg"] and out["backend"] == "adb"
    assert "head -n 2" in phone.remote() and "ls -t /sdcard/DCIM/Camera " in phone.remote()
    assert "shared storage" in _j(field.handle_files_list({"folder": "/data/local/tmp"}))["error"]


def test_file_pull_over_termux_checks_size(termux_env, phone, home):
    phone.on("stat -c %s", "5")
    phone.on("cat --", b"hello")
    out = _j(field.handle_file_pull({"path": "IMG_1.jpg"}))
    assert out["remote"] == "/sdcard/DCIM/Camera/IMG_1.jpg" and out["bytes"] == 5 and out["media"]
    assert open(out["path"], "rb").read() == b"hello"
    again = _j(field.handle_file_pull({"path": "IMG_1.jpg"}))
    assert again["path"].endswith("IMG_1-1.jpg")  # never overwrites an earlier pull
    phone.rules.clear()
    phone.on("stat -c %s", "5")
    phone.on("cat --", b"hel")
    assert "incomplete" in _j(field.handle_file_pull({"path": "IMG_1.jpg"}))["error"]


def test_file_push_over_adb(adb_env, phone, tmp_path, home):
    doc = tmp_path / "offer.pdf"
    doc.write_bytes(b"%PDF-1.7 offer")
    phone.on("connect", "connected")
    phone.on("stat -c %s", str(len(b"%PDF-1.7 offer")))
    out = _j(field.handle_file_push({"local_path": str(doc)}))
    assert out["dest"] == "/sdcard/Download/Hermes/offer.pdf"
    pushes = [cmd for cmd, _ in phone.calls if len(cmd) > 3 and cmd[3] == "push"]
    assert pushes == [["adb", "-s", "100.64.0.7:5555", "push", str(doc), "/sdcard/Download/Hermes/offer.pdf"]]
    assert "file_push" in (home / "android" / "audit.jsonl").read_text()
    assert "not found" in _j(field.handle_file_push({"local_path": str(tmp_path / "nope.pdf")}))["error"]


def test_file_push_over_termux_streams_stdin(termux_env, phone, tmp_path):
    doc = tmp_path / "comps.csv"
    doc.write_bytes(b"a,b\n1,2\n")
    phone.on("stat -c %s", "8")
    out = _j(field.handle_file_push({"local_path": str(doc), "dest": "Deals/comps.csv"}))
    assert out["dest"] == "/sdcard/Download/Hermes/Deals/comps.csv"
    script, stdin = phone.calls[0][0][-1], phone.calls[0][1]
    assert script.startswith("mkdir -p /sdcard/Download/Hermes/Deals && cat > ") and stdin == b"a,b\n1,2\n"


def test_clipboard_get_and_set(termux_env, phone):
    phone.on("termux-clipboard-get", "12 Oak St, Atlanta GA")
    assert _j(field.handle_clipboard({"action": "get"}))["text"] == "12 Oak St, Atlanta GA"
    assert _j(field.handle_clipboard({"action": "set", "text": "hello"}))["set"]
    assert phone.calls[-1][1] == b"hello" and phone.remote() == "termux-clipboard-set"


# --- doctor + discovery ------------------------------------------------------------------------

def test_doctor_reports_both_transports(termux_env, adb_env, phone, monkeypatch, capsys):
    monkeypatch.setattr(c.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    phone.on("connect", "connected")
    phone.on("getprop", "Pixel 8\n15\n")
    phone.on("termux-battery-status", {"percentage": 81, "status": "CHARGING"})
    assert doctor.main(["--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["adb"]["device"] == "Pixel 8 15" and report["termux"]["battery_pct"] == 81
    assert c.adb_available() and c.termux_available()
    phone.rules.clear()
    phone.on("termux-battery-status", "", rc=255, stderr=b"ssh: connect to host 100.64.0.7 port 8022: timed out")
    phone.on("connect", "connected")
    phone.on("getprop", "Pixel 8")
    assert doctor.main([]) == 1


_PLUGIN_TOOLS = {
    "android/sms": {"android_sms_list", "android_sms_send"},
    "android/calls": {"android_call_log", "android_contacts_search", "android_call_dial"},
    "android/notifications": {"android_notifications_list", "android_notify"},
    "android/screen": {"android_screenshot", "android_screen_read", "android_input"},
    "android/field": {"android_location", "android_files_list", "android_file_pull", "android_file_push",
                      "android_clipboard"},
}


def test_discovered_but_off_by_default(home):
    from hermes_cli import plugins as pmod
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    for key in _PLUGIN_TOOLS:
        loaded = mgr._plugins[key]
        assert loaded.manifest.source == "bundled" and not loaded.enabled


def test_enabled_plugins_register_their_tools(home):
    import hermes_yaml as yaml
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": list(_PLUGIN_TOOLS)}}))
    from hermes_cli import plugins as pmod
    from tools.registry import registry
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    for key, tools in _PLUGIN_TOOLS.items():
        loaded = mgr._plugins[key]
        assert loaded.enabled, (key, loaded.error)
        for tool in tools:
            assert registry.get_entry(tool, scope=mgr.scope_key) is not None, tool
