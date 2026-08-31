"""Version-compatibility shim for the senza-sdk Python layer.

``senza.stream_prompt`` gained the ``max_consecutive_timeouts`` parameter in
SDK 1.2.4 (oh-my-harness/Senza#35). The Rust runtime itself has supported
passing it through to ``obj.events()`` / ``obj.subscribe()`` since 1.2.1, but
the Python wrappers in 1.2.1–1.2.3 hard-code it to ``1`` and reject the
keyword — which makes senza-agent's webserver tasks crash with a TypeError on
those versions (blocking tools such as ask_user then starve the event stream
after ``timeout_ms``).

We vendor the SDK's own ``stream_prompt`` implementation (verbatim from
senza-sdk 1.2.3, plus the extra parameter) and call ``obj.events()`` directly,
so the behaviour is identical across SDK 1.2.1+ regardless of which wheel is
installed. When the installed SDK already exposes the parameter (>= 1.2.4),
the vendored copy is still used — same code path everywhere.
"""
from __future__ import annotations

import asyncio as _asyncio
import threading as _threading
from typing import Any, AsyncGenerator

_TERMINAL_TYPES = frozenset(
    {"agent_end", "error", "settled", "aborted", "workflow_done", "workflow_failed"}
)

_STOP = object()


def _get_event_iterator(obj: Any, timeout_ms: int, max_consecutive_timeouts: int) -> Any:
    """Return the sync event iterator for *obj*, regardless of class."""
    if hasattr(obj, "events"):
        return obj.events(timeout_ms=timeout_ms, max_consecutive_timeouts=max_consecutive_timeouts)
    if hasattr(obj, "subscribe"):
        return obj.subscribe(
            timeout_ms=timeout_ms, max_consecutive_timeouts=max_consecutive_timeouts
        )
    raise TypeError(f"{type(obj).__name__} has no events() or subscribe() method")


async def _next_event(it: Any) -> Any:
    """Call next(it) in a thread, converting StopIteration to a sentinel.

    ``asyncio.to_thread`` cannot propagate ``StopIteration`` because it
    interacts badly with the generator protocol, so we catch it in the
    worker thread and return ``_STOP`` instead.
    """

    def _step() -> Any:
        try:
            return next(it)
        except StopIteration:
            return _STOP

    result = await _asyncio.to_thread(_step)
    return result


async def stream_prompt(
    obj: Any,
    text: str,
    timeout_ms: int = 5000,
    max_consecutive_timeouts: int = 1,
    attachments: Optional[list] = None,
) -> AsyncGenerator[dict, None]:
    """Send a prompt and yield events as they arrive (Agent / AgentHarness).

    Version-compatible re-implementation of ``senza.stream_prompt`` that
    always accepts ``max_consecutive_timeouts`` (see module docstring) and
    forwards ``attachments`` (senza-sdk >= 1.3.0 ``prompt(text, attachments=)``).
    On older SDKs a non-empty ``attachments`` raises TypeError from
    ``obj.prompt`` — surfaced through the stream as the first event's sibling
    error, matching upstream behaviour.

    Starts ``obj.prompt(text, attachments)`` on a background thread, then
    yields events until a terminal event (``agent_end``, ``settled``,
    ``aborted``, ``error``) is received or the stream is exhausted.
    """
    it = _get_event_iterator(obj, timeout_ms, max_consecutive_timeouts)

    done = _threading.Event()
    errors: list = []

    def _do_prompt() -> None:
        try:
            obj.prompt(text, attachments)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    t = _threading.Thread(target=_do_prompt, daemon=True)
    t.start()

    try:
        while True:
            event = await _next_event(it)
            if event is _STOP:
                break
            yield event
            if event.get("type") in _TERMINAL_TYPES:
                break
    finally:
        done.wait(timeout=60)
        t.join(timeout=60)
        if errors:
            raise errors[0]
