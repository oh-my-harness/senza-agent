"""Task runner: submits tasks to the agent harness and streams events.

Maintains a single in-flight task at a time. Browser clients connect via
``/ws/task`` WebSocket to receive a live event stream (text deltas, tool
calls, settled/aborted/error). ``POST /api/task`` starts a new task;
``POST /api/abort`` cancels the running task.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Optional

from aiohttp import web, WSMsgType


class TaskManager:
    """Manages agent task execution and event streaming.

    The harness prompt runs on a worker thread; events are relayed to all
    connected WebSocket clients.
    """

    def __init__(self) -> None:
        self._clients: set[web.WebSocketResponse] = set()
        self._harness: Any = None  # AgentHarness
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running: bool = False
        self._task_text: str = ""
        self._task_start: float = 0.0
        self._cancel_flag: bool = False
        self._history: list[dict[str, Any]] = []  # finished task summaries
        self._on_event: Any = None  # callback(event: dict)
        self._on_start: Any = None  # callback(text: str)
        self._on_end: Any = None    # callback(summary: dict)

    # ── Harness wiring ───────────────────────────────────────────────────

    def set_harness(self, harness: Any) -> None:
        """Attach the agent harness (called from CLI after agent creation)."""
        self._harness = harness

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def is_running(self) -> bool:
        return self._running

    # ── WebSocket client management ──────────────────────────────────────

    def add_client(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    def remove_client(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        """Send a JSON message to all connected WS clients."""
        dead: list[web.WebSocketResponse] = []
        for ws in self._clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    # ── Task execution ───────────────────────────────────────────────────

    async def start_task(
        self, text: str, timeout_ms: int = 300000, attachments: Optional[list] = None
    ) -> dict[str, Any]:
        """Start a new task. Returns ``{"ok": True}`` or ``{"error": ...}``.

        ``attachments`` is an optional list of Senza ``Attachment`` objects
        (e.g. from ``senza.image_base64``) passed through to the harness
        prompt — senza-sdk >= 1.3.0.
        """
        if self._harness is None:
            return {"ok": False, "error": "Agent harness not available"}
        if self._running:
            return {"ok": False, "error": "A task is already running"}
        if not text.strip():
            return {"ok": False, "error": "Task text is empty"}

        self._running = True
        self._task_text = text
        self._task_start = time.time()
        self._cancel_flag = False

        # Launch the streaming task in the background.
        asyncio.ensure_future(self._run_task(text, timeout_ms, attachments))
        return {"ok": True, "task": text[:200]}

    async def abort_task(self) -> dict[str, Any]:
        """Abort the running task."""
        if not self._running:
            return {"ok": False, "error": "No task running"}
        try:
            self._harness.abort()
        except Exception:
            pass
        return {"ok": True}

    async def _run_task(self, text: str, timeout_ms: int, attachments: Optional[list] = None) -> None:
        await self._broadcast({
            "type": "task_start",
            "text": text[:500],
            "timestamp": time.time(),
        })
        # _on_start callback removed — API handlers call on_task_start/on_followup
        # directly, so _run_task no longer manages event state.

        events: list[dict[str, Any]] = []

        async def _emit(wire: dict[str, Any]) -> None:
            events.append(wire)
            await self._broadcast({"type": "event", "event": wire})
            if self._on_event:
                import inspect
                if inspect.iscoroutinefunction(self._on_event):
                    await self._on_event(wire)
                else:
                    self._on_event(wire)

        try:
            # Native SDK >= 1.3.0: subscribe to the event stream first, then
            # run the blocking prompt (positional attachments) on a worker
            # thread.  prompt() raises on LLM failure; stream_events yields
            # until the terminal event or stream exhaustion.
            prompt_error: list[BaseException] = []

            def _do_prompt() -> None:
                try:
                    self._harness.prompt(text, attachments)
                except BaseException as exc:  # noqa: BLE001
                    prompt_error.append(exc)

            import senza
            prompt_thread = threading.Thread(target=_do_prompt, daemon=True)
            prompt_thread.start()
            async for ev in senza.stream_events(
                self._harness, timeout_ms=30000, max_consecutive_timeouts=999999
            ):
                if self._cancel_flag:
                    break
                wire = self._normalize_event(ev)
                await _emit(wire)
                if wire.get("type") in ("settled", "aborted", "error", "agent_end"):
                    break
            prompt_thread.join(timeout=60)
            if prompt_error:
                raise prompt_error[0]
        except Exception as e:
            await _emit({"type": "error", "message": str(e)})

        elapsed = time.time() - self._task_start
        summary = {
            "text": text[:200],
            "elapsed": round(elapsed, 2),
            "event_count": len(events),
            "timestamp": time.time(),
        }
        self._history.append(summary)
        if len(self._history) > 50:
            self._history.pop(0)

        await self._broadcast({
            "type": "task_end",
            "summary": summary,
            "timestamp": time.time(),
        })
        if self._on_end:
            import inspect
            if inspect.iscoroutinefunction(self._on_end):
                await self._on_end(summary)
            else:
                self._on_end(summary)

        self._running = False
        self._task_text = ""
        self._cancel_flag = False

    def _normalize_event(self, ev: Any) -> dict[str, Any]:
        """Normalize a harness event into a JSON-safe dict."""
        if isinstance(ev, dict):
            # Already a dict — ensure it's JSON-safe.
            return _json_safe(ev)
        # Try to convert dataclass / pyclass events.
        if hasattr(ev, "__dict__"):
            return _json_safe(vars(ev))
        if hasattr(ev, "_asdict"):
            return _json_safe(ev._asdict())
        return {"type": "unknown", "raw": str(ev)}

    # ── Status ───────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "task": self._task_text[:200] if self._running else "",
            "elapsed": round(time.time() - self._task_start, 2) if self._running else 0,
            "harness_ready": self._harness is not None,
            "history": self._history[-10:],
        }

    # ── WebSocket handler ────────────────────────────────────────────────

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket for task event streaming."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.add_client(ws)

        # Send current status on connect.
        await ws.send_json({"type": "status", **self.status()})

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    mtype = data.get("type", "")
                    if mtype == "ping":
                        await ws.send_json({"type": "pong"})
                    elif mtype == "status":
                        await ws.send_json({"type": "status", **self.status()})
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.remove_client(ws)

        return ws


def _json_safe(obj: Any) -> Any:
    """Recursively convert to JSON-safe types."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items() if k != "_harness"}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)
