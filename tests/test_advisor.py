"""Tests for the advisor behavior module."""
from __future__ import annotations

from unittest.mock import patch

from senza_agent.behavior.advisor import (
    _build_advisor_context,
    _build_tools_catalog,
    _extract_user_injections,
    advisor_after_turn,
    ensure_progress_log,
    inject_advisor_advice,
    run_advisor,
    should_trigger_advisor,
)
from senza_agent.behavior.state import AgentState
from senza_agent.config import BehaviorConfig


# ── advisor_after_turn: turn counting ───────────────────────────────────────


def test_advisor_increments_turn_count():
    state = AgentState()
    config = BehaviorConfig(advisor_interval=100)
    cb = advisor_after_turn(state, config)
    cb({})
    assert state.turn_count == 1


def test_advisor_skips_until_interval():
    state = AgentState()
    config = BehaviorConfig(advisor_interval=3)
    runner = advisor_after_turn(state, config)
    runner({})
    assert state.last_advice == ""
    runner({})
    assert state.last_advice == ""
    runner({})
    assert state.turn_count == 3


# ── should_trigger_advisor ───────────────────────────────────────────────────


def test_advisor_triggers_on_request():
    state = AgentState()
    state.advisor_requested = True
    config = BehaviorConfig(advisor_interval=100)
    # should trigger immediately when requested
    assert should_trigger_advisor(state, config) is True


def test_advisor_periodic_trigger():
    state = AgentState()
    config = BehaviorConfig(advisor_interval=5)
    # turn 0 never triggers periodic
    state.turn_count = 0
    assert should_trigger_advisor(state, config) is False
    state.turn_count = 5
    assert should_trigger_advisor(state, config) is True
    state.turn_count = 4
    assert should_trigger_advisor(state, config) is False


def test_advisor_stall_trigger():
    # No advice yet and we've crossed one full interval with no advisor run.
    state = AgentState()
    config = BehaviorConfig(advisor_interval=3)
    state.turn_count = 3
    state.last_advice = ""
    assert should_trigger_advisor(state, config) is True  # periodic also true here

    # Simulate: periodic just fired, advice set, last_iter updated.
    state.last_advice = "do something"
    state.meta["_advisor_last_iter"] = 3
    state.turn_count = 4
    # Not periodic (4 % 3 != 0), not requested, advice present → no stall trigger.
    assert should_trigger_advisor(state, config) is False


def test_advisor_no_trigger_default():
    state = AgentState()
    config = BehaviorConfig(advisor_interval=100)
    state.turn_count = 1
    assert should_trigger_advisor(state, config) is False


# ── run_advisor: must not raise, must not call LLM without credentials ──────


def test_run_advisor_silent_without_api_key(tmp_path, monkeypatch):
    # Ensure no API key leaks in from the environment.
    monkeypatch.delenv("SENZA_AGENT_API_KEY", raising=False)
    monkeypatch.delenv("SENZA_AGENT_API_BASE", raising=False)
    state = AgentState()
    state.goal = "write a haiku"
    state.run_dir = str(tmp_path)
    config = BehaviorConfig(advisor_interval=1)
    # Should not raise even though it cannot make an LLM call.
    run_advisor(state, config)
    # last_advice stays empty (no credentials → logged as failed, no advice).
    assert state.last_advice == ""
    # A failed-call log entry was written.
    log_path = tmp_path / "advisor_log.jsonl"
    assert log_path.is_file()
    import json
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["status"] == "failed"


def test_run_advisor_catches_llm_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("SENZA_AGENT_API_KEY", "sk-test")
    state = AgentState()
    state.goal = "write a haiku"
    state.run_dir = str(tmp_path)
    config = BehaviorConfig(advisor_interval=1)

    # Force the lazy senza import path to raise when building the provider.
    import sys
    import types

    fake_senza = types.ModuleType("senza")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated provider failure")

    class _Providers:
        openai = staticmethod(_boom)

    class _HarnessBuilder:
        def __init__(self, *a, **kw):
            raise RuntimeError("should not reach builder")

    fake_senza.providers = _Providers()
    fake_senza.HarnessBuilder = _HarnessBuilder
    monkeypatch.setitem(sys.modules, "senza", fake_senza)

    # Must not raise.
    run_advisor(state, config)
    assert state.last_advice == ""
    log_path = tmp_path / "advisor_log.jsonl"
    assert log_path.is_file()


# ── inject_advisor_advice ────────────────────────────────────────────────────


def test_inject_advisor_advice_calls_steer():
    state = AgentState()
    state.last_advice = "focus on tests first"

    calls: list[str] = []

    class FakeHarness:
        def steer(self, text: str) -> None:
            calls.append(text)

    inject_advisor_advice(state, FakeHarness())
    assert calls == ["focus on tests first"]


def test_inject_advisor_advice_noop_when_empty():
    state = AgentState()
    state.last_advice = ""

    class FakeHarness:
        def steer(self, text: str) -> None:
            raise AssertionError("steer should not be called with empty advice")

    inject_advisor_advice(state, FakeHarness())  # must not raise


def test_inject_advisor_advice_swallows_errors():
    state = AgentState()
    state.last_advice = "something"

    class FakeHarness:
        def steer(self, text: str) -> None:
            raise RuntimeError("harness down")

    # Must not raise.
    inject_advisor_advice(state, FakeHarness())


# ── ensure_progress_log ──────────────────────────────────────────────────────


def test_ensure_progress_log_summarises_state():
    state = AgentState()
    state.goal = "build feature X"
    state.turn_count = 7
    state.meta["scratchpad"] = "wrote module A"
    summary = ensure_progress_log(state)
    assert "Turn: 7" in summary
    assert "build feature X" in summary
    assert "wrote module A" in summary
    # Cached onto meta for the advisor context to read.
    assert state.meta["_progress_log"] == summary
    assert state.meta["_progress_log_method"] == "state_summary"


def test_ensure_progress_log_returns_cached():
    state = AgentState()
    state.meta["_progress_log"] = "cached entry"
    assert ensure_progress_log(state) == "cached entry"


# ── context builders ─────────────────────────────────────────────────────────


def test_extract_user_injections_from_meta():
    state = AgentState()
    state.meta["_user_injections"] = [
        {"content": "stop and review", "iter": 3, "source": "explicit"},
        {"content": "", "iter": 4},  # filtered out
    ]
    items = _extract_user_injections(state)
    assert len(items) == 1
    assert items[0]["content"] == "stop and review"
    assert items[0]["source"] == "explicit"


def test_extract_user_injections_scans_session_entries():
    state = AgentState()
    state.meta["_session_entries"] = [
        {"role": "user", "content": "[User Injection]\nchange approach"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "normal message"},
    ]
    items = _extract_user_injections(state)
    assert len(items) == 1
    assert items[0]["content"] == "change approach"
    assert items[0]["source"] == "scanned"


def test_build_tools_catalog_from_list():
    state = AgentState()
    state.tools = ["search", "read_file", "write_file"]
    catalog = _build_tools_catalog(state)
    assert "- read_file" in catalog
    assert "- search" in catalog
    assert "- write_file" in catalog


def test_build_advisor_context_contains_sections():
    state = AgentState()
    state.goal = "achieve world domination"
    state.turn_count = 5
    state.tools = ["search"]
    state.meta["scratchpad"] = "step 1 done"
    ctx = _build_advisor_context(state, BehaviorConfig())
    assert "achieve world domination" in ctx
    assert "step 1 done" in ctx
    assert "- search" in ctx


# ── advisor_after_turn integration (no LLM) ─────────────────────────────────


def test_advisor_after_turn_does_not_call_llm_before_interval(tmp_path, monkeypatch):
    monkeypatch.delenv("SENZA_AGENT_API_KEY", raising=False)
    state = AgentState()
    state.run_dir = str(tmp_path)
    config = BehaviorConfig(advisor_interval=100)
    cb = advisor_after_turn(state, config)
    # Patch run_advisor to assert it is NOT called when the trigger is false.
    with patch("senza_agent.behavior.advisor.run_advisor") as mock_run:
        cb({})
    assert state.turn_count == 1
    mock_run.assert_not_called()


def test_advisor_after_turn_fires_on_request(tmp_path, monkeypatch):
    monkeypatch.delenv("SENZA_AGENT_API_KEY", raising=False)
    state = AgentState()
    state.run_dir = str(tmp_path)
    state.advisor_requested = True
    config = BehaviorConfig(advisor_interval=100)
    cb = advisor_after_turn(state, config)
    with patch("senza_agent.behavior.advisor.run_advisor") as mock_run:
        cb({})
    assert state.turn_count == 1
    mock_run.assert_called_once()
    # Request flag consumed after firing.
    assert state.advisor_requested is False
