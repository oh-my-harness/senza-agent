"""Senza tool wrappers for the execution graph.

Exposes ``plan_create`` / ``plan_revise`` / ``plan_abandon`` as Senza tools.
A module-level singleton holds the current :class:`ExecutionGraph`; the agent
loop reads it via :func:`get_graph` for context injection.
"""
from __future__ import annotations

from typing import Any, Optional

from senza_agent.behavior.graph import ExecutionGraph


# ── Module-level graph instance ─────────────────────────────────────────────

_current_graph: Optional[ExecutionGraph] = None


def get_graph() -> Optional[ExecutionGraph]:
    """Return the current execution graph, or None if no graph is active."""
    return _current_graph


def set_graph(graph: Optional[ExecutionGraph]) -> None:
    """Set (or clear with None) the current execution graph."""
    global _current_graph
    _current_graph = graph


# ── Tool callbacks ──────────────────────────────────────────────────────────


def tool_plan_create(
    title: str,
    nodes: list[dict],
    edges: Optional[list[dict]] = None,
    reason: str = "",
    from_skill: Optional[str] = None,
    time_budget_min: Optional[float] = None,
) -> dict:
    """Create a new execution graph.

    Replaces any currently-active graph (the previous one is marked abandoned).
    """
    global _current_graph
    if _current_graph is not None and _current_graph.status == "active":
        _current_graph.abandon(reason="replaced by new graph")

    run_dir = _current_graph.run_dir if _current_graph is not None else ""
    graph = ExecutionGraph(
        title=title,
        nodes=nodes,
        edges=edges,
        reason=reason,
        from_skill=from_skill,
        time_budget_min=time_budget_min,
        run_dir=run_dir,
    )
    _current_graph = graph

    total = max(0, len(graph.nodes) - 1)
    msg = f"created graph {graph.gid}「{graph.title}」 with {total} nodes"
    if graph.time_budget:
        msg += f" (budget {graph.time_budget:.0f}s)"
    return {"status": "ok", "message": msg, "graph": graph.summary()}


def tool_plan_revise(ops: list[dict], reason: str = "") -> dict:
    """Apply a batch of structural operations to the current graph."""
    graph = _current_graph
    if graph is None:
        return {"status": "error", "message": "no active graph to revise"}
    if graph.status != "active":
        return {"status": "error", "message": f"graph is {graph.status}, not active"}

    result = graph.apply_revision(ops)
    return {
        "status": "ok" if result["fail"] == 0 else "partial",
        "message": f"revised: {result['ok']} ok, {result['fail']} failed"
                   + (f" — {reason}" if reason else ""),
        "ok": result["ok"],
        "fail": result["fail"],
        "details": result["details"],
        "graph": graph.summary(),
    }


def tool_plan_abandon(reason: str = "") -> dict:
    """Abandon the current execution graph."""
    global _current_graph
    graph = _current_graph
    if graph is None:
        return {"status": "error", "message": "no active graph to abandon"}

    result = graph.abandon(reason=reason)
    return {"status": "ok", "message": result["message"], "graph": graph.summary()}


# ── Tool factory ────────────────────────────────────────────────────────────


def get_graph_tools() -> list:
    """Return the list of Senza tool definitions for graph management."""
    import senza

    plan_create_params = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short title for the plan.",
            },
            "nodes": {
                "type": "array",
                "description": (
                    "Planned steps. Each node: {id?, title, goal, exit: "
                    "{evidence_type, expect: [paths]}, budget}. "
                    "evidence_type: artifact|tool_result|observation|none."
                ),
                "items": {"type": "object"},
            },
            "edges": {
                "type": "array",
                "description": (
                    "Optional edges: [{from, to, kind}]. kind: then|alt|fallback. "
                    "Omit to chain nodes in given order."
                ),
                "items": {"type": "object"},
            },
            "reason": {
                "type": "string",
                "description": "Why this plan is needed.",
            },
            "from_skill": {
                "type": "string",
                "description": "Skill that suggested this plan, if any.",
            },
            "time_budget_min": {
                "type": "number",
                "description": "Self-requested time budget in minutes.",
            },
        },
        "required": ["title", "nodes"],
    }

    plan_revise_params = {
        "type": "object",
        "properties": {
            "ops": {
                "type": "array",
                "description": (
                    "Batch of structural operations. Each op: {op, ...}. "
                    "ops: enter|exit|extend|fork|abandon|block|complete|update."
                ),
                "items": {"type": "object"},
            },
            "reason": {
                "type": "string",
                "description": "Why the revision is needed.",
            },
        },
        "required": ["ops"],
    }

    plan_abandon_params = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Why the plan is being abandoned.",
            },
        },
        "required": [],
    }

    return [
        senza.create_tool(
            name="plan_create",
            description=(
                "Create an execution graph to plan a multi-step task. "
                "Simple tasks do not need a graph. Only one graph is active "
                "at a time; creating a new one abandons the previous."
            ),
            parameters=plan_create_params,
            callback=lambda **kw: tool_plan_create(**kw),
        ),
        senza.create_tool(
            name="plan_revise",
            description=(
                "Apply a batch of structural operations to the current graph "
                "(enter/exit/extend/fork/abandon/block/complete/update)."
            ),
            parameters=plan_revise_params,
            callback=lambda **kw: tool_plan_revise(**kw),
        ),
        senza.create_tool(
            name="plan_abandon",
            description="Abandon the current execution graph entirely.",
            parameters=plan_abandon_params,
            callback=lambda **kw: tool_plan_abandon(**kw),
        ),
    ]
