"""planning-files plugin — re-inject task_plan.md / findings.md / progress.md every turn.

The plan lives on disk, not in the chat window: ``pre_llm_call`` reads the three files from the
session working directory and appends them to the current user message as context. Nothing is
injected until ``task_plan.md`` exists (``/planfiles init`` writes templates), so ordinary chats
pay zero tokens. Only the user message is extended, never the system prompt, so the cached prefix
survives. Each file is capped (head + tail kept) so a runaway findings log cannot eat the context.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PLAN_FILES = ("task_plan.md", "findings.md", "progress.md")
# Per-file cap in characters; head and tail are kept around an elision marker.
MAX_FILE_CHARS = 6000
_globally_disabled = False

TEMPLATES: dict[str, str] = {
    "task_plan.md": (
        "# Task plan\n\n## Goal\n<one sentence: what done looks like>\n\n"
        "## Phases\n- [ ] 1. <micro-task>\n- [ ] 2. <micro-task>\n- [ ] 3. <micro-task>\n\n"
        "## Decisions\n- <decision> — <why>\n\n## Errors\n| Error | Attempt | Resolution |\n|---|---|---|\n"
    ),
    "findings.md": "# Findings\n\n<!-- Bugs hit, root causes, fixes, key learnings. Never repeat a failed approach. -->\n",
    "progress.md": "# Progress\n\n<!-- Timestamped log: what finished, test results, next step. -->\n",
}

RULES = (
    "Planning-with-files rules: re-read task_plan.md before major decisions; after every 2 "
    "view/search/browse actions save key findings to findings.md; after each phase tick it in "
    "task_plan.md and log it in progress.md; log every error; 3-strike rule: diagnose, then a "
    "different approach, then rethink assumptions, then ask the user."
)


def _plan_dir() -> Path:
    """Session working directory (profile/gateway aware), falling back to the process cwd."""
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return Path(resolve_agent_cwd())
    except (ImportError, RuntimeError, OSError) as exc:  # older core / refused scope: plain cwd
        logger.debug("planning-files: session cwd unavailable, using process cwd: %s", exc)
        return Path(os.getcwd())


def _clip(text: str, limit: int = MAX_FILE_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: limit // 2], text[-limit // 2 :]
    return f"{head}\n\n[... {len(text) - limit} chars elided by planning-files ...]\n\n{tail}"


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def build_context(directory: Path) -> Optional[str]:
    """Return the injected block, or None when no task_plan.md exists in ``directory``."""
    if not (directory / "task_plan.md").is_file():
        return None
    parts = [f"<planning_files dir=\"{directory}\">", RULES]
    for name in PLAN_FILES:
        body = _read(directory / name)
        if body is None:
            parts.append(f"## {name}\n(missing — create it)")
            continue
        parts.append(f"## {name}\n{_clip(body.strip())}")
    parts.append("</planning_files>")
    return "\n\n".join(parts)


def _on_pre_llm_call(**_kwargs: Any) -> Optional[dict[str, str]]:
    if _globally_disabled:
        return None
    try:
        block = build_context(_plan_dir())
    except OSError as exc:  # fail open: never break a turn over a planning file
        logger.debug("planning-files: skipped injection: %s", exc)
        return None
    return {"context": block} if block else None


def init_files(directory: Path) -> list[str]:
    """Write templates for whichever of the three files are missing; return the names created."""
    created = []
    for name, body in TEMPLATES.items():
        target = directory / name
        if not target.exists():
            target.write_text(body, encoding="utf-8")
            created.append(name)
    return created


def _status(directory: Path) -> str:
    lines = [f"planning-files dir: {directory}", f"injection: {'off' if _globally_disabled else 'on'}"]
    for name in PLAN_FILES:
        p = directory / name
        lines.append(f"  {name}: {p.stat().st_size} bytes" if p.is_file() else f"  {name}: missing")
    return "\n".join(lines)


_HELP = (
    "/planfiles init   create task_plan.md, findings.md, progress.md in the working dir\n"
    "/planfiles status show where the files are and whether injection is on\n"
    "/planfiles off    stop injecting (this process)\n"
    "/planfiles on     resume injecting"
)


def _handle_slash(raw_args: str) -> str:
    global _globally_disabled
    cmd = (raw_args or "").strip().split()[:1]
    directory = _plan_dir()
    if not cmd or cmd[0] in {"help", "-h", "--help"}:
        return _HELP
    if cmd[0] == "init":
        created = init_files(directory)
        return f"Created {', '.join(created)} in {directory}" if created else f"All three files already exist in {directory}"
    if cmd[0] == "status":
        return _status(directory)
    if cmd[0] in {"off", "on"}:
        _globally_disabled = cmd[0] == "off"
        return f"planning-files injection {cmd[0]}"
    return f"Unknown subcommand: {cmd[0]}\n\n{_HELP}"


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_command("planfiles", handler=_handle_slash,
                         description="Planning with files: init/status/off/on for task_plan.md, findings.md, progress.md.")
