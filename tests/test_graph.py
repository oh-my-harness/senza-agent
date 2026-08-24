"""Tests for the execution graph state machine."""
from __future__ import annotations

from senza_agent.behavior.graph import ExecutionGraph


# ── helpers ─────────────────────────────────────────────────────────────────

def _simple_node(
    nid: str = "n1",
    title: str = "Step 1",
    goal: str = "do X",
    evidence_type: str = "none",
    expect: list = None,
    budget: int = 5,
) -> dict:
    return {
        "id": nid,
        "title": title,
        "goal": goal,
        "exit": {"evidence_type": evidence_type, "expect": expect or []},
        "budget": budget,
    }


# ── creation ────────────────────────────────────────────────────────────────


def test_create_graph():
    g = ExecutionGraph("test", [_simple_node()], [])
    assert g.current_node() is None  # not entered yet
    assert g.status == "active"
    assert "n1" in g.nodes


def test_create_graph_auto_chains_nodes():
    nodes = [_simple_node("n1"), _simple_node("n2", title="Step 2")]
    g = ExecutionGraph("test", nodes, [])
    # Without explicit edges, nodes chain in order: n0 → n1 → n2
    assert any(e.from_node == "n1" and e.to_node == "n2" for e in g.edges)


def test_create_graph_with_explicit_edges():
    nodes = [_simple_node("n1"), _simple_node("n2")]
    edges = [{"from": "n1", "to": "n2", "kind": "then"}]
    g = ExecutionGraph("test", nodes, edges)
    assert any(e.from_node == "n1" and e.to_node == "n2" and e.kind == "then" for e in g.edges)


def test_create_graph_assigns_ids_for_missing():
    node = {"title": "no id", "goal": "g"}
    g = ExecutionGraph("test", [node], [])
    assert len(g.nodes) == 2  # root + one


# ── enter ───────────────────────────────────────────────────────────────────


def test_enter_node():
    g = ExecutionGraph("test", [_simple_node()], [])
    result = g.graph_op("enter", node="n1")
    assert result["ok"] is True
    assert g.current_node().id == "n1"
    assert g.current_node().status == "active"
    assert g.current_node().visits == 1


def test_enter_abandoned_node_fails():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.graph_op("abandon", node="n1", reason="wrong")
    result = g.graph_op("enter", node="n1")
    assert result["ok"] is False


def test_enter_when_another_active_fails():
    nodes = [_simple_node("n1"), _simple_node("n2", title="Step 2")]
    g = ExecutionGraph("test", nodes, [])
    g.graph_op("enter", node="n1")
    result = g.graph_op("enter", node="n2")
    assert result["ok"] is False


def test_enter_missing_node_fails():
    g = ExecutionGraph("test", [_simple_node()], [])
    result = g.graph_op("enter", node="n999")
    assert result["ok"] is False


def test_enter_grants_budget_once():
    g = ExecutionGraph("test", [_simple_node(budget=5)], [])
    r1 = g.graph_op("enter", node="n1")
    assert r1["granted"] == 5
    # Re-enter after exit would not re-grant
    g.graph_op("exit", node="n1", summary="done")
    # Can't re-enter a completed node, but the granted flag is sticky
    assert g.nodes["n1"].granted is True


# ── exit ────────────────────────────────────────────────────────────────────


def test_exit_node_no_evidence():
    g = ExecutionGraph("test", [_simple_node(evidence_type="none")], [])
    g.graph_op("enter", node="n1")
    result = g.graph_op("exit", node="n1", summary="done")
    assert result["ok"] is True
    assert g.current_node() is None
    assert g.nodes["n1"].status == "completed"
    assert g.nodes["n1"].closed_by == "self_certified"
    assert g.nodes["n1"].summary == "done"


def test_exit_not_active_fails():
    g = ExecutionGraph("test", [_simple_node()], [])
    result = g.graph_op("exit", node="n1", summary="done")
    assert result["ok"] is False


def test_exit_node_artifact_check_fails(tmp_path):
    missing = str(tmp_path / "nonexistent.py")
    g = ExecutionGraph(
        "test",
        [_simple_node(evidence_type="artifact", expect=[missing])],
        [],
    )
    g.graph_op("enter", node="n1")
    result = g.graph_op("exit", node="n1", summary="done")
    assert result["ok"] is False
    assert g.nodes["n1"].status == "active"
    assert "missing" in result


def test_exit_node_artifact_check_passes(tmp_path):
    f = tmp_path / "output.py"
    f.write_text("print('hello')")
    g = ExecutionGraph(
        "test",
        [_simple_node(evidence_type="artifact", expect=[str(f)])],
        [],
    )
    g.graph_op("enter", node="n1")
    result = g.graph_op("exit", node="n1", summary="done")
    assert result["ok"] is True
    assert g.nodes["n1"].closed_by == "evidence_verified"


def test_exit_artifact_force_without_detail_fails(tmp_path):
    missing = str(tmp_path / "nonexistent.py")
    g = ExecutionGraph(
        "test",
        [_simple_node(evidence_type="artifact", expect=[missing])],
        [],
    )
    g.graph_op("enter", node="n1")
    result = g.graph_op("exit", node="n1", summary="done", force=True,
                        residue="无", impact="无")
    assert result["ok"] is False


def test_exit_artifact_force_with_detail_passes(tmp_path):
    missing = str(tmp_path / "nonexistent.py")
    g = ExecutionGraph(
        "test",
        [_simple_node(evidence_type="artifact", expect=[missing])],
        [],
    )
    g.graph_op("enter", node="n1")
    result = g.graph_op(
        "exit", node="n1", summary="done", force=True,
        residue="the file was not created because the API returned 404",
        impact="downstream n2 comparison analysis will lack the baseline data",
    )
    assert result["ok"] is True
    assert result["forced"] is True
    assert g.nodes["n1"].closed_by == "unverified_override"
    assert g.nodes["n1"].residue != ""


def test_exit_merges_side_effects(tmp_path):
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done",
              side_effects=["created foo.py", "modified bar.py"])
    assert "created foo.py" in g.nodes["n1"].side_effects
    assert "modified bar.py" in g.nodes["n1"].side_effects


# ── extend / fork ───────────────────────────────────────────────────────────


def test_extend_graph():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done")
    result = g.graph_op("extend", after="n1", node={
        "id": "n2", "title": "Step 2", "goal": "do Y",
        "exit": {"evidence_type": "none"}, "budget": 3,
    })
    assert result["ok"] is True
    assert "n2" in g.nodes
    assert any(e.from_node == "n1" and e.to_node == "n2" for e in g.edges)


def test_extend_uses_cursor_when_no_after():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    result = g.graph_op("extend", node={
        "title": "Step 2", "goal": "do Y",
        "exit": {"evidence_type": "none"}, "budget": 3,
    })
    assert result["ok"] is True
    new_id = result["node_id"]
    assert any(e.from_node == "n1" and e.to_node == new_id for e in g.edges)


def test_extend_rejects_list():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    result = g.graph_op("extend", after="n1", node=[{"title": "a"}, {"title": "b"}])
    assert result["ok"] is False


def test_fork_creates_alt_edge():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    result = g.graph_op("fork", from_node="n1", node={
        "title": "Alt approach", "goal": "try other way",
        "exit": {"evidence_type": "none"}, "budget": 3,
    })
    assert result["ok"] is True
    new_id = result["node_id"]
    assert any(e.from_node == "n1" and e.to_node == new_id and e.kind == "alt"
               for e in g.edges)


# ── abandon / block ─────────────────────────────────────────────────────────


def test_abandon_node():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    result = g.graph_op("abandon", node="n1", reason="wrong approach")
    assert result["ok"] is True
    assert g.nodes["n1"].status == "abandoned"
    assert g.nodes["n1"].abandon_reason == "wrong approach"


def test_abandon_cascades_then_descendants():
    nodes = [_simple_node("n1"), _simple_node("n2", title="Step 2")]
    g = ExecutionGraph("test", nodes, [])
    g.graph_op("enter", node="n1")
    result = g.graph_op("abandon", node="n1", reason="wrong")
    assert result["ok"] is True
    assert "n2" in result.get("cascaded", [])
    assert g.nodes["n2"].status == "abandoned"


def test_abandon_root_fails():
    g = ExecutionGraph("test", [_simple_node()], [])
    result = g.graph_op("abandon", node="n0")
    assert result["ok"] is False


def test_block_node():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    result = g.graph_op("block", node="n1", reason="waiting on external API")
    assert result["ok"] is True
    assert g.nodes["n1"].status == "blocked"
    assert g.nodes["n1"].summary == "waiting on external API"


# ── complete ────────────────────────────────────────────────────────────────


def test_complete_with_open_nodes_fails():
    nodes = [_simple_node("n1"), _simple_node("n2", title="Step 2")]
    g = ExecutionGraph("test", nodes, [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done")
    # n2 still pending
    result = g.graph_op("complete")
    assert result["ok"] is False
    assert g.status == "active"


def test_complete_succeeds_when_all_terminal():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done")
    result = g.graph_op("complete", reason="all done")
    assert result["ok"] is True
    assert g.status == "completed"


# ── render_context ──────────────────────────────────────────────────────────


def test_render_context():
    g = ExecutionGraph("test", [_simple_node()], [])
    ctx = g.render_context()
    assert isinstance(ctx, str)
    assert len(ctx) > 0
    assert "n1" in ctx


def test_render_context_shows_current_node():
    g = ExecutionGraph("test", [_simple_node(goal="accomplish X")], [])
    g.graph_op("enter", node="n1")
    ctx = g.render_context()
    assert "accomplish X" in ctx
    assert "▶" in ctx


def test_render_context_completed_graph():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done")
    g.graph_op("complete")
    ctx = g.render_context()
    assert "completed" in ctx


# ── serialization ───────────────────────────────────────────────────────────


def test_to_dict_from_dict_roundtrip():
    g = ExecutionGraph(
        "test",
        [_simple_node("n1"), _simple_node("n2", title="Step 2")],
        [{"from": "n1", "to": "n2", "kind": "then"}],
        reason="testing",
    )
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="did it")

    data = g.to_dict()
    g2 = ExecutionGraph.from_dict(data)

    assert g2.gid == g.gid
    assert g2.title == g.title
    assert set(g2.nodes.keys()) == set(g.nodes.keys())
    assert g2.nodes["n1"].status == "completed"
    assert g2.nodes["n1"].summary == "did it"
    assert len(g2.edges) == len(g.edges)
    assert g2.cursor == g.cursor


def test_summary():
    g = ExecutionGraph("test", [_simple_node()], [])
    s = g.summary()
    assert s["gid"] == g.gid
    assert s["total"] == 1
    assert s["done"] == 0
    assert s["open"] == 1  # n1 is pending
    assert s["active_node"] == ""


# ── plan_revise ─────────────────────────────────────────────────────────────


def test_apply_revision_batch():
    g = ExecutionGraph("test", [_simple_node()], [])
    ops = [
        {"op": "enter", "node": "n1"},
        {"op": "exit", "node": "n1", "summary": "done"},
    ]
    result = g.apply_revision(ops)
    assert result["ok"] == 2
    assert result["fail"] == 0
    assert g.nodes["n1"].status == "completed"


def test_apply_revision_with_update():
    g = ExecutionGraph("test", [_simple_node()], [])
    ops = [
        {"op": "update", "node": "n1", "goal": "revised goal", "budget": 10},
    ]
    result = g.apply_revision(ops)
    assert result["ok"] == 1
    assert g.nodes["n1"].goal == "revised goal"
    assert g.nodes["n1"].budget == 10


def test_apply_revision_update_terminal_fails():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done")
    ops = [{"op": "update", "node": "n1", "goal": "new"}]
    result = g.apply_revision(ops)
    assert result["fail"] == 1


# ── graph abandon ───────────────────────────────────────────────────────────


def test_abandon_whole_graph():
    g = ExecutionGraph("test", [_simple_node()], [])
    result = g.abandon(reason="not needed")
    assert result["ok"] is True
    assert g.status == "abandoned"


def test_op_on_non_active_graph_fails():
    g = ExecutionGraph("test", [_simple_node()], [])
    g.abandon(reason="done")
    result = g.graph_op("enter", node="n1")
    assert result["ok"] is False


# ── unknown op ──────────────────────────────────────────────────────────────


def test_unknown_op_fails():
    g = ExecutionGraph("test", [_simple_node()], [])
    result = g.graph_op("bogus", node="n1")
    assert result["ok"] is False


# ── time budget & stall detection ──────────────────────────────────────────

import time as _time


def test_time_used_zero_before_start():
    """time_used() returns 0.0 when time_start was never set."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.time_start = None  # simulate never started
    assert g.time_used() == 0.0


def test_time_used_positive_after_start():
    """time_used() returns positive value after graph is created."""
    g = ExecutionGraph("test", [_simple_node()], [])
    _time.sleep(0.05)
    used = g.time_used()
    assert used > 0.0


def test_time_used_frozen_after_close():
    """time_used() freezes after graph close (time_end set)."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.time_end = g.time_start + 10.0
    used = g.time_used()
    _time.sleep(0.05)
    assert g.time_used() == used  # frozen


def test_time_left_none_without_budget():
    """time_left() returns None when no budget is set."""
    g = ExecutionGraph("test", [_simple_node()], [])
    assert g.time_left() is None


def test_time_left_positive_with_budget():
    """time_left() returns positive value when budget > used."""
    g = ExecutionGraph("test", [_simple_node()], [], time_budget_min=10.0)
    left = g.time_left()
    assert left is not None
    assert left > 0.0


def test_time_left_negative_when_exceeded():
    """time_left() returns negative value when budget exhausted."""
    g = ExecutionGraph("test", [_simple_node()], [], time_budget_min=0.001)
    _time.sleep(0.1)
    left = g.time_left()
    assert left is not None
    assert left < 0.0


def test_expire_if_out_of_time_no_budget():
    """expire_if_out_of_time() returns None when no budget set."""
    g = ExecutionGraph("test", [_simple_node()], [])
    assert g.expire_if_out_of_time() is None
    assert g.status == "active"


def test_expire_if_out_of_time_budget_remaining():
    """expire_if_out_of_time() returns None when budget remaining."""
    g = ExecutionGraph("test", [_simple_node()], [], time_budget_min=10.0)
    assert g.expire_if_out_of_time() is None
    assert g.status == "active"


def test_expire_if_out_of_time_expires():
    """expire_if_out_of_time() expires graph when budget exhausted."""
    g = ExecutionGraph("test", [_simple_node()], [], time_budget_min=0.001)
    _time.sleep(0.1)
    result = g.expire_if_out_of_time()
    assert result is not None
    assert g.status == "expired"
    assert g.time_end is not None
    assert "expired" in g.closed_reason


def test_expire_if_out_of_time_not_active():
    """expire_if_out_of_time() returns None on already-closed graph."""
    g = ExecutionGraph("test", [_simple_node()], [], time_budget_min=0.001)
    g.status = "completed"
    assert g.expire_if_out_of_time() is None
    assert g.status == "completed"


def test_metrics_empty_when_no_nodes():
    """metrics() returns empty dict when no non-root nodes."""
    g = ExecutionGraph("test", [], [])
    assert g.metrics() == {}


def test_metrics_returns_dict_with_stall_iters():
    """metrics() returns dict with stall_iters when populated."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g._iter = 5
    m = g.metrics()
    assert "stall_iters" in m
    assert m["stall_iters"] == 5  # no closed nodes → stall from created_iter=0
    assert m["gid"] == g.gid
    assert m["done_count"] == 0
    assert m["open_count"] == 1  # n1 is pending
    assert m["active_node"] == ""


def test_metrics_after_node_closure():
    """metrics() reflects closed node correctly."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g._iter = 3
    g.graph_op("exit", node="n1", summary="done")
    g._iter = 8
    m = g.metrics()
    assert m["done_count"] == 1
    assert m["stall_iters"] == 5  # 8 - 3 (last progress at iter 3)
    assert m["active_node"] == ""


def test_stall_level_healthy():
    """stall_level() returns (0, '') when healthy."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g._iter = 1
    level, reason = g.stall_level()
    assert level == 0
    assert reason == ""


def test_stall_level_l1_stall():
    """stall_level() returns (1, 'stall') when stall_iters >= L1 threshold."""
    import os
    os.environ["GRAPH_STALL_L1"] = "5"
    try:
        g = ExecutionGraph("test", [_simple_node()], [])
        g._iter = 10
        level, reason = g.stall_level()
        assert level == 1
        assert reason == "stall"
    finally:
        del os.environ["GRAPH_STALL_L1"]


def test_stall_level_l2_stall():
    """stall_level() returns (2, 'stall') when stall_iters >= L2 threshold."""
    import os
    os.environ["GRAPH_STALL_L2"] = "10"
    try:
        g = ExecutionGraph("test", [_simple_node()], [])
        g._iter = 15
        level, reason = g.stall_level()
        assert level == 2
        assert reason == "stall"
    finally:
        del os.environ["GRAPH_STALL_L2"]


def test_stall_level_l1_revisit():
    """stall_level() returns (1, 'revisit') when visits >= L1 revisit threshold."""
    import os
    os.environ["GRAPH_REVISIT_L1"] = "2"
    try:
        g = ExecutionGraph("test", [_simple_node()], [])
        g.graph_op("enter", node="n1")
        g.graph_op("exit", node="n1", summary="done")
        g.graph_op("enter", node="n1")  # re-enter
        g.graph_op("exit", node="n1", summary="done again")
        g.nodes["n1"].visits = 3
        level, reason = g.stall_level()
        assert level == 1
        assert reason == "revisit"
    finally:
        del os.environ["GRAPH_REVISIT_L1"]


def test_stall_hint_returns_text():
    """stall_hint() returns non-empty string when stalled."""
    import os
    os.environ["GRAPH_STALL_L1"] = "5"
    try:
        g = ExecutionGraph("test", [_simple_node()], [])
        g._iter = 10
        hint = g.stall_hint()
        assert isinstance(hint, str)
        assert len(hint) > 0
        assert "10" in hint
    finally:
        del os.environ["GRAPH_STALL_L1"]


def test_pace_empty_when_no_closed():
    """pace() returns empty dict when no nodes have time spans."""
    g = ExecutionGraph("test", [_simple_node()], [])
    assert g.pace() == {}


def test_pace_returns_data_after_closure():
    """pace() returns dict with per_node after a node is closed with time."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    _time.sleep(0.15)
    g.graph_op("exit", node="n1", summary="done")
    p = g.pace()
    assert p != {}
    assert p["nodes"] == 1
    assert p["per_node"] > 0.0


def test_node_seconds_none_for_unentered():
    """node_seconds() returns None for a node that was never entered."""
    g = ExecutionGraph("test", [_simple_node()], [])
    assert g.node_seconds(g.nodes["n1"]) is None


def test_node_seconds_returns_duration():
    """node_seconds() returns positive duration after enter+exit."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    _time.sleep(0.15)
    g.graph_op("exit", node="n1", summary="done")
    secs = g.node_seconds(g.nodes["n1"])
    assert secs is not None
    assert secs > 0.0


def test_pending_isolate_none_by_default():
    """pending_isolate() returns None when no node has isolate=True."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    assert g.pending_isolate() is None


def test_pending_isolate_returns_node():
    """pending_isolate() returns the active node when isolate=True and seg is None."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.nodes["n1"].isolate = True
    result = g.pending_isolate()
    assert result is not None
    assert result.id == "n1"


def test_pending_isolate_none_after_sealed():
    """pending_isolate() returns None after mark_sealed sets seg."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.nodes["n1"].isolate = True
    g.mark_sealed(g.nodes["n1"], "seg-1")
    assert g.pending_isolate() is None
    assert g.nodes["n1"].seg == "seg-1"


def test_gap_lines_empty_when_all_closed():
    """gap_lines() returns empty list when all nodes are closed."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done")
    assert g.gap_lines() == []


def test_gap_lines_returns_lines_with_open_nodes():
    """gap_lines() returns lines when open nodes exist."""
    g = ExecutionGraph("test", [_simple_node(goal="implement feature X")], [])
    lines = g.gap_lines()
    assert len(lines) == 1
    assert "n1" in lines[0]
    assert "status:" in lines[0]
    assert "goal:" in lines[0]


def test_gap_lines_includes_override_nodes():
    """gap_lines() includes override (degraded-closed) nodes."""
    g = ExecutionGraph("test", [_simple_node(evidence_type="artifact", expect=["nonexistent.py"])], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done", force=True,
              residue="could not create artifact but work is done",
              impact="minor delay expected")
    lines = g.gap_lines()
    assert len(lines) >= 1
    override_lines = [l for l in lines if "override" in l]
    assert len(override_lines) == 1
    assert "n1" in override_lines[0]
    assert "residue:" in override_lines[0]


def test_gap_lines_likely_done():
    """gap_lines() reports likely_done when artifacts exist for open nodes."""
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
        f.write(b"print('hello')")
        artifact_path = f.name
    try:
        g = ExecutionGraph("test", [_simple_node(evidence_type="artifact", expect=[artifact_path])], [])
        lines = g.gap_lines()
        # The node is still open (pending), but artifacts exist → likely_done line
        assert len(lines) == 1
        assert "artifacts present" in lines[0]
    finally:
        os.unlink(artifact_path)


def test_open_nodes_returns_open():
    """open_nodes() returns pending/active/blocked nodes."""
    g = ExecutionGraph("test", [_simple_node()], [])
    open_list = g.open_nodes()
    assert len(open_list) == 1
    assert open_list[0]["node"] == "n1"
    assert open_list[0]["status"] == "pending"
    assert open_list[0]["likely_done"] is False


def test_open_nodes_empty_when_all_closed():
    """open_nodes() returns empty list when all nodes are terminal."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done")
    assert g.open_nodes() == []


def test_override_nodes_empty_by_default():
    """override_nodes() returns empty list when no degraded closures."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done")
    assert g.override_nodes() == []


def test_override_nodes_returns_degraded():
    """override_nodes() returns nodes closed with unverified_override."""
    g = ExecutionGraph("test", [_simple_node(evidence_type="artifact", expect=["nonexistent.py"])], [])
    g.graph_op("enter", node="n1")
    g.graph_op("exit", node="n1", summary="done", force=True,
              residue="could not create artifact but work is done",
              impact="minor delay expected")
    overrides = g.override_nodes()
    assert len(overrides) == 1
    assert overrides[0]["node"] == "n1"
    assert overrides[0]["status"] == "done_unverified"
    assert overrides[0]["residue"] != ""


def test_graph_node_new_fields_default():
    """GraphNode has isolate=False, seg=None, iter_range=[None,None] by default."""
    from senza_agent.behavior.graph import GraphNode
    n = GraphNode(id="n1", title="t", goal="g", exit={}, budget=0)
    assert n.isolate is False
    assert n.seg is None
    assert n.iter_range == [None, None]


def test_to_dict_includes_new_fields():
    """to_dict() includes isolate, seg, iter_range."""
    g = ExecutionGraph("test", [_simple_node()], [])
    d = g.nodes["n1"].to_dict()
    assert "isolate" in d
    assert "seg" in d
    assert "iter_range" in d
    assert d["isolate"] is False
    assert d["seg"] is None
    assert d["iter_range"] == [None, None]


def test_from_dict_restores_new_fields():
    """from_dict() restores isolate, seg, iter_range."""
    g = ExecutionGraph("test", [_simple_node()], [])
    g.nodes["n1"].isolate = True
    g.nodes["n1"].seg = "seg-abc"
    g.nodes["n1"].iter_range = [3, 7]
    d = g.to_dict()
    g2 = ExecutionGraph.from_dict(d)
    assert g2.nodes["n1"].isolate is True
    assert g2.nodes["n1"].seg == "seg-abc"
    assert g2.nodes["n1"].iter_range == [3, 7]
