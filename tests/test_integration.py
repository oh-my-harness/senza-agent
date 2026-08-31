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


# ── Hook pipeline integration smoke tests ───────────────────────────────────

from senza_agent.behavior.context_injector import behavior_transform_context
from senza_agent.behavior.wrapup import behavior_should_stop


def test_full_hook_pipeline_advisor_advice_flow():
    """Advisor writes advice to state → transform_context injects it → should_stop continues."""
    from senza_agent.behavior.state import AgentState
    from senza_agent.config import BehaviorConfig
    from senza_agent.behavior.bundle import BehaviorBundle

    state = AgentState()
    config = BehaviorConfig()
    bundle = BehaviorBundle(state, config)

    # Simulate advisor writing advice
    state.last_advice = "Break down the problem into smaller steps."

    # transform_context should inject it
    tctx = behavior_transform_context(state)
    ctx = {
        "system_prompt": "test",
        "messages": [],
        "turn_index": 1,
        "run_id": "test",
        "started_at": "2025-01-01T00:00:00Z",
    }
    result = tctx(ctx)
    assert len(result["messages"]) == 1
    assert "Break down" in result["messages"][0]["content"][0]["text"]
    assert state.last_advice == ""  # consumed

    # should_stop should return False (no remediation, no wrapup)
    sstop = behavior_should_stop(state)
    assert sstop({}) is False


def test_full_hook_pipeline_remediation_flow():
    """Bad completion_report → transform_context injects feedback → should_stop continues."""
    from senza_agent.behavior.state import AgentState

    state = AgentState()
    state.completion_report = {
        "outcome": "done",
        "evidence_type": "artifact",
        "evidence": ["/nonexistent/artifact.py"],
    }

    tctx = behavior_transform_context(state)
    ctx = {
        "system_prompt": "test",
        "messages": [],
        "turn_index": 5,
        "run_id": "test",
        "started_at": "2025-01-01T00:00:00Z",
    }
    result = tctx(ctx)
    assert len(result["messages"]) >= 1
    assert state.needs_remediation is True
    assert state.completion_report is None

    # should_stop must return False to let the model see the feedback
    sstop = behavior_should_stop(state)
    assert sstop({}) is False
    # Flag consumed
    assert state.needs_remediation is False

    # Next call to should_stop (no remediation) should return False too
    # (no wrapup active)
    assert sstop({}) is False


def test_full_hook_pipeline_wrapup_termination():
    """wrapup_turns_left=0 → should_stop returns True (no remediation)."""
    from senza_agent.behavior.state import AgentState

    state = AgentState()
    state.wrapup_turns_left = 0

    sstop = behavior_should_stop(state)
    assert sstop({}) is True


def test_agent_state_covers_tools_shared_fields():
    """AgentState must carry every attribute tools.standard reads via _state.

    agent.py calls set_state(AgentState) — any field the tool callbacks use
    that is missing here crashes at runtime with AttributeError (regression
    guard: load_image/load_video/register_tool/repair/delete were broken this
    way until the fields were added).
    """
    from dataclasses import fields

    from senza_agent.behavior.state import AgentState
    from senza_agent.tools import standard

    agent_fields = {f.name for f in fields(AgentState)}
    for attr in standard._STATE_DEFAULTS:
        assert attr in agent_fields, (
            f"AgentState is missing '{attr}' used by tools.standard"
        )


def test_set_state_fills_missing_fields():
    """set_state on a bare object fills defaults instead of crashing later."""
    from senza_agent.tools import standard

    class Bare:
        pass

    bare = Bare()
    standard.set_state(bare)
    try:
        assert bare.evolved_tools == {}
        assert bare.long_term == []
        assert bare.vision_supported is None
        assert standard.tool_register_tool(
            name="rt", description="d", args_schema={},
            python_code='def run(**kwargs):\n    return {"status": "ok", "output": 1}\n',
        )["status"] == "ok"
    finally:
        standard.set_state(standard._StateRef())
