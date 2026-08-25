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

from senza_agent.behavior.graph import ExecutionGraph
from senza_agent.tools import graph_tools


def _simple_node(node_id="n1", title="Step 1", goal="Do thing"):
    return {"id": node_id, "title": title, "goal": goal}


def test_transform_context_no_graph_no_injection():
    """No active graph → no graph-related injection."""
    graph_tools.set_graph(None)
    state = AgentState()
    hook = behavior_transform_context(state)
    ctx = _make_ctx([])
    result = hook(ctx)
    assert result["messages"] == []


def test_transform_context_graph_stall_injects_hint():
    """When stall_level >= 1, inject stall_hint and set advisor_requested."""
    graph_tools.set_graph(None)  # clean slate
    g = ExecutionGraph("test", [_simple_node()], [])
    # Force stall: enter and exit node many times to bump revisits
    for i in range(5):
        g.graph_op("enter", node="n1")
        g.graph_op("exit", node="n1", summary=f"attempt {i+1}")
    graph_tools.set_graph(g)

    state = AgentState()
    hook = behavior_transform_context(state)
    ctx = _make_ctx([])
    result = hook(ctx)

    # Should have injected at least one graph-related message
    assert len(result["messages"]) >= 1
    graph_msg = result["messages"][0]["content"][0]["text"]
    assert "graph" in graph_msg.lower() or "stall" in graph_msg.lower() or "revisit" in graph_msg.lower()
    # Advisor should be requested
    assert state.advisor_requested is True

    graph_tools.set_graph(None)  # cleanup

from senza_agent.tools.async_manager import AsyncJobManager


def test_transform_context_injects_completed_bg_jobs():
    """Completed background jobs are injected as user messages."""
    mgr = AsyncJobManager()
    job_id = mgr.start_shell("echo done_bg")
    for _ in range(50):
        info = mgr.peek(job_id, wait_secs=0.1)
        if info["status"] != "running":
            break

    # Patch the module-level get_manager to return our test manager
    import senza_agent.tools.async_manager as am_mod
    original_get = am_mod.get_manager
    am_mod.get_manager = lambda: mgr
    try:
        state = AgentState()
        hook = behavior_transform_context(state)
        ctx = _make_ctx([])
        ctx["turn_index"] = 1
        result = hook(ctx)
        assert len(result["messages"]) >= 1
        bg_msg = result["messages"][0]["content"][0]["text"]
        assert "done_bg" in bg_msg or job_id in bg_msg
    finally:
        am_mod.get_manager = original_get


def test_transform_context_no_bg_injection_when_no_jobs():
    """No completed jobs → no injection."""
    import senza_agent.tools.async_manager as am_mod
    mgr = AsyncJobManager()
    original_get = am_mod.get_manager
    am_mod.get_manager = lambda: mgr
    try:
        state = AgentState()
        hook = behavior_transform_context(state)
        ctx = _make_ctx([])
        ctx["turn_index"] = 1
        result = hook(ctx)
        assert result["messages"] == []
    finally:
        am_mod.get_manager = original_get

from senza_agent.behavior.acceptance_gate import review_completion_report


def test_transform_context_injects_remediation_feedback():
    """When completion_report fails review, inject feedback and set needs_remediation."""
    state = AgentState()
    state.completion_report = {
        "outcome": "done",
        "evidence_type": "artifact",
        "evidence": ["/nonexistent/file.py"],
    }
    hook = behavior_transform_context(state)
    ctx = _make_ctx([])
    result = hook(ctx)

    # Should have injected a remediation message
    assert len(result["messages"]) >= 1
    feedback = result["messages"][0]["content"][0]["text"]
    assert "needs_more_work" in feedback or "missing" in feedback.lower() or "artifact" in feedback.lower()
    # Should set needs_remediation flag
    assert state.needs_remediation is True
    # Should clear completion_report so it's not re-evaluated
    assert state.completion_report is None


def test_transform_context_no_remediation_when_no_report():
    """No completion_report → no remediation injection."""
    state = AgentState()
    hook = behavior_transform_context(state)
    ctx = _make_ctx([])
    result = hook(ctx)
    assert result["messages"] == []
    assert state.needs_remediation is False


def test_transform_context_no_remediation_when_report_passes():
    """Passing completion_report → no remediation injection, report left intact."""
    state = AgentState()
    state.completion_report = {
        "goal_understanding": "do the thing",
        "completed_work": ["wrote file.py"],
        "outcome": "done",
        "confidence": "high",
        "evidence_type": "none",
        "evidence": [],
    }
    hook = behavior_transform_context(state)
    ctx = _make_ctx([])
    result = hook(ctx)
    assert result["messages"] == []
    assert state.needs_remediation is False

from senza_agent.behavior.wrapup import behavior_should_stop


def test_should_stop_continues_when_remediation_pending():
    """needs_remediation=True → should_stop returns False (continue loop)."""
    state = AgentState()
    state.needs_remediation = True
    hook = behavior_should_stop(state)
    assert hook({}) is False
    # Flag is consumed after one check
    assert state.needs_remediation is False


def test_should_stop_stops_when_wrapup_exhausted():
    """wrapup_turns_left <= 0 → should_stop returns True."""
    state = AgentState()
    state.wrapup_turns_left = 0
    hook = behavior_should_stop(state)
    assert hook({}) is True


def test_should_stop_default_does_not_stop():
    """No remediation, no wrapup → should_stop returns False."""
    state = AgentState()
    hook = behavior_should_stop(state)
    assert hook({}) is False


def test_should_stop_remediation_takes_priority_over_wrapup():
    """needs_remediation=True overrides wrapup stop."""
    state = AgentState()
    state.needs_remediation = True
    state.wrapup_turns_left = 0
    hook = behavior_should_stop(state)
    # Remediation wins: continue so the model can fix things
    assert hook({}) is False
    assert state.needs_remediation is False

from senza_agent.config import BehaviorConfig
from senza_agent.behavior.bundle import BehaviorBundle


def test_bundle_registers_transform_context_hook():
    """BehaviorBundle.hooks must include a transform_context hook."""
    state = AgentState()
    config = BehaviorConfig()
    bundle = BehaviorBundle(state, config)
    # hooks is a list of senza.hooks.Hook — we can't inspect the kind
    # directly, but we can check that the list is non-empty and includes
    # more than just after_turn + prepare_next_turn (which was 2 before)
    assert len(bundle.hooks) >= 3


def test_bundle_uses_behavior_should_stop():
    """BehaviorBundle.should_stop must be set (to the combined hook)."""
    state = AgentState()
    config = BehaviorConfig()
    bundle = BehaviorBundle(state, config)
    assert bundle.should_stop is not None
