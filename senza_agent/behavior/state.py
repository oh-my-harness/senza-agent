"""AgentState: state shared across hooks and tools during a single run."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentState:
    """State shared across hooks and tools during a single run."""

    completion_report: dict | None = None
    turn_count: int = 0
    last_advice: str = ""
    budget_exhausted: bool = False
    wrapup_turns_left: int | None = None
    goal: str = ""
    advisor_requested: bool = False
    episodic_required: bool = False
    concept_required: bool = False
    needs_remediation: bool = False
    pending_injections: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    run_dir: str = ""
    tools: list = field(default_factory=list)
    # ── Tools-module shared state (mirrors tools.standard._StateRef) ─────
    # agent.py passes this dataclass to tools.standard.set_state(); every
    # attribute the tool callbacks read/write must live here too.
    evolved_tools: dict = field(default_factory=dict)
    repair_candidates: dict = field(default_factory=dict)
    repair_failures: dict = field(default_factory=dict)
    repair_history: list = field(default_factory=list)
    long_term: list = field(default_factory=list)
    concept_memory: str = ""
    runtime_patches: list = field(default_factory=list)
    interrupt_handler: Any = None
    vision_supported: bool | None = None
    bad_image_urls: dict = field(default_factory=dict)
