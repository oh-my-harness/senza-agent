"""Render panel backend: web_show / web_notify.

Maintains WebSocket clients and pushes render updates / chat notifications
to all connected browsers. The agent tools write content via :func:`show`
and :func:`notify`; the WebSocket handler in ``app.py`` forwards to clients.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web, WSMsgType


@dataclass
class DisplayPanel:
    """A single render panel identified by ``display_id``."""

    display_id: str
    content_type: str = "html"
    title: str = ""
    content: str = ""
    base_path: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "display_id": self.display_id,
            "content_type": self.content_type,
            "title": self.title,
            "content": self.content,
            "base_path": self.base_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class RenderManager:
    """Manages render panels and WebSocket clients.

    The agent calls :meth:`show` / :meth:`notify` to push content; the web
    server broadcasts to all connected WS clients. Browser clients also
    connect via WS to receive real-time updates.
    """

    def __init__(self) -> None:
        self._panels: dict[str, DisplayPanel] = {}
        self._chat_log: list[dict[str, Any]] = []
        self._clients: set[web.WebSocketResponse] = set()

    # ── WebSocket client management ──────────────────────────────────────

    def add_client(self, ws: web.WebSocketResponse) -> None:
        self._clients.add(ws)

    def remove_client(self, ws: web.WebSocketResponse) -> None:
        self._clients.discard(ws)

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        """Send a JSON message to all connected WS clients."""
        data = json.dumps(msg, ensure_ascii=False)
        dead: list[web.WebSocketResponse] = []
        for ws in self._clients:
            if ws.closed:
                dead.append(ws)
                continue
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    # ── web_show ─────────────────────────────────────────────────────────

    def show(
        self,
        content: str,
        content_type: str = "html",
        display_id: str = "default",
        title: str = "",
        mode: str = "replace",
    ) -> dict[str, Any]:
        """Write content to a render panel and push to browsers.

        Args:
            content: The content to display (HTML, markdown, text, chart JSON, etc.).
            content_type: html | markdown | table | chart | text | image
            display_id: Panel identifier; multiple panels can coexist.
            title: Optional panel title.
            mode: replace | append
        """
        now = time.time()
        if mode == "append" and display_id in self._panels:
            panel = self._panels[display_id]
            panel.content += "\n" + content
            panel.updated_at = now
        else:
            self._panels[display_id] = DisplayPanel(
                display_id=display_id,
                content_type=content_type,
                title=title,
                content=content,
                created_at=now,
                updated_at=now,
            )

        panel = self._panels[display_id]
        return panel.to_dict()

    async def show_async(
        self,
        content: str,
        content_type: str = "html",
        display_id: str = "default",
        title: str = "",
        mode: str = "replace",
    ) -> dict[str, Any]:
        """Like :meth:`show` but also broadcasts to WS clients."""
        result = self.show(content, content_type, display_id, title, mode)
        await self._broadcast({"type": "render", **result})
        return result

    # ── web_notify ───────────────────────────────────────────────────────

    def notify(self, message: str, display_id: str = "*") -> dict[str, Any]:
        """Push a chat notification to browser panels.

        Args:
            message: The message text.
            display_id: Target panel; ``"*`` broadcasts to all.
        """
        record = {
            "role": "agent",
            "message": message,
            "display_id": display_id,
            "ts": time.time(),
        }
        self._chat_log.append(record)
        # Keep last 500 messages
        if len(self._chat_log) > 500:
            self._chat_log = self._chat_log[-500:]
        return record

    async def notify_async(self, message: str, display_id: str = "*") -> dict[str, Any]:
        """Like :meth:`notify` but also broadcasts to WS clients."""
        record = self.notify(message, display_id)
        await self._broadcast({"type": "web_chat", **record})
        return record

    # ── Query ────────────────────────────────────────────────────────────

    def get_panel(self, display_id: str) -> dict[str, Any] | None:
        panel = self._panels.get(display_id)
        return panel.to_dict() if panel else None

    def list_panels(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self._panels.values()]

    def get_chat_log(self, since: float = 0.0) -> list[dict[str, Any]]:
        return [r for r in self._chat_log if r["ts"] > since]

    # ── WebSocket handler ────────────────────────────────────────────────

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle a WebSocket connection for render panel updates."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.add_client(ws)

        # Send current state on connect
        await ws.send_str(json.dumps({
            "type": "state",
            "panels": self.list_panels(),
            "chat": self.get_chat_log(),
        }, ensure_ascii=False))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if data.get("type") == "ping":
                        await ws.send_str(json.dumps({"type": "pong", "ts": time.time()}))
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.remove_client(ws)

        return ws
