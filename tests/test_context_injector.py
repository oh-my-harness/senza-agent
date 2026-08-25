"""Tests for the context_injector transform_context hook."""
from senza_agent.behavior.state import AgentState


def test_state_has_new_fields():
    state = AgentState()
    assert state.episodic_required is False
    assert state.concept_required is False
    assert state.needs_remediation is False
    assert state.pending_injections == []
