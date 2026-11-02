#!/usr/bin/env python3
"""Detect installed coding-agent CLIs and print one JSON document for the orchestrator.

Usage: python3 detect_agents.py [--pretty]
Output: {"agents": [{"id", "binary", "path", "version", "headless", "safe_flags", "unsafe_flags",
         "strengths"}], "missing": [...], "git_worktree": bool}
Never runs an agent: it only resolves binaries on PATH and asks for --version (3 s timeout each).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

# id -> (candidate binaries, headless template, safe flags, permissionless flags, strengths)
KNOWN = {
    "claude-code": (["claude"], "claude -p {prompt}", "--permission-mode acceptEdits --max-turns 30",
                    "--dangerously-skip-permissions", "multi-file refactors, reviews, planning"),
    "codex": (["codex"], "codex exec {prompt}", "--sandbox workspace-write",
              "--dangerously-bypass-approvals-and-sandbox", "tests, bug fixes, tight diffs"),
    "cursor": (["cursor-agent"], "cursor-agent -p {prompt} --output-format text", "",
               "--force", "frontend/UI work, quick edits"),
    "opencode": (["opencode"], "opencode run {prompt}", "", "", "model-agnostic, local models"),
    "gemini": (["gemini"], "gemini -p {prompt}", "", "--yolo", "large-context reading, docs"),
    "qwen-code": (["qwen"], "qwen -p {prompt}", "", "--yolo", "free-tier coding"),
    "aider": (["aider"], "aider --message {prompt}", "--no-auto-commits", "--yes-always", "git-native small edits"),
    "openhands": (["openhands"], "openhands --headless -t {prompt}", "", "", "long autonomous tasks"),
    "kimi": (["kimi"], "kimi -p {prompt}", "", "", "long-context coding"),
}


def _version(binary: str) -> str:
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=3, check=False)
        text = (out.stdout or out.stderr).strip().splitlines()
        return text[0][:80] if text else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def detect() -> dict:
    agents, missing = [], []
    for agent_id, (bins, headless, safe, unsafe, strengths) in KNOWN.items():
        found = next(((b, shutil.which(b)) for b in bins if shutil.which(b)), None)
        if not found:
            missing.append(agent_id)
            continue
        binary, path = found
        agents.append({"id": agent_id, "binary": binary, "path": path, "version": _version(binary),
                       "headless": headless, "safe_flags": safe, "unsafe_flags": unsafe,
                       "strengths": strengths})
    return {"agents": agents, "missing": missing, "git_worktree": shutil.which("git") is not None}


if __name__ == "__main__":
    print(json.dumps(detect(), indent=2 if "--pretty" in sys.argv else None))
