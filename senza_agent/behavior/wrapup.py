"""Wrap-up window — budget-exhaustion handler, ported from QevosAgent.

When the run's cost budget is exhausted, the agent is given a small number of
extra turns (``wrapup_turns``) to submit a completion report. During this
window each turn is preceded by a reminder, and once the turns run out the
``should_stop`` hook terminates the run.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from senza_agent.behavior.state import AgentState
    from senza_agent.config import BehaviorConfig

def wrapup_window(
    state: "AgentState", config: "BehaviorConfig"
) -> Callable[[dict], Optional[dict]]:
    """Return a ``prepare_next_turn`` hook that drives the wrap-up window.

    - When cost >= ``config.budget_limit`` and the window hasn't opened yet,
      open it: set ``state.wrapup_turns_left`` and emit a reminder.
    - While the window is open and turns remain, decrement and emit a reminder
      (or ``None`` on the last turn, letting ``should_stop`` handle it).
    - Otherwise return ``None``.

    The reminder text is written to ``state.pending_injections`` so that the
    ``transform_context`` hook (context_injector) can inject it into the LLM
    context.  Returning a dict from ``prepare_next_turn`` does NOT inject it.
    """

    def _hook(ctx: dict) -> Optional[dict]:
        cost = _extract_cost(ctx)

        # Window not yet opened
        if state.wrapup_turns_left is None:
            if cost >= config.budget_limit:
                state.wrapup_turns_left = config.wrapup_turns
                _emit_reminder(state, state.wrapup_turns_left)
            return None

        # Window open
        if state.wrapup_turns_left > 0:
            state.wrapup_turns_left -= 1
            if state.wrapup_turns_left > 0:
                _emit_reminder(state, state.wrapup_turns_left)
            # Reached 0 — let should_stop terminate; no reminder needed.
            return None

        # wrapup_turns_left == 0 but window was open: nothing to inject.
        return None

    return _hook


def wrapup_stop(state: "AgentState") -> Callable[[dict], bool]:
    """Return a ``should_stop`` hook that terminates the run once turns are spent."""

    def _hook(ctx: dict) -> bool:
        return state.wrapup_turns_left is not None and state.wrapup_turns_left <= 0

    return _hook


def behavior_should_stop(state: "AgentState") -> Callable[[dict], bool]:
    """Return a combined ``should_stop`` hook.

    Logic (checked in order):
    1. If ``state.needs_remediation`` is True, return False (continue the
       loop so the model sees the remediation feedback injected by
       ``context_injector``). Consume the flag.
    2. If ``state.wrapup_turns_left`` is not None and <= 0, return True
       (budget exhausted, wrap-up window closed).
    3. Otherwise return False (normal continuation).
    """

    def _hook(ctx: dict) -> bool:
        # 1. Remediation takes priority — let the model see the feedback
        if getattr(state, "needs_remediation", False):
            state.needs_remediation = False
            return False
        # 2. Wrap-up window exhausted
        if state.wrapup_turns_left is not None and state.wrapup_turns_left <= 0:
            return True
        # 3. Normal
        return False

    return _hook


# ── helpers ─────────────────────────────────────────────────────────────────


def _extract_cost(ctx: dict) -> float:
    usage = ctx.get("usage") if isinstance(ctx, dict) else None
    if not isinstance(usage, dict):
        return 0.0
    try:
        return float(usage.get("total_cost", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _reminder_text(turns_left: int) -> str:
    return (
        "[wrap-up window] Budget exhausted. "
        f"You have {turns_left} turn(s) left to wrap up. "
        "Call submit_completion_report with any gaps, then finish."
    )


def _emit_reminder(state: "AgentState", turns_left: int) -> None:
    """Append the wrap-up reminder to ``state.pending_injections``.

    The ``transform_context`` hook drains ``pending_injections`` and appends
    each entry as a user message before the next LLM call.
    """
    state.pending_injections.append(_reminder_text(turns_left))
