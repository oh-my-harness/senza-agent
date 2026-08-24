"""aiohttp web application for senza-agent.

Routes:
  WebSocket:
    GET /ws           — render panel state stream (broadcast)
    GET /ws/term      — interactive terminal (per-client PTY, two-way)

  Render:
    POST /api/show    — web_show (push content to panel)
    POST /api/notify  — web_notify (push chat message)
    GET  /api/panels  — list all panels
    GET  /api/panel/:id — get a single panel

  Terminal:
    GET    /api/term          — list sessions
    POST   /api/term          — create session
    DELETE /api/term/:id      — kill session
    POST   /api/term/:id/input   — write input
    GET    /api/term/:id/output  — read output (since=N)
    POST   /api/term/:id/owner   — set owner (agent/user)

  Files:
    GET  /api/fs/list?path=    — list directory
    GET  /api/fs/read?path=    — read file for preview
    PUT  /api/fs/write         — write file
    GET  /api/fs/raw?path=     — serve binary file
    POST /api/fs/upload?dir=&rel=  — upload file
    GET  /api/fs/zip?path=     — zip directory
    GET  /api/fs/roots         — filesystem roots

  Browser:
    POST /api/browser-action   — perform browser automation action

  Apps:
    GET    /api/apps           — list apps
    GET    /api/app/:id        — get app content
    POST   /api/app/:id        — create/update app
    DELETE /api/app/:id        — delete app
    POST   /api/app/:id/run    — run app

  Static:
    GET /                      — panel.html
    GET /static/*              — static assets
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from aiohttp import web, WSMsgType

from .render import RenderManager
from .terminal import TerminalManager, _strip_ansi
from .files import list_dir, read_file, write_file, upload_file, zip_dir, get_roots, guess_mime
from .browser import BrowserController
from .task import TaskManager
from . import apps as apps_mod
from .qevos_bridge import StateBridge, QevosAPI


_STATIC_DIR = Path(__file__).parent / "static"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8090


class WebServer:
    """aiohttp web server for senza-agent interactive UI.

    Runs on port 8090 alongside the Inspector (port 8080).
    """

    def __init__(
        self,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
    ) -> None:
        self.host = host
        self.port = port
        self.render = RenderManager()
        self.terminal = TerminalManager()
        self.browser = BrowserController()
        self.task = TaskManager()
        self.state_bridge = StateBridge(self.task, self.render)
        self.qevos_api = QevosAPI(self.state_bridge, self.task)
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def set_harness(self, harness: Any) -> None:
        """Attach the agent harness for task execution."""
        self.task.set_harness(harness)
        # Wire task events to the QevosAgent state bridge.
        self.task._on_start = self.state_bridge.on_task_start
        self.task._on_event = self.state_bridge.on_task_event
        self.task._on_end = self.state_bridge.on_task_end

    # ── App setup ────────────────────────────────────────────────────────

    def create_app(self) -> web.Application:
        """Build the aiohttp Application with all routes."""
        app = web.Application()
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        self._add_routes(app)
        self._app = app
        return app

    def _add_routes(self, app: web.Application) -> None:
        # Root: serve dashboard HTML, or upgrade to WebSocket for state stream.
        # QevosAgent frontend connects to ws://host (root path) for state.
        app.router.add_get("/", self._root_handler)
        app.router.add_get("/{filename:[^/]+\\.(js|css|png|svg|ico|woff2?|ttf)}", self._static_file_handler)

        # QevosAgent-compatible REST API
        self.qevos_api.add_routes(app)

        # Task (agent execution)
        app.router.add_get("/ws/task", self.task.ws_handler)
        app.router.add_post("/api/task", self._api_task_start)
        app.router.add_post("/api/abort", self._api_task_abort)
        app.router.add_get("/api/status", self._api_task_status)

        # WebSocket (render panels)
        app.router.add_get("/ws", self._ws_render_handler)
        app.router.add_get("/ws/term", self._ws_term_handler)

        # Render
        app.router.add_post("/api/show", self._api_show)
        app.router.add_post("/api/notify", self._api_notify)
        app.router.add_get("/api/panels", self._api_panels)
        app.router.add_get("/api/panel/{display_id}", self._api_panel_get)

        # Terminal
        app.router.add_get("/api/term", self._api_term_list)
        app.router.add_post("/api/term", self._api_term_create)
        app.router.add_delete("/api/term/{sid}", self._api_term_kill)
        app.router.add_post("/api/term/{sid}/input", self._api_term_input)
        app.router.add_get("/api/term/{sid}/output", self._api_term_output)
        app.router.add_post("/api/term/{sid}/owner", self._api_term_owner)

        # Files
        app.router.add_get("/api/fs/list", self._api_fs_list)
        app.router.add_get("/api/fs/read", self._api_fs_read)
        app.router.add_put("/api/fs/write", self._api_fs_write)
        app.router.add_get("/api/fs/raw", self._api_fs_raw)
        app.router.add_post("/api/fs/upload", self._api_fs_upload)
        app.router.add_get("/api/fs/zip", self._api_fs_zip)
        app.router.add_get("/api/fs/roots", self._api_fs_roots)

        # Browser
        app.router.add_post("/api/browser-action", self._api_browser_action)

        # Apps
        app.router.add_get("/api/apps", self._api_apps_list)
        app.router.add_get("/api/app/{app_id}", self._api_app_get)
        app.router.add_post("/api/app/{app_id}", self._api_app_save)
        app.router.add_delete("/api/app/{app_id}", self._api_app_delete)
        app.router.add_post("/api/app/{app_id}/run", self._api_app_run)

        # Static
        if _STATIC_DIR.is_dir():
            app.router.add_static("/static", str(_STATIC_DIR))

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _on_startup(self, app: web.Application) -> None:
        self._loop = asyncio.get_event_loop()
        self.task.set_loop(self._loop)

    async def _on_cleanup(self, app: web.Application) -> None:
        self.terminal.cleanup()
        self.browser.shutdown()

    # ── Start / stop ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the web server (async)."""
        if self._runner is not None:
            return
        app = self.create_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

    async def stop(self) -> None:
        """Stop the web server."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    def start_in_thread(self) -> None:
        """Start the server in a background daemon thread.

        Use this when the agent runs in a synchronous context — the server
        gets its own event loop in the thread.
        """
        import threading

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.start())
            try:
                loop.run_forever()
            finally:
                loop.run_until_complete(self.stop())
                loop.close()

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    # ══ WebSocket handlers ════════════════════════════════════════════════

    async def _ws_render_handler(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket for render panel state stream."""
        return await self.render.ws_handler(request)

    async def _ws_term_handler(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket for interactive terminal (per-client PTY, two-way)."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        sess = None

        # Send session list on connect
        await ws.send_str(json.dumps({
            "type": "sessions",
            "sessions": self.terminal.list_sessions(),
        }))

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type == WSMsgType.ERROR:
                        break
                    continue
                try:
                    data = json.loads(msg.data)
                except (json.JSONDecodeError, ValueError):
                    continue

                msg_type = data.get("type")

                if msg_type == "start":
                    if sess is not None:
                        continue
                    sid = data.get("id")
                    existing = self.terminal.get_session(sid) if sid else None
                    if existing:
                        sess = existing
                    else:
                        result = self.terminal.create_session(
                            title=data.get("title", "Terminal"),
                            cols=data.get("cols", 80),
                            rows=data.get("rows", 24),
                        )
                        if "error" in result:
                            await ws.send_str(json.dumps({
                                "type": "output",
                                "data": f"\r\n\x1b[31mCannot start shell: {result['error']}\x1b[0m\r\n",
                            }))
                            await ws.close()
                            return ws
                        sess = self.terminal.get_session(result["id"])
                    if sess:
                        await ws.send_str(json.dumps({
                            "type": "session",
                            "id": sess.id,
                            "title": sess.title,
                            "owner": sess.owner,
                        }))
                        # Replay buffered output
                        if sess.buf:
                            await ws.send_str(json.dumps({"type": "output", "data": sess.buf}))

                elif msg_type == "input":
                    if sess and sess.alive:
                        self.terminal.write_input(sess, data.get("data", ""))

                elif msg_type == "resize":
                    if sess and sess.alive:
                        self.terminal.resize(sess, data.get("cols", 80), data.get("rows", 24))

                elif msg_type == "kill":
                    if sess:
                        self.terminal.kill_session(sess.id)
                        sess = None

                elif msg_type == "ping":
                    await ws.send_str(json.dumps({"type": "pong"}))

        finally:
            pass  # Detach only; session persists

        return ws

    # ══ Render API ════════════════════════════════════════════════════════

    async def _api_show(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        result = await self.render.show_async(
            content=body.get("content", ""),
            content_type=body.get("content_type", "html"),
            display_id=body.get("display_id", "default"),
            title=body.get("title", ""),
            mode=body.get("mode", "replace"),
        )
        return web.json_response(result)

    async def _api_notify(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        result = await self.render.notify_async(
            message=body.get("message", ""),
            display_id=body.get("display_id", "*"),
        )
        return web.json_response(result)

    async def _api_panels(self, request: web.Request) -> web.Response:
        return web.json_response({"panels": self.render.list_panels()})

    async def _api_panel_get(self, request: web.Request) -> web.Response:
        display_id = request.match_info["display_id"]
        panel = self.render.get_panel(display_id)
        if panel is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(panel)

    # ══ Terminal API ══════════════════════════════════════════════════════

    async def _api_term_list(self, request: web.Request) -> web.Response:
        return web.json_response({"sessions": self.terminal.list_sessions()})

    async def _api_term_create(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        result = self.terminal.create_session(
            title=body.get("title", "Terminal"),
            cols=body.get("cols", 80),
            rows=body.get("rows", 24),
            cwd=body.get("cwd", ""),
        )
        if "error" in result:
            return web.json_response(result, status=503)
        return web.json_response(result)

    async def _api_term_kill(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        ok = self.terminal.kill_session(sid)
        return web.json_response({"ok": ok})

    async def _api_term_input(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        sess = self.terminal.get_session(sid)
        if sess is None:
            return web.json_response({"error": "no such session"}, status=404)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        result = self.terminal.write_input(sess, body.get("data", ""))
        if "error" in result:
            return web.json_response(result, status=500)
        return web.json_response(result)

    async def _api_term_output(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        sess = self.terminal.get_session(sid)
        if sess is None:
            return web.json_response({"error": "no such session"}, status=404)
        since = int(request.query.get("since", 0))
        result = self.terminal.read_since(sess, since)
        # Strip ANSI for API consumers
        result["data"] = _strip_ansi(result.get("data", ""))
        return web.json_response(result)

    async def _api_term_owner(self, request: web.Request) -> web.Response:
        sid = request.match_info["sid"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        ok = self.terminal.set_owner(sid, body.get("who", "user"))
        if not ok:
            return web.json_response({"error": "no such session"}, status=404)
        return web.json_response({"ok": True})

    # ══ Files API ═════════════════════════════════════════════════════════

    async def _api_fs_list(self, request: web.Request) -> web.Response:
        dir_path = request.query.get("path")
        if not dir_path:
            return web.json_response({"error": "path required"}, status=400)
        return web.json_response(list_dir(dir_path))

    async def _api_fs_read(self, request: web.Request) -> web.Response:
        file_path = request.query.get("path")
        if not file_path:
            return web.json_response({"error": "path required"}, status=400)
        return web.json_response(read_file(file_path))

    async def _api_fs_write(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        file_path = body.get("path")
        if not file_path:
            return web.json_response({"error": "path required"}, status=400)
        return web.json_response(write_file(file_path, body.get("content", "")))

    async def _api_fs_raw(self, request: web.Request) -> web.Response:
        file_path = request.query.get("path")
        if not file_path:
            return web.Response(text="path required", status=400)
        p = Path(file_path)
        if not p.is_file():
            return web.Response(text="not found", status=404)
        try:
            return web.FileResponse(p, headers={"Content-Type": guess_mime(file_path)})
        except Exception as e:
            return web.Response(text=str(e), status=500)

    async def _api_fs_upload(self, request: web.Request) -> web.Response:
        dest_dir = request.query.get("dir")
        rel = request.query.get("rel")
        if not dest_dir or not rel:
            return web.json_response({"error": "dir and rel required"}, status=400)
        data = await request.read()
        return web.json_response(upload_file(dest_dir, rel, data))

    async def _api_fs_zip(self, request: web.Request) -> web.Response:
        dir_path = request.query.get("path")
        if not dir_path:
            return web.Response(text="path required", status=400)
        try:
            zip_bytes, fname = zip_dir(dir_path)
        except (NotADirectoryError, OSError) as e:
            return web.Response(text=str(e), status=400)
        return web.Response(
            body=zip_bytes,
            content_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    async def _api_fs_roots(self, request: web.Request) -> web.Response:
        return web.json_response(get_roots())

    # ══ Browser API ═══════════════════════════════════════════════════════

    async def _api_browser_action(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        result = self.browser.perform_action(
            action=body.get("action", ""),
            payload=body.get("payload", {}),
            display_id=body.get("display_id", "default"),
        )
        return web.json_response(result)

    # ══ Apps API ══════════════════════════════════════════════════════════

    async def _api_apps_list(self, request: web.Request) -> web.Response:
        return web.json_response(apps_mod.list_apps())

    async def _api_app_get(self, request: web.Request) -> web.Response:
        app_id = request.match_info["app_id"]
        result = apps_mod.get_app(app_id)
        if result is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(result)

    async def _api_app_save(self, request: web.Request) -> web.Response:
        app_id = request.match_info["app_id"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        result = apps_mod.register_app(
            name=body.get("name", app_id),
            description=body.get("description", ""),
            runtime=body.get("runtime", "shell"),
            script=body.get("script", ""),
            icon=body.get("icon", "📦"),
        )
        if "error" in result:
            return web.json_response(result, status=400)
        return web.json_response(result)

    async def _api_app_delete(self, request: web.Request) -> web.Response:
        app_id = request.match_info["app_id"]
        result = apps_mod.delete_app(app_id)
        if "error" in result:
            return web.json_response(result, status=404)
        return web.json_response(result)

    # ══ Task API ═════════════════════════════════════════════════════════

    async def _api_task_start(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        text = body.get("text", "").strip()
        timeout_ms = int(body.get("timeout_ms", 300000))
        result = await self.task.start_task(text, timeout_ms=timeout_ms)
        if not result.get("ok"):
            return web.json_response(result, status=409)
        return web.json_response(result)

    async def _api_task_abort(self, request: web.Request) -> web.Response:
        result = await self.task.abort_task()
        if not result.get("ok"):
            return web.json_response(result, status=409)
        return web.json_response(result)

    async def _api_task_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.task.status())

    async def _api_app_run(self, request: web.Request) -> web.Response:
        app_id = request.match_info["app_id"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        run_args = body.get("args")
        result = apps_mod.run_app(app_id, run_args)
        if "error" in result:
            return web.json_response(result, status=500)
        return web.json_response(result)

    # ══ Static ════════════════════════════════════════════════════════════

    async def _root_handler(self, request: web.Request) -> web.StreamResponse:
        """Root handler: WebSocket upgrade for dashboard state, else serve HTML."""
        # WebSocket upgrade headers
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.state_bridge.ws_handler(request)
        # Regular HTTP → serve dashboard
        index = _STATIC_DIR / "panel.html"
        if index.is_file():
            return web.FileResponse(index)

    async def _static_file_handler(self, request: web.Request) -> web.StreamResponse:
        """Serve static files from root path (e.g. /cm6-bundle.js, /ui_i18n.js)."""
        filename = request.match_info["filename"]
        filepath = _STATIC_DIR / filename
        if filepath.is_file():
            return web.FileResponse(filepath)
        return web.Response(text="404: Not Found", status=404)
