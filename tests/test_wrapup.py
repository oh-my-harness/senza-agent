"""Tests for the wrap-up window."""
from __future__ import annotations

from senza_agent.behavior.state import AgentState
from senza_agent.config import BehaviorConfig
from senza_agent.behavior.wrapup import wrapup_window, wrapup_stop


def test_not_triggered_under_budget():
    state = AgentState()
    config = BehaviorConfig(budget_limit=10.0, wrapup_turns=2)
    result = wrapup_window(state, config)({"usage": {"total_cost": 1.0}})
    assert result is None
    assert state.wrapup_turns_left is None


def test_triggered_on_budget_exhaustion():
    state = AgentState()
    config = BehaviorConfig(budget_limit=10.0, wrapup_turns=2)
    result = wrapup_window(state, config)({"usage": {"total_cost": 11.0}})
    assert result is not None
    assert state.wrapup_turns_left == 2


def test_decrements_turns():
    state = AgentState()
    state.wrapup_turns_left = 2
    config = BehaviorConfig(budget_limit=10.0, wrapup_turns=2)
    wrapup_window(state, config)({"usage": {"total_cost": 11.0}})
    assert state.wrapup_turns_left == 1


def test_stop_when_exhausted():
    state = AgentState()
    state.wrapup_turns_left = 0
    assert wrapup_stop(state)({}) is True


def test_stop_when_not_started():
    state = AgentState()
    assert wrapup_stop(state)({}) is False
