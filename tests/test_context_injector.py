"""Tests for the context_injector transform_context hook."""
from senza_agent.behavior.state import AgentState


def test_state_has_new_fields():
    state = AgentState()
    assert state.episodic_required is False
    assert state.concept_required is False
    assert state.needs_remediation is False
    assert state.pending_injections == []

from senza_agent.behavior.context_injector import behavior_transform_context


def _make_ctx(messages=None):
    """Minimal transform_context ctx dict."""
    return {
        "system_prompt": "You are a helpful agent.",
        "messages": messages or [],
        "turn_index": 0,
        "run_id": "test-run",
        "started_at": "2025-01-01T00:00:00Z",
    }


def test_transform_context_noop_when_nothing_pending():
    state = AgentState()
    hook = behavior_transform_context(state)
    ctx = _make_ctx([{"role": "user", "content": [{"type": "text", "text": "hi"}], "timestamp": "2025-01-01T00:00:00Z"}])
    result = hook(ctx)
    assert result["messages"] == ctx["messages"]


def test_transform_context_injects_advisor_advice():
    state = AgentState()
    state.last_advice = "Focus on writing tests first."
    hook = behavior_transform_context(state)
    ctx = _make_ctx([])
    result = hook(ctx)
    assert len(result["messages"]) == 1
    msg = result["messages"][0]
    assert msg["role"] == "user"
    text = msg["content"][0]["text"]
    assert "advisor" in text.lower()
    assert "Focus on writing tests first." in text
    # Advice is consumed after injection
    assert state.last_advice == ""


def test_transform_context_injects_pending_injections():
    state = AgentState()
    state.pending_injections = ["[bg] job_abc completed", "[env] watcher: CPU high"]
    hook = behavior_transform_context(state)
    ctx = _make_ctx([])
    result = hook(ctx)
    assert len(result["messages"]) == 2
    assert "[bg]" in result["messages"][0]["content"][0]["text"]
    assert "[env]" in result["messages"][1]["content"][0]["text"]
    assert state.pending_injections == []
