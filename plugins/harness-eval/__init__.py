"""harness-eval plugin — outcome scoring for Hermes's own harness (skills + tools).

``post_tool_call`` records every tool call's ok/error; ``on_skill_lifecycle`` records skill loads and
revisions (create/patch/edit). The scorer credits tool calls made after a skill load to that skill and
compares the result with the profile's baseline, so a skill whose loads are followed by failures is
flagged for revision, and archived (through ``hermes curator archive``, which keeps pinned, bundled and
hub skills safe) once a revision did not fix it. ``/harness-eval good|bad <skill>`` adds explicit
verdicts. The ``harness-eval:self-eval`` skill and the weekly blueprint close the loop.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

from . import store

logger = logging.getLogger(__name__)

_REVISION_ACTIONS = frozenset({"created", "patched", "edited"})
_PLUGIN_DIR = Path(__file__).resolve().parent


def _owner(task_id: str, session_id: str) -> str:
    return task_id or session_id or "default"


# --- Hooks (observers: never raise) ---

def _on_post_tool_call(tool_name: str = "", task_id: str = "", session_id: str = "", status: str = "ok",
                       error_type: Optional[str] = None, error_message: Optional[str] = None,
                       duration_ms: int = 0, **_: Any) -> None:
    store.append(_owner(task_id, session_id), store.KIND_TOOL, tool_name, status=status or "ok",
                 error_type=error_type, detail=error_message if status == "error" else None,
                 duration_ms=duration_ms)


def _on_skill_lifecycle(action: str = "", skill_name: str = "", task_id: str = "", session_id: str = "",
                        **_: Any) -> None:
    if action == "loaded":
        kind = store.KIND_SKILL_LOADED
    elif action in _REVISION_ACTIONS:
        kind = store.KIND_SKILL_REVISED
    else:
        return
    store.append(_owner(task_id, session_id), kind, skill_name, detail=action)


# --- Shared actions (slash command + CLI) ---

def build_report(th: Optional[store.Thresholds] = None) -> dict[str, Any]:
    th = th or store.Thresholds()
    return store.score(store.load_events(th.days), th)


def format_report(report: dict[str, Any], *, limit: int = 15) -> str:
    lines = [f"harness-eval: {report['events']} events over {report['window_days']:g}d, "
             f"baseline tool success {report['baseline_success']:.0%}", "", "Skills:"]
    skills = report["skills"]
    if not skills:
        lines.append("  (no skill loads recorded yet)")
    for row in skills[:limit]:
        lines.append(f"  [{row['action']:>7}] {row['skill']}: {row['reason']}")
        lines.extend(f"            · {err}" for err in row["top_errors"][:2] if row["action"] in {"revise", "archive"})
    flaky = [t for t in report["tools"] if t["verdict"] == "flaky"]
    lines += ["", "Flaky tools:" if flaky else "Flaky tools: none"]
    for row in flaky[:limit]:
        lines.append(f"  {row['tool']}: {row['errors']}/{row['calls']} errors ({row['error_rate']:.0%})")
        lines.extend(f"      · {err}" for err in row["top_errors"][:2])
    return "\n".join(lines)


def _archive_via_curator(skill: str) -> tuple[bool, str]:
    """Archive through the curator CLI so its guards (pinned, bundled, hub, external) apply unchanged."""
    env = {**os.environ, "HERMES_HOME": str(get_hermes_home())}
    try:
        proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "curator", "archive", skill],
                              capture_output=True, text=True, timeout=120, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"curator archive failed to run: {exc}"
    output = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 0, output[-1] if output else f"exit {proc.returncode}"


def prune(report: dict[str, Any], *, apply: bool) -> list[dict[str, Any]]:
    results = []
    for row in report["skills"]:
        if row["action"] != "archive":
            continue
        ok, message = _archive_via_curator(row["skill"]) if apply else (True, "dry run")
        results.append({"skill": row["skill"], "archived": ok and apply, "message": message})
    return results


# --- Slash command ---

_HELP = """/harness-eval — score skills and tools by outcome.
  /harness-eval              report for the last 30 days
  /harness-eval good <skill> record that a skill worked
  /harness-eval bad <skill> [why]  record that a skill misled or failed"""


def _handle_slash(raw_args: str, session_id: str = "", **_: Any) -> str:
    argv = raw_args.strip().split(maxsplit=2)
    if not argv or argv[0] == "report":
        return format_report(build_report())
    if argv[0] in {"good", "bad"} and len(argv) >= 2:
        store.append(session_id or "default", store.KIND_VERDICT, argv[1], status=argv[0],
                     detail=argv[2] if len(argv) > 2 else None)
        return f"harness-eval: recorded '{argv[0]}' for {argv[1]}."
    return _HELP


# --- CLI: hermes harness-eval ... ---

def _thresholds_from_args(args: argparse.Namespace) -> store.Thresholds:
    return store.Thresholds(days=args.days, min_calls=args.min_calls, min_episodes=args.min_loads)


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="harness_eval_cmd")
    for name, help_text in (("report", "Score skills and tools by outcome"),
                            ("prune", "Archive skills that failed again after a revision (dry run by default)")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--days", type=float, default=30.0)
        p.add_argument("--min-calls", type=int, default=10)
        p.add_argument("--min-loads", type=int, default=3)
        p.add_argument("--json", action="store_true", help="Structured output for agents")
        if name == "prune":
            p.add_argument("--apply", action="store_true", help="Actually archive (default: dry run)")
    vac = sub.add_parser("vacuum", help="Drop events older than N days")
    vac.add_argument("--days", type=float, default=90.0)


def _handle_cli(args: argparse.Namespace) -> int:
    cmd = getattr(args, "harness_eval_cmd", None) or "report"
    if cmd == "vacuum":
        print(json.dumps({"deleted": store.purge_older_than(args.days)}))
        return 0
    if not hasattr(args, "days"):  # bare `hermes harness-eval`
        args = argparse.Namespace(days=30.0, min_calls=10, min_loads=3, json=False)
    report = build_report(_thresholds_from_args(args))
    if cmd == "prune":
        results = prune(report, apply=args.apply)
        failed = [r for r in results if args.apply and not r["archived"]]
        print(json.dumps({"applied": args.apply, "results": results}, indent=2) if args.json
              else "\n".join(f"{r['skill']}: {r['message']}" for r in results) or "harness-eval: nothing to archive")
        return 1 if failed else 0
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    return 0


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_skill_lifecycle", _on_skill_lifecycle)
    ctx.register_command("harness-eval", handler=_handle_slash, args_hint="[good|bad <skill> [why]]",
                         description="Score skills and tools by outcome; record skill verdicts.")
    ctx.register_cli_command("harness-eval", help="Outcome scoring for skills and tools", setup_fn=_setup_cli,
                             handler_fn=_handle_cli,
                             description="Report, revise-queue and prune Hermes skills by measured outcome.")
    ctx.register_skill("self-eval", _PLUGIN_DIR / "skills" / "self-eval" / "SKILL.md",
                       description="Revise or retire skills that measurably fail.")
    ctx.register_automation_blueprint(
        "weekly-self-eval",
        title="Weekly harness self-eval",
        description="Score skills and tools by outcome, revise the failing ones, archive repeat failures.",
        category="weekly",
        schedule_template="{minute} {hour} * * {dow}",
        prompt_template="Run the harness-eval:self-eval skill's procedure and report what changed.",
        slots=[
            {"name": "time", "type": "time", "label": "What time?", "default": "03:00"},
            {"name": "day", "type": "enum", "label": "Which day?", "default": "sunday",
             "options": ["sunday", "monday", "saturday"]},
            {"name": "deliver", "type": "enum", "label": "Where?", "default": "local",
             "options": ["origin", "local"], "strict": False},
        ],
        skills=["harness-eval:self-eval"],
        tags=["weekly", "maintenance"],
    )
