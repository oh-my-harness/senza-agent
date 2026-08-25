"""BehaviorBundle: assembles all behavior mechanisms into a single bundle."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from senza_agent.behavior.state import AgentState
    from senza_agent.config import BehaviorConfig


class BehaviorBundle:
    """Assembles all behavior mechanisms into a single bundle for agent.py."""

    def __init__(self, state: "AgentState", config: "BehaviorConfig") -> None:
        from senza_agent.behavior.acceptance_gate import (
            acceptance_gate_tools,
            acceptance_validator,
        )
        from senza_agent.behavior.advisor import advisor_runner
        from senza_agent.behavior.context_injector import behavior_transform_context
        from senza_agent.behavior.wrapup import behavior_should_stop, wrapup_window

        import senza

        self.state = state
        self.config = config
        self.tools = acceptance_gate_tools(state)
        self.hooks = [
            senza.hooks.after_turn(advisor_runner(state, config)),
            senza.hooks.transform_context(behavior_transform_context(state)),
            senza.hooks.prepare_next_turn(wrapup_window(state, config)),
        ]
        self.validator = senza.hooks.final_answer_validator(
            acceptance_validator(state)
        )
        self.should_stop = senza.hooks.should_stop(behavior_should_stop(state))
