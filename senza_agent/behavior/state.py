"""AgentState: state shared across hooks and tools during a single run."""
from __future__ import annotations

from dataclasses import dataclass, field


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
    meta: dict = field(default_factory=dict)
    run_dir: str = ""
    tools: list = field(default_factory=list)
