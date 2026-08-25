"""Transform-context hook: injects pending messages into the LLM context.

This is the CORRECT way to inject messages from hooks in Senza. The
``transform_context`` hook receives the full AgentContext (system_prompt +
messages) before each LLM call and returns a modified copy. This is how
StatusBarHook, TruncationContinuationHook, and InjectionFilterHook work
in the runtime.

Previous code tried to call ``harness.steer()`` from ``after_turn`` hooks,
but hook ctx dicts do not contain a harness reference, so those calls
silently did nothing. This module replaces that broken pattern.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from senza_agent.behavior.state import AgentState


def _make_user_message(text: str) -> dict:
    """Build a serialized UserMessage dict suitable for AgentContext.messages."""
    return {
        "role": "user",
        "content": [{"type": "text", "text": text}],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def behavior_transform_context(state: "AgentState") -> Callable[[dict], dict]:
    """Return a ``senza.hooks.transform_context`` callback.

    The callback reads pending injections from ``state`` and appends them
    as user messages to the context. Each injection is consumed (cleared)
    after being appended, so it is injected exactly once.
    """

    def _hook(ctx: dict) -> dict:
        messages = list(ctx.get("messages") or [])
        injected = False

        # 1a. Advisor advice (highest priority — strategic guidance)
        advice = (getattr(state, "last_advice", "") or "").strip()
        if advice:
            messages.append(_make_user_message(f"[advisor] {advice}"))
            state.last_advice = ""
            injected = True

        # 1b. Graph stall detection
        try:
            from senza_agent.tools.graph_tools import get_graph
            graph = get_graph()
            if graph is not None and graph.status == "active":
                graph.expire_if_out_of_time()
                level, reason = graph.stall_level()
                if level >= 1:
                    hint = graph.stall_hint()
                    messages.append(_make_user_message(
                        f"[graph] Convergence stall detected (level {level}, {reason}): {hint}"
                    ))
                    state.advisor_requested = True
                    injected = True
        except Exception:
            pass

        # 1c. Background task completion notifications
        try:
            from senza_agent.tools.async_manager import get_manager as get_async_mgr
            async_mgr = get_async_mgr()
            if async_mgr is not None:
                completed = async_mgr.drain_completed()
                for job_id, output in completed:
                    messages.append(_make_user_message(
                        f"[background] Job {job_id} completed:\n{output}"
                    ))
                    injected = True
        except Exception:
            pass

        # 1d. Watcher events
        try:
            from senza_agent.tools.watcher import get_manager as get_watcher_mgr
            watcher_mgr = get_watcher_mgr()
            if watcher_mgr is not None:
                turn_index = ctx.get("turn_index", 0)
                events = watcher_mgr.poll(turn_index)
                for ev in events:
                    content = ev.get("content", "")
                    if content:
                        messages.append(_make_user_message(content))
                        injected = True
        except Exception:
            pass

        # 2. General-purpose pending injections (bg tasks, watchers, etc.)
        pending = getattr(state, "pending_injections", None)
        if pending:
            for text in pending:
                messages.append(_make_user_message(text))
            state.pending_injections = []
            injected = True

        if injected:
            ctx["messages"] = messages
        return ctx

    return _hook
