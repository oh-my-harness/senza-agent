"""Integration tests for senza-agent package."""
from __future__ import annotations


def test_behavior_importable():
    from senza_agent.behavior import AgentState, BehaviorBundle
    assert AgentState is not None
    assert BehaviorBundle is not None


def test_agent_state_fields():
    from senza_agent.behavior.state import AgentState
    state = AgentState()
    assert state.completion_report is None
    assert state.turn_count == 0
    assert state.last_advice == ""
    assert state.budget_exhausted is False
    assert state.wrapup_turns_left is None
    assert state.goal == ""
    assert state.advisor_requested is False
    assert state.run_dir == ""
    assert state.tools == []


def test_config_importable():
    from senza_agent.config import Config, load_config
    assert Config is not None
    assert load_config is not None


def test_system_prompt_importable():
    from senza_agent.system_prompt import build_system_prompt
    from senza_agent.config import Config
    assert len(build_system_prompt(Config())) > 0


def test_agent_importable():
    from senza_agent.agent import create_agent
    assert create_agent is not None


def test_inspector_importable():
    from senza_agent.inspector import setup_inspector
    assert setup_inspector is not None


def test_persistence_importable():
    from senza_agent.persistence import RunPersistence
    assert RunPersistence is not None


def test_cli_importable():
    from senza_agent.cli import main
    assert main is not None


def test_tools_registry_importable():
    # registry.py imports senza, which may not be installed in test env
    # Just check the module exists
    import senza_agent.tools.registry as reg
    assert hasattr(reg, "get_standard_tools")


def test_graph_importable():
    from senza_agent.behavior.graph import ExecutionGraph
    assert ExecutionGraph is not None


def test_timing_importable():
    from senza_agent.behavior.timing import TimeLedger
    assert TimeLedger is not None


def test_artifact_index_importable():
    from senza_agent.behavior.artifact_index import ArtifactIndex
    assert ArtifactIndex is not None


def test_confidence_importable():
    from senza_agent.behavior.confidence import compute_confidence
    assert compute_confidence is not None


def test_depcheck_importable():
    from senza_agent.depcheck import check_dependencies
    assert check_dependencies is not None


def test_i18n_importable():
    from senza_agent.i18n import t
    assert callable(t)


def test_acceptance_gate_importable():
    from senza_agent.behavior.acceptance_gate import (
        acceptance_gate_tools,
        acceptance_validator,
    )
    assert acceptance_gate_tools is not None
    assert acceptance_validator is not None


def test_advisor_importable():
    from senza_agent.behavior.advisor import (
        advisor_after_turn,
        should_trigger_advisor,
        run_advisor,
    )
    assert advisor_after_turn is not None
    assert should_trigger_advisor is not None
    assert run_advisor is not None


def test_wrapup_importable():
    from senza_agent.behavior.wrapup import wrapup_window, wrapup_stop
    assert wrapup_window is not None
    assert wrapup_stop is not None


def test_async_manager_importable():
    from senza_agent.tools.async_manager import AsyncJobManager
    assert AsyncJobManager is not None


def test_watcher_importable():
    from senza_agent.tools.watcher import WatcherManager
    assert WatcherManager is not None
