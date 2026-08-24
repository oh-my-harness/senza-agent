"""Execution Graph — a planning-aid state machine.

Ported from QevosAgent's ``agent/core/graph.py``. The graph is **not** the
execution engine — the Senza runtime's Rust ``agent_loop`` still drives tool
execution. The graph only makes the model's method/plan explicit and tracks
node-boundary progress with evidence-contract checks.

Design tenets preserved from the source:

* The graph is a *capability* the model opts into. Simple tasks never create one.
* Nodes are **logical** boundaries, not context boundaries — no segment sealing.
* Exit evidence-contract: ``artifact`` nodes may only close when their expected
  files exist. ``force=True`` allows a downgrade, but demands a non-perfunctory
  residue + impact statement (the debt must be written down before it gets
  compressed away).
* Per-node iteration budget is *granted on enter*, never on plan — drawing nodes
  must not mint budget. Exhausted budget auto-tops-up while open work remains.
* ``complete`` is an explicit declaration: reaching the end of planned nodes is
  *not* auto-completion (the model usually wants to ``extend`` next).

This module is self-contained: no ``AgentState``/``state.meta`` coupling, no
timing/i18n dependencies. Persistence is via :meth:`ExecutionGraph.to_dict` /
:meth:`ExecutionGraph.from_dict`.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# ── Constants ───────────────────────────────────────────────────────────────

ROOT_ID = "n0"

# Node statuses. ``pending`` ≈ QevosAgent's ``planned``; renamed to match the
# assignment's contract.
NODE_STATUS = ("pending", "active", "completed", "abandoned", "blocked")
OPEN_STATUS = ("pending", "active", "blocked")  # not terminal → counts as gap
GRAPH_STATUS = ("active", "completed", "abandoned", "expired")
EDGE_KINDS = ("then", "alt", "fallback")
EVIDENCE_TYPES = ("artifact", "tool_result", "observation", "none")

# Inline graph_op operations.
INLINE_OPS = ("enter", "exit", "extend", "fork", "abandon", "block", "complete")

_STATUS_MARK = {
    "completed": "✓",
    "abandoned": "✗",
    "blocked": "⏸",
    "active": "▶",
    "pending": "·",
}

# Perfunctory conclusions — bare verdicts with no backing. "No impact" is a
# legitimate conclusion but must carry a reason, so we block the bare form.
_PERFUNCTORY = frozenset({
    "无", "没有", "不影响", "有影响", "略", "同上", "有点问题", "还有问题", "小问题",
    "n/a", "na", "none", "no", "yes", "nil", "tbd", "unknown", "no impact", "minor",
})


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class GraphNode:
    """A single planned step in the execution graph."""

    id: str
    title: str
    goal: str
    exit: dict  # {evidence_type, expect: [paths]}
    budget: int  # iterations granted on enter
    status: str = "pending"
    parent: Optional[str] = None
    summary: str = ""
    side_effects: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    # bookkeeping
    visits: int = 0
    granted: bool = False
    residue: str = ""
    impact: str = ""
    closed_by: str = ""
    abandon_reason: str = ""
    time_range: list = field(default_factory=lambda: [None, None])
    # segment isolation: when True, the model declares the next segment
    # does not need this node's process detail (KV-cache boundary).
    isolate: bool = False
    seg: Any = None
    iter_range: list = field(default_factory=lambda: [None, None])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "exit": dict(self.exit),
            "budget": self.budget,
            "status": self.status,
            "parent": self.parent,
            "summary": self.summary,
            "side_effects": list(self.side_effects),
            "gaps": list(self.gaps),
            "visits": self.visits,
            "granted": self.granted,
            "residue": self.residue,
            "impact": self.impact,
            "closed_by": self.closed_by,
            "abandon_reason": self.abandon_reason,
            "time_range": list(self.time_range),
            "isolate": self.isolate,
            "seg": self.seg,
            "iter_range": list(self.iter_range),
        }


@dataclass
class GraphEdge:
    from_node: str
    to_node: str
    kind: str = "then"

    def to_dict(self) -> dict:
        return {"from": self.from_node, "to": self.to_node, "kind": self.kind}


# ── Helpers ─────────────────────────────────────────────────────────────────


def _clip(value: Any, limit: int) -> str:
    s = str(value or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


def _str_list(value: Any, limit: int = 20, item_chars: int = 300) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value[:limit]:
        s = _clip(item, item_chars)
        if s:
            out.append(s)
    return out


def _normalize_exit(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raw = {}
    expect = _str_list(raw.get("expect"), limit=12)
    evidence_type = str(raw.get("evidence_type") or "").strip().lower()
    if evidence_type not in EVIDENCE_TYPES:
        # Infer from expect: artifact paths → verifiable; otherwise self-cert.
        evidence_type = "artifact" if expect else "observation"
    return {"evidence_type": evidence_type, "expect": expect}


def _normalize_node(raw: Any, node_id: str, parent: Optional[str] = None) -> GraphNode:
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        raw = {}

    try:
        budget = int(raw.get("budget") or 0)
    except Exception:
        budget = 0
    budget = max(0, min(budget, 200))

    status = str(raw.get("status") or "pending").strip().lower()
    if status not in NODE_STATUS:
        status = "pending"

    title = _clip(raw.get("title"), 40) or _clip(raw.get("goal"), 40) or node_id

    return GraphNode(
        id=node_id,
        title=title,
        goal=_clip(raw.get("goal"), 600),
        status=status,
        parent=parent,
        exit=_normalize_exit(raw.get("exit")),
        budget=budget,
        summary="",
        side_effects=[],
        gaps=[],
    )


def _free_node_id(used: set[str]) -> str:
    i = 1
    while f"n{i}" in used:
        i += 1
    return f"n{i}"


def _is_perfunctory(text: str, min_len: int) -> bool:
    s = (text or "").strip().strip("。.！!，,、；;：: ")
    if len(s) < min_len:
        return True
    return s.lower() in _PERFUNCTORY


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except Exception:
        return default


def _path_exists(path: str, run_dir: str = "") -> bool:
    """Artifact existence check: try as-is / run_dir-relative / cwd-relative."""
    s = str(path or "").strip().strip("`\"'")
    if not s:
        return False
    if s.startswith("./"):
        s = s[2:]
    candidates = [s]
    if run_dir:
        candidates.append(os.path.join(run_dir, s))
    candidates.append(os.path.join(os.getcwd(), s))
    for candidate in candidates:
        try:
            if os.path.exists(candidate):
                return True
        except Exception:
            continue
    return False


# ── ExecutionGraph ──────────────────────────────────────────────────────────


class ExecutionGraph:
    """A planning-aid DAG state machine.

    Single source of truth: in-memory nodes + edges. Mirror to dict for
    persistence via :meth:`to_dict`.
    """

    def __init__(
        self,
        title: str,
        nodes: list[dict],
        edges: Optional[list[dict]] = None,
        reason: str = "",
        from_skill: Optional[str] = None,
        time_budget_min: Optional[float] = None,
        run_dir: str = "",
        gid: Optional[str] = None,
    ) -> None:
        self.gid = gid or "g1"
        self.title = _clip(title, 60) or self.gid
        self.created_reason = _clip(reason, 300)
        self.from_skill = _clip(from_skill, 60) or None
        self.run_dir = run_dir or ""
        self.status = "active"
        self.cursor = ROOT_ID
        self.created_iter = 0
        self.closed_iter: Optional[int] = None
        self.closed_reason = ""
        self.time_start = round(time.monotonic(), 2)
        self.time_end: Optional[float] = None
        self.time_budget: Optional[float] = None
        self.budget_granted = 0
        self._iter = 0  # internal iteration counter

        requested = None
        if time_budget_min is not None and str(time_budget_min).strip() != "":
            try:
                requested = max(0.0, float(time_budget_min) * 60.0)
            except Exception:
                requested = None
        self.time_budget = requested or None

        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []

        # Root node: history before the graph must have a landing point.
        root = GraphNode(
            id=ROOT_ID,
            title="前序工作",
            goal="",
            exit={"evidence_type": "none", "expect": []},
            budget=0,
            status="completed",
            parent=None,
            closed_by="implicit",
        )
        self.nodes[ROOT_ID] = root

        self._build(nodes, edges)

    # ── construction ───────────────────────────────────────────────────────

    def _build(self, raw_nodes: Any, raw_edges: Any) -> None:
        if isinstance(raw_nodes, dict):
            raw_nodes = [raw_nodes]
        if not isinstance(raw_nodes, (list, tuple)):
            raw_nodes = []

        # Assign ids: keep model-supplied id if valid & unique.
        assigned: list[tuple[str, Any]] = []
        used: set[str] = {ROOT_ID}
        for raw in raw_nodes:
            wanted = ""
            if isinstance(raw, dict):
                wanted = str(raw.get("id") or "").strip()
            node_id = wanted if (wanted and wanted not in used) else _free_node_id(used)
            used.add(node_id)
            assigned.append((node_id, raw))

        # Edges: use given; else chain in given order (most common usage).
        parent_of: dict[str, str] = {}
        explicit: list[tuple[str, str, str]] = []
        edges_given = isinstance(raw_edges, (list, tuple)) and bool(raw_edges)
        if edges_given:
            for e in raw_edges:
                if not isinstance(e, dict):
                    continue
                src = str(e.get("from") or "").strip()
                dst = str(e.get("to") or "").strip()
                if src not in used or dst not in used or src == dst:
                    continue
                kind = str(e.get("kind") or "then").strip().lower()
                explicit.append((src, dst, kind))
                parent_of.setdefault(dst, src)
        else:
            prev = ROOT_ID
            for node_id, _raw in assigned:
                explicit.append((prev, node_id, "then"))
                parent_of.setdefault(node_id, prev)
                prev = node_id

        # Orphans (no incoming edge): chain after previous, not all to root.
        prev_id = ROOT_ID
        for node_id, _raw in assigned:
            if node_id not in parent_of:
                parent_of[node_id] = prev_id
                explicit.append((prev_id, node_id, "then"))
            prev_id = node_id

        for node_id, raw in assigned:
            self.nodes[node_id] = _normalize_node(raw, node_id, parent=parent_of[node_id])

        for src, dst, kind in explicit:
            self._add_edge(src, dst, kind)

    def _add_edge(self, src: str, dst: str, kind: str) -> None:
        if kind not in EDGE_KINDS:
            kind = "then"
        for e in self.edges:
            if e.from_node == src and e.to_node == dst:
                return
        self.edges.append(GraphEdge(from_node=src, to_node=dst, kind=kind))

    def _next_node_id(self) -> str:
        return _free_node_id(set(self.nodes.keys()))

    # ── queries ────────────────────────────────────────────────────────────

    def current_node(self) -> Optional[GraphNode]:
        """Return the active node, or None if no node is active."""
        for node in self.nodes.values():
            if node.status == "active":
                return node
        return None

    def has_open_work(self) -> bool:
        return any(n.status in OPEN_STATUS for n in self.nodes.values())

    def _children(self, node_id: str) -> list[GraphNode]:
        return [n for n in self.nodes.values() if n.parent == node_id]

    def _ancestors(self, node_id: str) -> list[GraphNode]:
        """Parent chain from root to node_id (exclusive of self). Cycle-safe."""
        chain: list[GraphNode] = []
        seen: set[str] = set()
        cur = self.nodes.get(node_id)
        while cur is not None and cur.parent is not None:
            parent_id = cur.parent
            if parent_id in seen or parent_id not in self.nodes:
                break
            seen.add(parent_id)
            parent = self.nodes[parent_id]
            chain.append(parent)
            cur = parent
        chain.reverse()
        return chain

    def _descendants(self, node_id: str, *, only_then: bool = False) -> list[GraphNode]:
        """Descendants of node_id. ``only_then`` restricts to ``then`` edges."""
        out: list[GraphNode] = []
        seen: set[str] = set()

        def walk(src: str) -> None:
            for e in self.edges:
                if e.from_node != src:
                    continue
                if only_then and e.kind != "then":
                    continue
                tgt = e.to_node
                if tgt in seen or tgt not in self.nodes:
                    continue
                seen.add(tgt)
                out.append(self.nodes[tgt])
                walk(tgt)

        walk(node_id)
        return out

    def _routes_from(self, node_id: str) -> list[GraphNode]:
        """Alt/fallback targets from node_id that are still pending."""
        out: list[GraphNode] = []
        for e in self.edges:
            if e.from_node != node_id:
                continue
            if e.kind not in ("alt", "fallback"):
                continue
            target = self.nodes.get(e.to_node)
            if target is None or target.status != "pending":
                continue
            out.append(target)
        return out

    def _route_hint(self, node_id: str) -> str:
        routes = self._routes_from(node_id)
        if not routes:
            return ""
        return " 备选退路: " + "; ".join(
            f"{r.id}「{r.title}」" for r in routes[:3]
        )

    # ── iteration budget ───────────────────────────────────────────────────

    def _grant_budget(self, node: GraphNode) -> int:
        """Grant the node's self-declared budget on first enter.

        Granted on enter, once per node: drawing a node never mints budget —
        otherwise the model could draw nodes to fabricate iterations.
        """
        if node.granted:
            return 0
        node.granted = True
        amount = int(node.budget or 0)
        if amount <= 0:
            return 0
        self.budget_granted += amount
        return amount

    def topup_budget(self, amount: int = 10) -> int:
        """Top up iteration budget when exhausted but open work remains."""
        if not self.has_open_work():
            return 0
        amount = max(1, amount)
        self.budget_granted += amount
        return amount

    # ── graph_op ───────────────────────────────────────────────────────────

    def graph_op(self, op: str, **kwargs) -> dict:
        """Apply an inline graph operation.

        Returns ``{"ok": bool, "message": str, ...}``.
        Any internal error is caught — a graph bug must never break the agent loop.
        """
        try:
            return self._apply_op(op, **kwargs)
        except Exception as e:  # pragma: no cover - defensive
            return {"ok": False, "message": f"graph_op internal error: {type(e).__name__}: {e}"}

    def _apply_op(self, op: str, **kwargs) -> dict:
        op = str(op or "").strip().lower()
        if op not in INLINE_OPS:
            return {"ok": False, "message": f"unknown graph_op: {op or '(empty)'}"}
        if self.status != "active":
            return {"ok": False, "message": f"graph is {self.status}, not active"}

        if op == "enter":
            return self._op_enter(**kwargs)
        if op == "exit":
            return self._op_exit(**kwargs)
        if op in ("extend", "fork"):
            return self._op_add(op, **kwargs)
        if op == "abandon":
            return self._op_abandon(**kwargs)
        if op == "complete":
            return self._op_complete(**kwargs)
        return self._op_block(**kwargs)

    def _require_node(self, node_id: str) -> tuple[Optional[GraphNode], str]:
        node = self.nodes.get(node_id)
        if node is None:
            return None, f"node not found: {node_id or '(empty)'}"
        return node, ""

    def _op_enter(self, node: str = "", **_) -> dict:
        node_id = str(node or "").strip()
        n, err = self._require_node(node_id)
        if n is None:
            return {"ok": False, "message": err}
        if n.status == "abandoned":
            return {"ok": False, "message": f"node {node_id} is abandoned"}

        current = self.current_node()
        if current is not None and current.id != node_id:
            return {"ok": False, "message": f"another node is active: {current.id}"}

        n.status = "active"
        n.visits += 1
        if n.time_range[0] is None:
            n.time_range[0] = round(time.monotonic(), 2)
        if n.iter_range[0] is None:
            n.iter_range[0] = self._iter
        n.time_range[1] = None
        self.cursor = node_id
        granted = self._grant_budget(n)
        msg = f"entered {node_id}「{n.title}」"
        if granted:
            msg += f" (granted {granted} iterations)"
        return {"ok": True, "message": msg, "granted": granted}

    def _op_exit(
        self,
        node: str = "",
        summary: str = "",
        side_effects: Any = None,
        gaps: Any = None,
        residue: str = "",
        impact: str = "",
        force: bool = False,
        **_,
    ) -> dict:
        node_id = str(node or "").strip() or self.cursor
        n, err = self._require_node(node_id)
        if n is None:
            return {"ok": False, "message": err}
        if n.status != "active":
            return {"ok": False, "message": f"node {node_id} is {n.status}, not active"}

        # Residue ledger is recorded regardless of exit outcome.
        self._merge_side_effects(n, side_effects)

        exit_spec = n.exit or {}
        evidence_type = exit_spec.get("evidence_type", "observation")
        expect = exit_spec.get("expect") or []

        forced = False
        if evidence_type == "artifact" and expect:
            missing = [p for p in expect if not _path_exists(p, self.run_dir)]
            if missing:
                if not force:
                    return {
                        "ok": False,
                        "message": (
                            f"exit blocked: missing artifacts: {', '.join(missing)}. "
                            "Use force=True to downgrade with residue + impact."
                        ) + self._route_hint(node_id),
                        "missing": missing,
                    }
                # Downgrade allowed, but the residue + impact narrative is a hard gate.
                if _is_perfunctory(residue, 8) or _is_perfunctory(impact, 10):
                    return {
                        "ok": False,
                        "message": (
                            f"force exit requires non-perfunctory residue and impact "
                            f"(missing: {', '.join(missing)})"
                        ),
                        "missing": missing,
                    }
                forced = True
                closed_by = "unverified_override"
            else:
                closed_by = "evidence_verified"
        else:
            closed_by = "self_certified"

        n.status = "completed"
        n.closed_by = closed_by
        n.time_range[1] = round(time.monotonic(), 2)
        n.iter_range[1] = self._iter
        n.summary = _clip(summary, 600)
        n.gaps = _str_list(gaps, limit=10)
        if forced:
            n.residue = _clip(residue, 800)
            n.impact = _clip(impact, 800)
        self.cursor = node_id

        msg = f"exited {node_id} ({closed_by})"
        if forced:
            downstream = ", ".join(
                d.id for d in self._descendants(node_id) if d.status in OPEN_STATUS
            )[:200] or "-"
            msg += (
                f"\n  residue: {n.residue}\n"
                f"  impact: {n.impact}\n"
                f"  downstream still open: {downstream}"
            )
        return {"ok": True, "message": msg, "closed_by": closed_by, "forced": forced}

    def _op_add(self, kind: str, **kwargs) -> dict:
        if kind == "extend":
            parent_id = str(kwargs.get("after") or "").strip() or self.cursor or ROOT_ID
            edge_kind = str(kwargs.get("kind") or "then").strip().lower()
        else:  # fork
            # ``from`` is a Python keyword; accept ``from_node`` from direct
            # graph_op calls and ``from`` from dict-based apply_revision.
            parent_id = str(kwargs.get("from") or kwargs.get("from_node") or "").strip() or self.cursor or ROOT_ID
            edge_kind = "alt"

        parent, err = self._require_node(parent_id)
        if parent is None:
            return {"ok": False, "message": err}

        raw = kwargs.get("node")
        if isinstance(raw, (list, tuple)):
            return {"ok": False, "message": "inline graph_op allows only one node at a time"}

        node_id = self._next_node_id()
        new_node = _normalize_node(raw, node_id, parent=parent_id)
        if not new_node.goal and not new_node.title:
            return {"ok": False, "message": "node must have a title or goal"}

        self.nodes[node_id] = new_node
        self._add_edge(parent_id, node_id, edge_kind)
        return {
            "ok": True,
            "message": f"added {node_id}「{new_node.title}」 after {parent_id} ({edge_kind})",
            "node_id": node_id,
        }

    def _op_abandon(self, node: str = "", reason: str = "", side_effects: Any = None, **_) -> dict:
        node_id = str(node or "").strip() or self.cursor
        n, err = self._require_node(node_id)
        if n is None:
            return {"ok": False, "message": err}
        if node_id == ROOT_ID:
            return {"ok": False, "message": "root node is immutable"}

        self._merge_side_effects(n, side_effects)
        n.status = "abandoned"
        n.abandon_reason = _clip(reason, 300)
        n.time_range[1] = round(time.monotonic(), 2)

        # Cascade: downstream `then` nodes that haven't started are abandoned too,
        # to avoid orphans. alt/fallback point to *alternatives* — they stay.
        cascaded: list[str] = []
        for child in self._descendants(node_id, only_then=True):
            if child.status == "pending":
                child.status = "abandoned"
                child.abandon_reason = f"cascade from {node_id}"
                cascaded.append(child.id)

        msg = f"abandoned {node_id} — {n.abandon_reason or '-'}"
        if cascaded:
            msg += f" (cascaded: {', '.join(cascaded)})"
        msg += self._route_hint(node_id)
        return {"ok": True, "message": msg, "cascaded": cascaded}

    def _op_block(self, node: str = "", reason: str = "", side_effects: Any = None, **_) -> dict:
        node_id = str(node or "").strip() or self.cursor
        n, err = self._require_node(node_id)
        if n is None:
            return {"ok": False, "message": err}
        if node_id == ROOT_ID:
            return {"ok": False, "message": "root node is immutable"}

        self._merge_side_effects(n, side_effects)
        n.status = "blocked"
        n.summary = _clip(reason, 300)
        return {"ok": True, "message": f"blocked {node_id} — {n.summary or '-'}"}

    def _op_complete(self, reason: str = "", **_) -> dict:
        """Explicit graph completion.

        Deliberately *not* auto-completing when all nodes reach terminal state:
        under local forward planning, reaching the end of planned nodes is the
        norm, not the finish line — the model usually wants to ``extend`` next.
        Auto-completing would close the graph and break that extend.
        """
        open_left = [n.id for n in self.nodes.values() if n.status in OPEN_STATUS]
        if open_left:
            return {
                "ok": False,
                "message": f"cannot complete: open nodes remain: {', '.join(sorted(open_left))}",
                "open_nodes": open_left,
            }
        self.status = "completed"
        self.closed_reason = _clip(reason, 300)
        self.time_end = round(time.monotonic(), 2)
        return {"ok": True, "message": f"graph {self.gid} completed"}

    def _merge_side_effects(self, node: GraphNode, items: Any) -> None:
        for item in _str_list(items, limit=20):
            if item not in node.side_effects:
                node.side_effects.append(item)

    # ── structural revision (plan_revise) ──────────────────────────────────

    def apply_revision(self, ops: Any) -> dict:
        """Batch-apply structural operations. Returns {ok, fail, details}."""
        if isinstance(ops, dict):
            ops = [ops]
        if not isinstance(ops, (list, tuple)):
            return {"ok": 0, "fail": 0, "details": []}

        ok_count = 0
        fail_count = 0
        details: list[str] = []
        for op in ops[:40]:
            if not isinstance(op, dict):
                details.append("✗ (not a dict)")
                fail_count += 1
                continue
            kind = str(op.get("op") or "").strip().lower()
            kwargs = {k: v for k, v in op.items() if k != "op"}
            if kind == "update":
                result = self._op_update(**kwargs)
            else:
                result = self.graph_op(kind, **kwargs)
            details.append(("✓ " if result.get("ok") else "✗ ") + result.get("message", ""))
            if result.get("ok"):
                ok_count += 1
            else:
                fail_count += 1
        return {"ok": ok_count, "fail": fail_count, "details": details}

    def _op_update(self, node: str = "", **kwargs) -> dict:
        node_id = str(node or "").strip()
        n, err = self._require_node(node_id)
        if n is None:
            return {"ok": False, "message": err}
        if n.status in ("completed", "abandoned"):
            return {"ok": False, "message": f"node {node_id} is terminal ({n.status})"}

        changed: list[str] = []
        if kwargs.get("title"):
            n.title = _clip(kwargs.get("title"), 40)
            changed.append("title")
        if kwargs.get("goal"):
            n.goal = _clip(kwargs.get("goal"), 600)
            changed.append("goal")
        if isinstance(kwargs.get("exit"), dict):
            n.exit = _normalize_exit(kwargs.get("exit"))
            changed.append("exit")
        if kwargs.get("budget") is not None:
            try:
                n.budget = max(0, min(int(kwargs.get("budget")), 200))
                changed.append("budget")
            except Exception:
                pass

        if not changed:
            return {"ok": False, "message": f"update noop for {node_id}"}
        return {"ok": True, "message": f"updated {node_id}: {', '.join(changed)}"}

    # ── abandon whole graph ────────────────────────────────────────────────

    def abandon(self, reason: str = "") -> dict:
        """Abandon the entire graph."""
        self.status = "abandoned"
        self.closed_reason = _clip(reason, 300)
        self.time_end = round(time.monotonic(), 2)
        return {"ok": True, "message": f"graph {self.gid} abandoned — {self.closed_reason or '-'}"}

    # ── context rendering ──────────────────────────────────────────────────

    def _node_line(self, node: GraphNode, *, with_reason: bool = False) -> str:
        mark = _STATUS_MARK.get(node.status, "·")
        line = f"{node.id} {mark} 「{node.title}」"
        if with_reason and node.status == "abandoned" and node.abandon_reason:
            line += f" — {_clip(node.abandon_reason, 120)}"
        if node.status == "blocked" and node.summary:
            line += f" — {_clip(node.summary, 120)}"
        return line

    def render_context(self) -> str:
        """Render current graph state for LLM context injection.

        Shows: header, current node (full), path, siblings, residue, overrides,
        upcoming planned nodes, folded abandoned branches, and the protocol hint.
        """
        if self.status != "active":
            return f"[graph {self.gid}「{self.title}」 — {self.status}]"

        nodes = self.nodes
        total = max(0, len(nodes) - 1)  # exclude root
        done = sum(1 for n in nodes.values() if n.status == "completed" and n.id != ROOT_ID)

        sections: list[str] = [
            f"── 执行图 {self.gid}「{self.title}」 ({done}/{total} done) ──"
        ]

        current = self.current_node()
        if current is not None:
            sections.append(
                f"▶ 当前: {current.id}「{current.title}」"
            )
            if current.goal:
                sections.append(f"  目标: {current.goal}")
            exit_spec = current.exit or {}
            sections.append(
                f"  出口: evidence={exit_spec.get('evidence_type', 'observation')}, "
                f"expect={', '.join(exit_spec.get('expect') or []) or '-'}"
            )
            if current.budget:
                sections.append(f"  预算: {current.budget} iterations (granted={current.granted})")
        else:
            frontier = sorted(
                (n for n in nodes.values() if n.status == "pending"),
                key=lambda n: n.id,
            )
            if frontier:
                sections.append(
                    "  待进入: " + "; ".join(self._node_line(n) for n in frontier[:5])
                )
            else:
                sections.append("  (无待办节点 — 可 extend 或 complete)")

        # Path (ancestor chain)
        anchor_id = (current.id if current else None) or self.cursor or ROOT_ID
        chain = self._ancestors(anchor_id)
        if chain:
            sections.append(
                "  路径: " + " → ".join(self._node_line(n) for n in chain[-6:])
            )

        # Siblings (same parent)
        anchor = nodes.get(anchor_id)
        parent_id = anchor.parent if anchor else None
        if parent_id:
            siblings = [n for n in self._children(parent_id) if n.id != anchor_id]
            if siblings:
                sections.append(
                    "  同层:\n" + "\n".join(
                        f"    - {self._node_line(n, with_reason=True)}" for n in siblings[:6]
                    )
                )

        # Residue from abandoned branches
        residue_lines: list[str] = []
        for node in nodes.values():
            if node.status != "abandoned":
                continue
            for item in node.side_effects:
                residue_lines.append(f"    - [{node.id}] {item}")
        if residue_lines:
            sections.append("  环境残留:\n" + "\n".join(residue_lines[:10]))

        # Downgrade overrides (permanent — survives compression)
        overrides = [
            n for n in nodes.values()
            if n.closed_by == "unverified_override" and n.residue
        ]
        if overrides:
            sections.append(
                "  降级遗留 (常驻):\n"
                + "\n".join(
                    f"    - [{n.id}「{n.title}」] {n.residue}\n"
                    f"      影响评估: {n.impact or '-'}"
                    for n in overrides[:6]
                )
            )

        # Upcoming planned
        if current is not None:
            upcoming = sorted(
                (n for n in nodes.values() if n.status == "pending"),
                key=lambda n: n.id,
            )
            if upcoming:
                sections.append(
                    "  前方:\n" + "\n".join(
                        f"    - {self._node_line(n)}" for n in upcoming[:5]
                    )
                )

        # Folded abandoned subtrees
        shown = {n.id for n in self._children(parent_id)} if parent_id else set()
        folded: list[str] = []
        abandoned_ids = {n.id for n in nodes.values() if n.status == "abandoned"}
        for node in nodes.values():
            if node.status != "abandoned" or node.id in shown:
                continue
            if node.parent in abandoned_ids:
                continue  # only report subtree roots
            subtree = [d for d in self._descendants(node.id) if d.status == "abandoned"]
            folded.append(
                f"    - {node.id} (含 {len(subtree) + 1} 节点) — "
                f"{_clip(node.abandon_reason, 120) or '-'}"
            )
        if folded:
            sections.append("  已废弃分支:\n" + "\n".join(folded[:6]))

        sections.append(
            "  协议: 进入节点 → 工作 → exit(summary) → 下一个; "
            "撞墙用 fork/abandon; 走完用 complete."
        )

        return "\n".join(s for s in sections if s)

    # ── serialization ──────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize for persistence (graph.json)."""
        return {
            "gid": self.gid,
            "title": self.title,
            "status": self.status,
            "cursor": self.cursor,
            "created_reason": self.created_reason,
            "from_skill": self.from_skill,
            "run_dir": self.run_dir,
            "created_iter": self.created_iter,
            "closed_iter": self.closed_iter,
            "closed_reason": self.closed_reason,
            "time_start": self.time_start,
            "time_end": self.time_end,
            "time_budget": self.time_budget,
            "budget_granted": self.budget_granted,
            "nodes": {nid: n.to_dict() for nid, n in self.nodes.items()},
            "edges": [e.to_dict() for e in self.edges],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ExecutionGraph":
        """Reconstruct from :meth:`to_dict` output."""
        g = cls.__new__(cls)
        g.gid = data.get("gid", "g1")
        g.title = data.get("title", g.gid)
        g.status = data.get("status", "active")
        g.cursor = data.get("cursor", ROOT_ID)
        g.created_reason = data.get("created_reason", "")
        g.from_skill = data.get("from_skill")
        g.run_dir = data.get("run_dir", "")
        g.created_iter = data.get("created_iter", 0)
        g.closed_iter = data.get("closed_iter")
        g.closed_reason = data.get("closed_reason", "")
        g.time_start = data.get("time_start", round(time.monotonic(), 2))
        g.time_end = data.get("time_end")
        g.time_budget = data.get("time_budget")
        g.budget_granted = data.get("budget_granted", 0)
        g._iter = 0

        g.nodes = {}
        for nid, nd in (data.get("nodes") or {}).items():
            g.nodes[nid] = GraphNode(
                id=nd.get("id", nid),
                title=nd.get("title", nid),
                goal=nd.get("goal", ""),
                exit=nd.get("exit", {"evidence_type": "none", "expect": []}),
                budget=nd.get("budget", 0),
                status=nd.get("status", "pending"),
                parent=nd.get("parent"),
                summary=nd.get("summary", ""),
                side_effects=list(nd.get("side_effects", [])),
                gaps=list(nd.get("gaps", [])),
                visits=nd.get("visits", 0),
                granted=nd.get("granted", False),
                residue=nd.get("residue", ""),
                impact=nd.get("impact", ""),
                closed_by=nd.get("closed_by", ""),
                abandon_reason=nd.get("abandon_reason", ""),
                time_range=list(nd.get("time_range", [None, None])),
                isolate=nd.get("isolate", False),
                seg=nd.get("seg"),
                iter_range=list(nd.get("iter_range", [None, None])),
            )
        if ROOT_ID not in g.nodes:
            g.nodes[ROOT_ID] = GraphNode(
                id=ROOT_ID, title="前序工作", goal="",
                exit={"evidence_type": "none", "expect": []},
                budget=0, status="completed", closed_by="implicit",
            )

        g.edges = [
            GraphEdge(from_node=e.get("from", ""), to_node=e.get("to", ""),
                      kind=e.get("kind", "then"))
            for e in (data.get("edges") or [])
        ]
        return g

    def summary(self) -> dict:
        """State snapshot for dashboard / checkpoint."""
        nodes = self.nodes
        current = self.current_node()
        return {
            "gid": self.gid,
            "status": self.status,
            "title": self.title,
            "total": max(0, len(nodes) - 1),
            "done": sum(1 for n in nodes.values() if n.status == "completed" and n.id != ROOT_ID),
            "open": sum(1 for n in nodes.values() if n.status in OPEN_STATUS),
            "active_node": current.id if current else "",
            "budget_granted": self.budget_granted,
        }
    # ── time budget & stall detection ─────────────────────────────────────
    # Ported from QevosAgent graph.py. Uses time.monotonic() instead of
    # _timing.active_seconds(state), self._iter instead of state.iteration.
    # All i18n t(...) calls replaced with plain English strings.

    def time_used(self) -> float:
        """Active time spent on this graph (frozen after close).

        After the graph closes, time_end is frozen — otherwise a long-expired
        graph's "used time" would grow with the run, making it impossible to
        read how long it actually took (which is the only reliable basis for
        estimating the next graph's budget).
        """
        started = self.time_start
        if not isinstance(started, (int, float)):
            return 0.0
        ended = self.time_end
        if not isinstance(ended, (int, float)):
            ended = time.monotonic()
        return max(0.0, float(ended) - float(started))

    def time_left(self) -> Optional[float]:
        """Remaining budget in seconds; None if no budget set."""
        budget = self.time_budget
        if not isinstance(budget, (int, float)) or budget <= 0:
            return None
        return float(budget) - self.time_used()

    def expire_if_out_of_time(self) -> Optional["ExecutionGraph"]:
        """Expire the graph if budget is exhausted; return self if expired, else None.

        Only closes the graph, not the run — the run may continue in free mode
        after the graph expires. "Graph-after-graph" is bounded by the run-level
        total time limit.
        """
        try:
            if self.status != "active":
                return None
            left = self.time_left()
            if left is None or left > 0:
                return None
            self.status = "expired"
            self.closed_iter = self._iter
            if not isinstance(self.time_end, (int, float)):
                self.time_end = round(time.monotonic(), 2)
            self.closed_reason = f"expired (used {round(self.time_used(), 1)}s)"
            return self
        except Exception:
            return None

    def pace(self) -> dict:
        """Measured speed: nodes closed / avg time per node.

        This is what makes self-requested budgets viable — the first graph is
        a pure guess, but from the second onward the model should estimate
        based on this number. Without it, the request stays at the optimistic
        value forever.
        """
        try:
            spans = [
                s for s in (
                    self.node_seconds(n) for n in self.nodes.values()
                    if n.id != ROOT_ID
                ) if s is not None and s > 0
            ]
            if not spans:
                return {}
            total = sum(spans)
            return {
                "gid": self.gid,
                "nodes": len(spans),
                "total": round(total, 1),
                "per_node": round(total / len(spans), 1),
            }
        except Exception:
            return {}

    def pending_isolate(self) -> Optional[GraphNode]:
        """Return the active node if it declared isolate and hasn't been sealed yet.

        Nodes default to *not* sealing — sealing means KV-cache reset, and
        sealing every node would make planning itself an order of magnitude
        more expensive. Only seal when the model explicitly says the next
        segment doesn't need this node's process detail.
        """
        node = self.current_node()
        if node and node.isolate and node.seg is None:
            return node
        return None

    def mark_sealed(self, node: GraphNode, seg: Any) -> None:
        """Seal segment boundary on node."""
        node.seg = seg

    def node_seconds(self, node: GraphNode) -> Optional[float]:
        """Per-node actual time in seconds; None if not closed or never entered."""
        rng = node.time_range
        if not isinstance(rng, list) or len(rng) != 2:
            return None
        if rng[0] is None or rng[1] is None:
            return None
        try:
            return max(0.0, float(rng[1]) - float(rng[0]))
        except Exception:
            return None

    def _closure_order(self) -> list[GraphNode]:
        """Closed nodes sorted by close iteration (root excluded — implicitly closed)."""
        closed = [
            n for n in self.nodes.values()
            if n.status == "completed" and n.id != ROOT_ID
            and isinstance(n.iter_range, list) and n.iter_range[1] is not None
        ]
        closed.sort(key=lambda n: n.iter_range[1])
        return closed

    def metrics(self) -> dict:
        """Compute convergence metrics. Returns {} when no nodes. Exceptions swallowed."""
        try:
            nodes = [n for n in self.nodes.values() if n.id != ROOT_ID]
            if not nodes:
                return {}

            now = self._iter
            closed = self._closure_order()
            # Abandonment is not progress: it saved effort but didn't advance the goal.
            last_progress = closed[-1].iter_range[1] if closed else int(self.created_iter or 0)

            done_count = len(closed)
            open_count = sum(1 for n in nodes if n.status in OPEN_STATUS)

            # Consecutive self-certified closures: artifact-type exits are
            # verifiable; the other three are self-certified. The model could
            # spike closure rate by closing a string of "I observed X" —
            # we track this as a soft signal, don't forbid it.
            unverified = 0
            for n in reversed(closed):
                if n.closed_by in ("self_certified", "unverified_override"):
                    unverified += 1
                else:
                    break

            # Stall also needs a time ruler: after acknowledging non-uniform
            # iterations, "20 iters without closure" is equally untenable —
            # 20 iters could be 100 seconds or 10 hours. Two rulers catch
            # different kinds of spinning.
            stall_secs = 0.0
            if closed:
                last_t = closed[-1].time_range
                if isinstance(last_t, list) and len(last_t) == 2 and last_t[1] is not None:
                    stall_secs = max(0.0, time.monotonic() - float(last_t[1]))
            elif isinstance(self.time_start, (int, float)):
                stall_secs = max(0.0, time.monotonic() - float(self.time_start))

            active = self.current_node()
            active_used = 0
            if active and isinstance(active.iter_range, list) and active.iter_range[0] is not None:
                active_used = max(0, now - int(active.iter_range[0]))
            return {
                "gid": self.gid,
                "stall_secs": round(stall_secs, 1),
                "time_left": self.time_left(),
                "active_used": active_used,
                "active_budget": int(active.budget) if active else 0,
                "stall_iters": max(0, now - int(last_progress)),
                "node_revisits": max([int(n.visits) for n in nodes] or [0]),
                "revisit_node": max(nodes, key=lambda n: int(n.visits)).id,
                "open_fanout": round(open_count / max(1, done_count), 2),
                "open_count": open_count,
                "done_count": done_count,
                "unverified_streak": unverified,
                "active_node": active.id if active else "",
                "active_title": active.title if active else "",
            }
        except Exception:
            return {}

    def stall_level(self) -> tuple[int, str]:
        """Translate metrics to escalation level (0..2, reason).

        L3 is decided by the loop based on continued stall after L2.
        Only gives advice, takes no action — all actions go through the
        existing escalation ladder to avoid two mechanisms fighting.
        """
        m = self.metrics()
        if not m:
            return 0, ""
        stall = int(m.get("stall_iters") or 0)
        revisits = int(m.get("node_revisits") or 0)
        fanout = float(m.get("open_fanout") or 0)
        unverified = int(m.get("unverified_streak") or 0)
        stall_secs = float(m.get("stall_secs") or 0.0)

        # Iteration and time: first to arrive wins. Short spinning and long
        # spinning are both spinning — two rulers catch different kinds.
        stalled_l1 = (stall >= _env_int("GRAPH_STALL_L1", 20)
                      or stall_secs >= _env_int("GRAPH_STALL_L1_SECS", 1800))
        stalled_l2 = (stall >= _env_int("GRAPH_STALL_L2", 40)
                      or stall_secs >= _env_int("GRAPH_STALL_L2_SECS", 3600))

        # Fanout needs warmup: at least one node must have closed for
        # "only branching, never closing" to be a valid claim — its premise
        # is that there *was* a chance to close. Otherwise a freshly drawn
        # 5-node plan with 5 open / 0 done = 5.0 gets flagged as L2 stall
        # on the first iteration after creation (seen in practice).
        fanout_ready = int(m.get("done_count") or 0) >= 1
        fanout_hit = fanout_ready and fanout >= float(_env_int("GRAPH_FANOUT_L2", 5))

        if (stalled_l2
                or revisits >= _env_int("GRAPH_REVISIT_L2", 5)
                or fanout_hit):
            if stalled_l2:
                return 2, "stall"
            return 2, "revisit" if revisits >= _env_int("GRAPH_REVISIT_L2", 5) else "fanout"

        if stalled_l1:
            return 1, "stall"
        if revisits >= _env_int("GRAPH_REVISIT_L1", 3):
            return 1, "revisit"
        if unverified >= _env_int("GRAPH_UNVERIFIED_L1", 5):
            return 1, "unverified"
        return 0, ""

    def stall_hint(self) -> str:
        """L1 soft hint text. Wording should offer a way out, not just alarm."""
        m = self.metrics()
        reason = self.stall_level()[1]
        node = m.get("active_node") or "-"
        if reason == "stall":
            return f"No node closed for {m.get('stall_iters', 0)} iterations (active: {node}). Consider extending the plan or requesting help."
        if reason == "revisit":
            return f"Node {m.get('revisit_node', node)} revisited {m.get('node_revisits', 0)} times without progress. Consider forking an alternative approach."
        if reason == "fanout":
            return f"Too many open nodes ({m.get('open_count', 0)}) relative to closed ({m.get('done_count', 0)}). Focus on closing existing nodes before branching."
        return f"{m.get('unverified_streak', 0)} consecutive self-certified closures without artifact evidence. Verify outputs before proceeding."

    def _artifacts_present(self, node: GraphNode) -> bool:
        """Check if this open node's declared artifacts now actually exist."""
        exit_spec = node.exit or {}
        if exit_spec.get("evidence_type") != "artifact":
            return False
        expect = exit_spec.get("expect") or []
        if not expect:
            return False
        return all(_path_exists(p, self.run_dir) for p in expect)

    def open_nodes(self) -> list[dict]:
        """Unclosed nodes for run gaps.

        Each node carries `likely_done`: the node is still open on the graph,
        but its declared artifacts now exist. Typical path: graph expired,
        model fell back to free mode and finished the work 3 minutes later —
        the graph stays unchanged (it's history), but gaps must acknowledge
        the done work, else the continuation re-does it.
        """
        try:
            out: list[dict] = []
            for node in self.nodes.values():
                if node.status not in OPEN_STATUS:
                    continue
                out.append({
                    "node": node.id,
                    "title": node.title,
                    "goal": node.goal,
                    "status": node.status,
                    "exit": dict(node.exit),
                    "parent": node.parent or "",
                    "likely_done": self._artifacts_present(node),
                })
            out.sort(key=lambda n: n.get("node", ""))
            return out
        except Exception:
            return []

    def override_nodes(self) -> list[dict]:
        """Degraded-closed nodes. Status is completed but with unfulfilled promises,
        so they're equally gaps — not reporting them in gaps erases the debt on continuation."""
        try:
            out = []
            for n in self.nodes.values():
                if n.closed_by != "unverified_override":
                    continue
                out.append({
                    "node": n.id,
                    "title": n.title,
                    "status": "done_unverified",
                    "residue": n.residue,
                    "impact": n.impact,
                    "exit": dict(n.exit),
                    "parent": n.parent or "",
                })
            out.sort(key=lambda n: n.get("node", ""))
            return out
        except Exception:
            return []

    def gap_lines(self) -> list[str]:
        """Render gaps as text lines.

        run_outcome["gaps"] contract is list[str]; consumers (auto-continuation)
        process strings. We don't change the contract — just compress structured
        info into one line. Structured originals go in graph_gaps.
        """
        lines: list[str] = []
        likely: list[str] = []
        for n in self.open_nodes():
            exit_spec = n.get("exit") or {}
            expect = ", ".join(exit_spec.get("expect") or [])
            if n.get("likely_done"):
                likely.append(
                    f"[{n.get('node', '?')}] {n.get('title', '')} — artifacts present ({expect or '-'})"
                )
                continue
            lines.append(
                f"[{n.get('node', '?')}] {n.get('title', '')} — "
                f"status:{n.get('status', '')} goal:{_clip(n.get('goal'), 200) or '-'} "
                f"exit:{exit_spec.get('evidence_type', '-')} expect:{expect or '-'}"
            )
        for n in self.override_nodes():
            lines.append(
                f"[{n.get('node', '?')}] {n.get('title', '')} — "
                f"override residue:{n.get('residue', '')} impact:{n.get('impact', '') or '-'}"
            )
        return lines + likely
