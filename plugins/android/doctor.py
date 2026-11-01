"""Connectivity check for the android/* plugins.

    python -m plugins.android.doctor          # human-readable
    python -m plugins.android.doctor --json   # structured, for agents/CI

Exit 0 when every configured transport answers, 1 when one is configured but failing, 2 when
nothing is configured. Reads the same settings as the plugins (``$HERMES_HOME/.env`` included when
run through ``hermes``; plain env vars otherwise).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from typing import Any

from plugins.android import _common as c


def _check_adb(s: c.Settings) -> dict[str, Any]:
    if not s.adb_serial:
        return {"configured": False}
    if not shutil.which(s.adb_bin):
        return {"configured": True, "ok": False, "error": f"{s.adb_bin} not on PATH (apt install adb)"}
    try:
        model = c.adb_shell("getprop ro.product.model; getprop ro.build.version.release", settings=s).split()
        return {"configured": True, "ok": True, "serial": s.adb_serial, "device": " ".join(model)}
    except c.AndroidError as exc:
        return {"configured": True, "ok": False, "serial": s.adb_serial, "error": str(exc)}


def _check_termux(s: c.Settings) -> dict[str, Any]:
    if not s.ssh_host:
        return {"configured": False}
    if not shutil.which("ssh"):
        return {"configured": True, "ok": False, "error": "ssh not on PATH (apt install openssh-client)"}
    try:
        battery = c.termux_json(["termux-battery-status"], settings=s) or {}
        return {"configured": True, "ok": True, "host": s.ssh_host, "battery_pct": battery.get("percentage"),
                "charging": battery.get("status")}
    except c.AndroidError as exc:
        return {"configured": True, "ok": False, "host": s.ssh_host, "error": str(exc)}


def run() -> dict[str, Any]:
    s = c.Settings.load()
    report = {"adb": _check_adb(s), "termux": _check_termux(s),
              "allow_send": s.allow_send, "allow_control": s.allow_control, "sms_daily_limit": s.sms_daily_limit}
    configured = [v for k, v in report.items() if isinstance(v, dict) and v.get("configured")]
    report["status"] = "unconfigured" if not configured else ("ok" if all(v["ok"] for v in configured) else "failing")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m plugins.android.doctor")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = run()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for name in ("adb", "termux"):
            r = report[name]
            state = "not configured" if not r["configured"] else ("OK" if r["ok"] else "FAIL")
            extra = r.get("device") or (f"battery {r['battery_pct']}%" if r.get("battery_pct") is not None else "")
            print(f"{name:7} {state:15} {r.get('error') or extra}")
        print(f"send={'on' if report['allow_send'] else 'off'}  control={'on' if report['allow_control'] else 'off'}"
              f"  sms/day={report['sms_daily_limit']}")
    return {"ok": 0, "failing": 1, "unconfigured": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
