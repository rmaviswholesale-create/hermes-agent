"""harness-eval store + scorer: one SQLite file per profile (``$HERMES_HOME/harness-eval/outcomes.db``).

Writers (the hooks) append small rows and never raise; the scorer is a pure function over those rows so
the report, the prune pass and the tests all see the same verdicts. Local only: nothing leaves the
machine, and error text is clipped before it is stored."""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

KIND_TOOL, KIND_SKILL_LOADED, KIND_SKILL_REVISED, KIND_VERDICT = "tool", "skill_loaded", "skill_revised", "verdict"

# Skill bookkeeping calls say nothing about whether a loaded skill helped, so they are counted in tool
# stats but never attributed to a skill's episode.
META_TOOLS = frozenset({"skill_view", "skill_manage", "skills_list", "memory", "todo", "clarify"})

_DETAIL_MAX = 240

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    owner TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT,
    error_type TEXT,
    detail TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_name ON events(kind, name, ts);
"""

_init_lock = threading.Lock()
_initialized: set[str] = set()


def db_path(home: Optional[Path] = None) -> Path:
    """Resolved per call so one process serving several profiles writes into each one's own home."""
    return Path(home or get_hermes_home()) / "harness-eval" / "outcomes.db"


def _connect(home: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    key = str(path)
    with _init_lock:
        if key not in _initialized:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            _initialized.add(key)
    return conn


def _clip(text: Any) -> Optional[str]:
    if text is None:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 1] + "…"


def append(owner: str, kind: str, name: str, *, status: Optional[str] = None, error_type: Optional[str] = None,
           detail: Any = None, duration_ms: Optional[int] = None, ts: Optional[float] = None,
           home: Optional[Path] = None) -> bool:
    """Append one event. Returns False (and logs at DEBUG) instead of raising: a hook must never break a turn."""
    if not name:
        return False
    try:
        conn = _connect(home)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO events (ts, owner, kind, name, status, error_type, detail, duration_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts if ts is not None else time.time(), owner or "default", kind, name, status, error_type,
                     _clip(detail), int(duration_ms) if duration_ms is not None else None))
        finally:
            conn.close()
        return True
    except (sqlite3.Error, OSError, ValueError):
        logger.debug("harness-eval: failed to record %s/%s", kind, name, exc_info=True)
        return False


def load_events(days: float, home: Optional[Path] = None) -> list[dict[str, Any]]:
    since = time.time() - days * 86400
    path = db_path(home)
    if not path.exists():
        return []
    conn = _connect(home)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM events WHERE ts >= ? ORDER BY ts, id", (since,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def purge_older_than(days: float, home: Optional[Path] = None) -> int:
    if not db_path(home).exists():
        return 0
    conn = _connect(home)
    try:
        with conn:
            cur = conn.execute("DELETE FROM events WHERE ts < ?", (time.time() - days * 86400,))
        return cur.rowcount
    finally:
        conn.close()


# --- Scoring ---

@dataclass
class Thresholds:
    days: float = 30.0
    min_calls: int = 10            # attributed tool calls before a skill can be judged
    min_episodes: int = 3          # separate loads before a skill can be judged
    margin: float = 0.15           # how far below the baseline success rate counts as underperforming
    attribution_calls: int = 25    # tool calls after a load that are credited to the skill
    max_revisions: int = 1         # revisions a skill gets before the next failing verdict archives it
    tool_min_calls: int = 10
    tool_error_floor: float = 0.30  # Wilson lower bound of a tool's error rate that flags it as flaky


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion; (0, 1) when there is no data."""
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class _SkillAcc:
    calls: int = 0
    errors: int = 0
    episodes: int = 0
    failed_episodes: int = 0
    good: int = 0
    bad: int = 0
    revisions: int = 0
    last_revised_at: Optional[float] = None
    errors_seen: Counter = field(default_factory=Counter)


def _tool_rows(events: Iterable[dict[str, Any]], th: Thresholds) -> tuple[list[dict[str, Any]], float]:
    calls: Counter = Counter()
    errors: Counter = Counter()
    samples: dict[str, Counter] = defaultdict(Counter)
    for ev in events:
        if ev["kind"] != KIND_TOOL:
            continue
        calls[ev["name"]] += 1
        if ev["status"] == "error":
            errors[ev["name"]] += 1
            samples[ev["name"]][ev["detail"] or ev["error_type"] or "error"] += 1
    work_calls = sum(n for name, n in calls.items() if name not in META_TOOLS)
    work_errors = sum(n for name, n in errors.items() if name not in META_TOOLS)
    baseline = 1.0 - (work_errors / work_calls) if work_calls else 1.0
    rows = []
    for name, n in calls.most_common():
        lo, _hi = wilson(errors[name], n)
        flaky = n >= th.tool_min_calls and lo >= th.tool_error_floor
        rows.append({"tool": name, "calls": n, "errors": errors[name], "error_rate": round(errors[name] / n, 3),
                     "verdict": "flaky" if flaky else ("ok" if n >= th.tool_min_calls else "insufficient_data"),
                     "top_errors": [e for e, _ in samples[name].most_common(3)]})
    return rows, baseline


def _revision_marks(events: list[dict[str, Any]]) -> dict[str, list[float]]:
    marks: dict[str, list[float]] = defaultdict(list)
    for ev in events:
        if ev["kind"] == KIND_SKILL_REVISED:
            marks[ev["name"]].append(ev["ts"])
    return marks


def _close_episode(acc: _SkillAcc, ep: dict[str, int]) -> None:
    if ep["calls"] and ep["errors"] * 2 > ep["calls"]:
        acc.failed_episodes += 1


def _accumulate_skills(events: list[dict[str, Any]], th: Thresholds) -> dict[str, _SkillAcc]:
    """Credit each non-meta tool call to the skills loaded earlier by the same owner (bounded by
    ``attribution_calls``). Only evidence newer than a skill's last revision counts: a patched skill
    starts a fresh trial instead of carrying the old version's failures."""
    marks = _revision_marks(events)
    accs: dict[str, _SkillAcc] = defaultdict(_SkillAcc)
    for name, stamps in marks.items():
        accs[name].revisions, accs[name].last_revised_at = len(stamps), max(stamps)
    active: dict[str, dict[str, dict[str, int]]] = defaultdict(dict)  # owner -> skill -> episode
    for ev in events:
        name, kind = ev["name"], ev["kind"]
        fresh = ev["ts"] >= (accs[name].last_revised_at or 0) if kind != KIND_TOOL else True
        if kind == KIND_SKILL_LOADED and fresh:
            if (prev := active[ev["owner"]].get(name)) is not None:
                _close_episode(accs[name], prev)
            active[ev["owner"]][name] = {"budget": th.attribution_calls, "calls": 0, "errors": 0}
            accs[name].episodes += 1
        elif kind == KIND_VERDICT and fresh:
            accs[name].good += ev["status"] == "good"
            accs[name].bad += ev["status"] == "bad"
        elif kind == KIND_TOOL and name not in META_TOOLS:
            _credit_tool_call(ev, active[ev["owner"]], accs)
    for episodes in active.values():
        for skill, ep in episodes.items():
            _close_episode(accs[skill], ep)
    return accs


def _credit_tool_call(ev: dict[str, Any], episodes: dict[str, dict[str, int]], accs: dict[str, _SkillAcc]) -> None:
    failed = ev["status"] == "error"
    for skill, ep in episodes.items():
        if ep["budget"] <= 0:
            continue
        ep["budget"] -= 1
        ep["calls"] += 1
        acc = accs[skill]
        acc.calls += 1
        if failed:
            ep["errors"] += 1
            acc.errors += 1
            acc.errors_seen[f"{ev['name']}: {ev['detail'] or ev['error_type'] or 'error'}"] += 1


def _skill_verdict(acc: _SkillAcc, baseline: float, th: Thresholds) -> tuple[str, str]:
    """(verdict, reason). Verdicts: keep | underperforming | insufficient_data."""
    if acc.bad >= 2 and acc.bad > acc.good:
        return "underperforming", f"{acc.bad} bad vs {acc.good} good verdicts since last revision"
    if acc.calls < th.min_calls or acc.episodes < th.min_episodes:
        return "insufficient_data", f"{acc.calls} calls over {acc.episodes} loads since last revision"
    _lo, hi = wilson(acc.calls - acc.errors, acc.calls)
    if hi < baseline - th.margin:
        return "underperforming", (f"success ≤ {hi:.0%} (95% upper bound) after loading vs "
                                   f"{baseline:.0%} baseline over {acc.calls} calls")
    return "keep", f"{1 - acc.errors / acc.calls:.0%} success over {acc.calls} calls"


def score(events: list[dict[str, Any]], th: Optional[Thresholds] = None) -> dict[str, Any]:
    th = th or Thresholds()
    tools, baseline = _tool_rows(events, th)
    skills = []
    for name, acc in sorted(_accumulate_skills(events, th).items()):
        if not (acc.episodes or acc.good or acc.bad or acc.revisions):
            continue
        verdict, reason = _skill_verdict(acc, baseline, th)
        action = {"keep": "keep", "insufficient_data": "wait"}.get(verdict)
        if action is None:
            action = "revise" if acc.revisions < th.max_revisions else "archive"
        skills.append({
            "skill": name, "verdict": verdict, "action": action, "reason": reason,
            "loads": acc.episodes, "failed_loads": acc.failed_episodes, "calls": acc.calls, "errors": acc.errors,
            "good": acc.good, "bad": acc.bad, "revisions": acc.revisions,
            "top_errors": [e for e, _ in acc.errors_seen.most_common(3)],
        })
    order = {"archive": 0, "revise": 1, "keep": 2, "wait": 3}
    skills.sort(key=lambda r: (order[r["action"]], r["skill"]))
    return {"generated_at": time.time(), "window_days": th.days, "baseline_success": round(baseline, 3),
            "events": len(events), "thresholds": asdict(th), "skills": skills, "tools": tools}
