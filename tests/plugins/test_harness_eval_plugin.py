"""harness-eval plugin: hooks record outcomes per profile home, the scorer turns them into
keep / wait / revise / archive verdicts, revisions start a fresh trial, prune goes through the
curator CLI, and the plugin loads through real discovery only when enabled."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "harness-eval"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


@pytest.fixture()
def plugin():
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.harness_eval", _PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(_PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.harness_eval"
    sys.modules["hermes_plugins.harness_eval"] = mod
    spec.loader.exec_module(mod)
    yield mod
    for name in [n for n in sys.modules if n.startswith("hermes_plugins.harness_eval")]:
        del sys.modules[name]


def _tool(plugin, name, *, task="t1", ok=True, err="boom"):
    plugin._on_post_tool_call(tool_name=name, task_id=task, status="ok" if ok else "error",
                              error_type=None if ok else "tool_error", error_message=None if ok else err,
                              duration_ms=5)


def _load(plugin, skill, *, task="t1"):
    plugin._on_skill_lifecycle(action="loaded", skill_name=skill, task_id=task)


def _baseline(plugin, n=60):
    for i in range(n):
        _tool(plugin, "terminal", task=f"base{i % 5}")


def _row(report, skill):
    return next(r for r in report["skills"] if r["skill"] == skill)


def test_hooks_write_into_the_active_profile_home(plugin, home):
    _tool(plugin, "terminal")
    _load(plugin, "deploy")
    plugin._on_skill_lifecycle(action="archived", skill_name="deploy", task_id="t1")  # ignored action
    events = plugin.store.load_events(1)
    assert [(e["kind"], e["name"]) for e in events] == [("tool", "terminal"), ("skill_loaded", "deploy")]
    assert (home / "harness-eval" / "outcomes.db").exists()


def test_failing_skill_is_queued_for_revision_and_working_skill_kept(plugin):
    _baseline(plugin)
    for i in range(4):
        _load(plugin, "bad-skill", task=f"b{i}")
        for _ in range(4):
            _tool(plugin, "terminal", task=f"b{i}", ok=False, err="command not found: kubectl")
        _load(plugin, "good-skill", task=f"g{i}")
        for _ in range(4):
            _tool(plugin, "terminal", task=f"g{i}")
    report = plugin.build_report()
    bad, good = _row(report, "bad-skill"), _row(report, "good-skill")
    assert (bad["verdict"], bad["action"]) == ("underperforming", "revise")
    assert bad["failed_loads"] == 4 and "kubectl" in bad["top_errors"][0]
    assert (good["verdict"], good["action"]) == ("keep", "keep")
    assert report["skills"][0]["skill"] == "bad-skill"  # actionable rows first


def test_revision_starts_a_fresh_trial_and_repeat_failure_archives(plugin):
    _baseline(plugin)
    for i in range(4):
        _load(plugin, "flaky-skill", task=f"a{i}")
        for _ in range(4):
            _tool(plugin, "terminal", task=f"a{i}", ok=False)
    plugin._on_skill_lifecycle(action="patched", skill_name="flaky-skill", task_id="fix")
    fresh = _row(plugin.build_report(), "flaky-skill")
    assert (fresh["action"], fresh["calls"], fresh["revisions"]) == ("wait", 0, 1)
    for i in range(4):
        _load(plugin, "flaky-skill", task=f"c{i}")
        for _ in range(4):
            _tool(plugin, "terminal", task=f"c{i}", ok=False)
    assert _row(plugin.build_report(), "flaky-skill")["action"] == "archive"


def test_meta_tools_and_other_tasks_are_not_credited(plugin):
    _load(plugin, "s", task="mine")
    _tool(plugin, "skill_view", task="mine", ok=False)
    _tool(plugin, "terminal", task="someone-else", ok=False)
    _tool(plugin, "terminal", task="mine")
    row = _row(plugin.build_report(), "s")
    assert (row["calls"], row["errors"]) == (1, 0)


def test_bad_verdicts_flag_a_skill_without_enough_calls(plugin):
    _load(plugin, "s")
    assert plugin._handle_slash("bad s gave a stale API", session_id="sess").startswith("harness-eval: recorded")
    plugin._handle_slash("bad s", session_id="sess")
    row = _row(plugin.build_report(), "s")
    assert (row["verdict"], row["action"], row["bad"]) == ("underperforming", "revise", 2)
    assert plugin._handle_slash("nonsense").startswith("/harness-eval")


def test_flaky_tool_detection(plugin):
    for i in range(30):
        _tool(plugin, "web_extract", ok=i % 3 == 0, err="HTTP 429")
        _tool(plugin, "read_file")
    tools = {t["tool"]: t for t in plugin.build_report()["tools"]}
    assert tools["web_extract"]["verdict"] == "flaky" and tools["web_extract"]["top_errors"] == ["HTTP 429"]
    assert tools["read_file"]["verdict"] == "ok"


def test_prune_is_dry_by_default_and_applies_through_curator_cli(plugin, monkeypatch, home):
    report = {"skills": [{"skill": "old", "action": "archive"}, {"skill": "keepme", "action": "keep"}]}
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["env"]["HERMES_HOME"]))
        return subprocess.CompletedProcess(cmd, 0, stdout="archived to x\n", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    assert plugin.prune(report, apply=False) == [{"skill": "old", "archived": False, "message": "dry run"}]
    assert calls == []
    assert plugin.prune(report, apply=True)[0]["archived"] is True
    assert calls[0][0][-3:] == ["curator", "archive", "old"] and calls[0][1] == str(home)


def test_cli_report_json(plugin, capsys):
    _tool(plugin, "terminal")
    args = plugin.argparse.Namespace(harness_eval_cmd="report", days=30.0, min_calls=10, min_loads=3, json=True)
    assert plugin._handle_cli(args) == 0
    assert json.loads(capsys.readouterr().out)["events"] == 1


def test_wilson_bounds():
    spec = importlib.util.spec_from_file_location("he_store", _PLUGIN_DIR / "store.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["he_store"] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("he_store", None)
    assert mod.wilson(0, 0) == (0.0, 1.0)
    lo, hi = mod.wilson(5, 10)
    assert 0.2 < lo < 0.5 < hi < 0.8


def test_discovered_but_inert_until_enabled_then_registers(home):
    import hermes_yaml as yaml
    from hermes_cli import plugins as pmod

    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    assert not mgr._plugins["harness-eval"].enabled

    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["harness-eval"]}}))
    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["harness-eval"]
    assert loaded.enabled, loaded.error
    assert {"post_tool_call", "on_skill_lifecycle"} <= set(mgr._hooks)
    assert "harness-eval:self-eval" in mgr._plugin_skills
    assert "harness-eval" in mgr._cli_commands
