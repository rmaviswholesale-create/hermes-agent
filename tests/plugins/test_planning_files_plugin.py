"""planning-files plugin: injects the three plan files only when task_plan.md exists, caps file
size, honours /planfiles off|on, and /planfiles init writes missing templates without clobbering."""

from __future__ import annotations

import importlib

import pytest

pf = importlib.import_module("plugins.planning-files")


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "_plan_dir", lambda: tmp_path)
    monkeypatch.setattr(pf, "_globally_disabled", False)
    return tmp_path


def test_no_plan_file_injects_nothing(workdir):
    (workdir / "findings.md").write_text("x")
    assert pf._on_pre_llm_call(session_id="s", user_message="hi") is None


def test_injects_all_three_files(workdir):
    (workdir / "task_plan.md").write_text("# Plan\n- [ ] ship it")
    (workdir / "findings.md").write_text("bug: flaky test")
    out = pf._on_pre_llm_call(session_id="s")
    ctx = out["context"]
    assert "ship it" in ctx and "flaky test" in ctx
    assert "## progress.md\n(missing" in ctx
    assert ctx.startswith("<planning_files") and ctx.endswith("</planning_files>")


def test_large_file_is_clipped(workdir):
    (workdir / "task_plan.md").write_text("A" * 5000 + "MIDDLE" + "Z" * 5000)
    ctx = pf._on_pre_llm_call()["context"]
    assert "elided by planning-files" in ctx and "MIDDLE" not in ctx
    assert "AAAA" in ctx and "ZZZZ" in ctx


def test_off_on_toggle(workdir):
    (workdir / "task_plan.md").write_text("plan")
    assert "off" in pf._handle_slash("off")
    assert pf._on_pre_llm_call() is None
    pf._handle_slash("on")
    assert pf._on_pre_llm_call() is not None


def test_init_creates_missing_without_overwriting(workdir):
    (workdir / "findings.md").write_text("keep me")
    msg = pf._handle_slash("init")
    assert "task_plan.md" in msg and "progress.md" in msg and "findings.md" not in msg
    assert (workdir / "findings.md").read_text() == "keep me"
    assert "already exist" in pf._handle_slash("init")


def test_register_wires_hook_and_command():
    calls = []

    class Ctx:
        def register_hook(self, name, fn):
            calls.append(("hook", name))

        def register_command(self, name, handler, description=""):
            calls.append(("cmd", name))

    pf.register(Ctx())
    assert ("hook", "pre_llm_call") in calls and ("cmd", "planfiles") in calls
