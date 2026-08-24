"""Tests for the senza-agent web server.

Covers render panels, terminal sessions, file browser, and apps modules.
Tests use aiohttp's test utilities and pytest-aiohttp.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# These tests import webserver modules directly — no senza SDK needed.
from senza_agent.webserver.render import RenderManager, DisplayPanel
from senza_agent.webserver.terminal import TerminalManager, _strip_ansi
from senza_agent.webserver import files as files_mod
from senza_agent.webserver import apps as apps_mod
from senza_agent.webserver.browser import BrowserController


# ══ RenderManager ═══════════════════════════════════════════════════════════

class TestRenderManager:
    def test_show_creates_panel(self):
        rm = RenderManager()
        result = rm.show("<h1>Hello</h1>", content_type="html", display_id="test")
        assert result["display_id"] == "test"
        assert result["content"] == "<h1>Hello</h1>"
        assert result["content_type"] == "html"

    def test_show_replace_mode(self):
        rm = RenderManager()
        rm.show("first", display_id="d1")
        rm.show("second", display_id="d1", mode="replace")
        panel = rm.get_panel("d1")
        assert panel["content"] == "second"

    def test_show_append_mode(self):
        rm = RenderManager()
        rm.show("line1", display_id="d1")
        rm.show("line2", display_id="d1", mode="append")
        panel = rm.get_panel("d1")
        assert panel["content"] == "line1\nline2"

    def test_notify_stores_message(self):
        rm = RenderManager()
        record = rm.notify("hello world")
        assert record["message"] == "hello world"
        assert record["role"] == "agent"
        log = rm.get_chat_log()
        assert len(log) == 1
        assert log[0]["message"] == "hello world"

    def test_notify_chat_log_since(self):
        rm = RenderManager()
        rec1 = rm.notify("msg1")
        log = rm.get_chat_log(since=rec1["ts"])
        assert len(log) == 0
        rec2 = rm.notify("msg2")
        log = rm.get_chat_log(since=rec1["ts"])
        assert len(log) == 1
        assert log[0]["message"] == "msg2"

    def test_list_panels(self):
        rm = RenderManager()
        rm.show("a", display_id="p1")
        rm.show("b", display_id="p2")
        panels = rm.list_panels()
        assert len(panels) == 2
        ids = {p["display_id"] for p in panels}
        assert ids == {"p1", "p2"}

    def test_get_panel_not_found(self):
        rm = RenderManager()
        assert rm.get_panel("nonexistent") is None


# ══ TerminalManager ═════════════════════════════════════════════════════════

class TestTerminalManager:
    def test_list_sessions_empty(self):
        tm = TerminalManager()
        assert tm.list_sessions() == []

    def test_create_session(self):
        tm = TerminalManager()
        result = tm.create_session(title="Test")
        # ptyprocess might not be installed in CI
        if "error" in result:
            pytest.skip(f"ptyprocess not available: {result['error']}")
        assert "id" in result
        assert result["title"] == "Test"
        sessions = tm.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["id"] == result["id"]

    def test_kill_session(self):
        tm = TerminalManager()
        result = tm.create_session()
        if "error" in result:
            pytest.skip("ptyprocess not available")
        sid = result["id"]
        assert tm.kill_session(sid) is True
        assert tm.get_session(sid) is None
        # Killing again returns False
        assert tm.kill_session(sid) is False

    def test_set_owner(self):
        tm = TerminalManager()
        result = tm.create_session()
        if "error" in result:
            pytest.skip("ptyprocess not available")
        sid = result["id"]
        assert tm.set_owner(sid, "agent") is True
        sess = tm.get_session(sid)
        assert sess.owner == "agent"
        assert tm.set_owner("nonexistent", "agent") is False


def test_strip_ansi_removes_escape_codes():
    raw = "\x1b[31mred text\x1b[0m\r\nclean"
    clean = _strip_ansi(raw)
    assert "\x1b" not in clean
    assert "red text" in clean
    assert "\r" not in clean


def test_strip_ansi_empty():
    assert _strip_ansi("") == ""
    assert _strip_ansi(None) == ""


# ══ Files module ════════════════════════════════════════════════════════════

class TestFilesModule:
    def test_list_dir(self, tmp_path):
        (tmp_path / "file1.txt").write_text("hello")
        (tmp_path / "subdir").mkdir()
        result = files_mod.list_dir(str(tmp_path))
        assert "error" not in result
        names = {f["name"] for f in result["files"]}
        assert "file1.txt" in names
        assert "subdir" in names
        # Dirs first
        types = [f["type"] for f in result["files"]]
        assert types.index("dir") < types.index("file")

    def test_list_dir_not_found(self):
        result = files_mod.list_dir("/nonexistent/path/xyz")
        assert "error" in result

    def test_read_file_text(self, tmp_path):
        fp = tmp_path / "test.txt"
        fp.write_text("hello world")
        result = files_mod.read_file(str(fp))
        assert result["binary"] is False
        assert result["content"] == "hello world"
        assert result["truncated"] is False

    def test_read_file_binary(self, tmp_path):
        fp = tmp_path / "binary.bin"
        fp.write_bytes(b"\x00\x01\x02\x03")
        result = files_mod.read_file(str(fp))
        assert result["binary"] is True
        assert result["content"] is None

    def test_read_file_truncated(self, tmp_path):
        fp = tmp_path / "big.txt"
        fp.write_text("x" * (files_mod._MAX_PREVIEW_BYTES + 100))
        result = files_mod.read_file(str(fp))
        assert result["truncated"] is True
        assert len(result["content"]) == files_mod._MAX_PREVIEW_BYTES

    def test_write_file(self, tmp_path):
        fp = tmp_path / "output.txt"
        result = files_mod.write_file(str(fp), "written content")
        assert result["ok"] is True
        assert fp.read_text() == "written content"

    def test_write_file_creates_parent(self, tmp_path):
        fp = tmp_path / "subdir" / "output.txt"
        result = files_mod.write_file(str(fp), "nested")
        assert result["ok"] is True
        assert fp.read_text() == "nested"

    def test_upload_file(self, tmp_path):
        result = files_mod.upload_file(str(tmp_path), "uploaded.txt", b"file data")
        assert result["ok"] is True
        assert (tmp_path / "uploaded.txt").read_bytes() == b"file data"

    def test_upload_file_nested(self, tmp_path):
        result = files_mod.upload_file(str(tmp_path), "sub/deep/file.txt", b"nested")
        assert result["ok"] is True
        assert (tmp_path / "sub" / "deep" / "file.txt").read_bytes() == b"nested"

    def test_upload_file_traversal_blocked(self, tmp_path):
        result = files_mod.upload_file(str(tmp_path), "../../etc/passwd", b"hax")
        assert "error" in result

    def test_zip_dir(self, tmp_path):
        (tmp_path / "a.txt").write_text("aaa")
        (tmp_path / "b.txt").write_text("bbb")
        data, fname = files_mod.zip_dir(str(tmp_path))
        assert fname.endswith(".zip")
        import zipfile
        import io
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = set(zf.namelist())
        assert "a.txt" in names
        assert "b.txt" in names

    def test_get_roots(self):
        result = files_mod.get_roots()
        assert "roots" in result
        assert "home" in result
        assert "cwd" in result
        assert "sep" in result

    def test_guess_mime(self):
        assert files_mod.guess_mime("file.html").startswith("text/html")
        assert files_mod.guess_mime("file.json") == "application/json"
        assert files_mod.guess_mime("file.png") == "image/png"
        assert files_mod.guess_mime("file.unknown") == "application/octet-stream"


# ══ Apps module ═════════════════════════════════════════════════════════════

class TestAppsModule:
    def test_parse_app_file_basic(self):
        content = (
            "---\n"
            'name: "My App"\n'
            "runtime: python\n"
            "description: " + '"A test app"\n' +
            "enabled: true\n"
            "---\n\n"
            "print('hello')\n"
        )
        meta, body = apps_mod.parse_app_file(content)
        assert meta["name"] == "My App"
        assert meta["runtime"] == "python"
        assert meta["description"] == "A test app"
        assert meta["enabled"] is True
        assert body == "print('hello')\n"

    def test_parse_app_file_with_fence(self):
        content = (
            "---\n"
            "name: test\n"
            "runtime: shell\n"
            "---\n\n"
            "```python\n"
            "print(1)\n"
            "```\n"
        )
        meta, body = apps_mod.parse_app_file(content)
        assert meta["runtime"] == "python"
        assert "print(1)" in body

    def test_parse_app_file_no_frontmatter(self):
        meta, body = apps_mod.parse_app_file("echo hello")
        assert meta["runtime"] == "shell"
        assert body == "echo hello"

    def test_parse_app_file_disabled(self):
        content = (
            "---\n"
            "name: test\n"
            "enabled: false\n"
            "---\n\n"
            "echo hi\n"
        )
        meta, _ = apps_mod.parse_app_file(content)
        assert meta["enabled"] is False

    def test_register_and_list_app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        result = apps_mod.register_app(
            name="TestApp",
            description="A test",
            runtime="python",
            script="print('hi')",
        )
        assert "error" not in result
        assert result["id"] == "TestApp"
        assert (tmp_path / "TestApp.md").is_file()

        listing = apps_mod.list_apps()
        assert len(listing["apps"]) == 1
        assert listing["apps"][0]["name"] == "TestApp"
        assert listing["apps"][0]["runtime"] == "python"

    def test_get_app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        apps_mod.register_app("MyApp", "desc", "shell", "echo hi")
        result = apps_mod.get_app("MyApp")
        assert result is not None
        assert "content" in result
        assert "echo hi" in result["content"]
        assert apps_mod.get_app("nonexistent") is None

    def test_delete_app(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        apps_mod.register_app("ToDelete", "desc", "shell", "echo hi")
        result = apps_mod.delete_app("ToDelete")
        assert result.get("ok") is True
        assert not (tmp_path / "ToDelete.md").exists()
        # Delete again → error
        result2 = apps_mod.delete_app("ToDelete")
        assert "error" in result2

    def test_register_app_invalid_runtime(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        result = apps_mod.register_app("Bad", "desc", "ruby", "puts 1")
        assert "error" in result

    def test_run_app_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        result = apps_mod.run_app("nonexistent")
        assert "error" in result

    def test_run_app_python(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        apps_mod.register_app("PyApp", "test", "python", "print(42)")
        result = apps_mod.run_app("PyApp")
        assert result["ok"] is True
        assert "42" in result["stdout"]

    def test_run_app_shell(self, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        apps_mod.register_app("ShApp", "test", "shell", "echo hello123")
        result = apps_mod.run_app("ShApp")
        assert result["ok"] is True
        assert "hello123" in result["stdout"]


# ══ BrowserController ═══════════════════════════════════════════════════════

class TestBrowserController:
    def test_perform_action_unknown(self):
        bc = BrowserController()
        result = bc.perform_action("unknown_action", {})
        assert "error" in result

    def test_perform_action_no_playwright(self):
        bc = BrowserController()
        # Force the error state
        bc._error = "playwright not installed"
        result = bc.perform_action("screenshot", {})
        assert "error" in result


# ══ Integration: aiohttp test server ════════════════════════════════════════

class TestWebServerRoutes:
    """Integration tests using aiohttp TestClient."""

    async def _make_server(self):
        from senza_agent.webserver.app import WebServer
        ws = WebServer(port=0)  # port 0 = ephemeral for testing
        app = ws.create_app()
        return ws, app

    async def test_api_show_and_get_panel(self, aiohttp_client):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)

        # Show content
        resp = await client.post("/api/show", json={
            "content": "<h1>Test</h1>",
            "content_type": "html",
            "display_id": "itest",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["display_id"] == "itest"

        # Get panel
        resp = await client.get("/api/panel/itest")
        assert resp.status == 200
        data = await resp.json()
        assert data["content"] == "<h1>Test</h1>"

    async def test_api_notify(self, aiohttp_client):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)

        resp = await client.post("/api/notify", json={
            "message": "test notification",
        })
        assert resp.status == 200
        data = await resp.json()
        assert data["message"] == "test notification"

    async def test_api_panels_list(self, aiohttp_client):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)

        await client.post("/api/show", json={
            "content": "a", "display_id": "p1",
        })
        await client.post("/api/show", json={
            "content": "b", "display_id": "p2",
        })
        resp = await client.get("/api/panels")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["panels"]) == 2

    async def test_api_fs_list(self, aiohttp_client, tmp_path):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)
        (tmp_path / "file.txt").write_text("x")

        resp = await client.get("/api/fs/list", params={"path": str(tmp_path)})
        assert resp.status == 200
        data = await resp.json()
        names = {f["name"] for f in data["files"]}
        assert "file.txt" in names

    async def test_api_fs_read(self, aiohttp_client, tmp_path):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)
        fp = tmp_path / "test.txt"
        fp.write_text("hello")

        resp = await client.get("/api/fs/read", params={"path": str(fp)})
        assert resp.status == 200
        data = await resp.json()
        assert data["content"] == "hello"

    async def test_api_fs_write(self, aiohttp_client, tmp_path):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)
        fp = tmp_path / "written.txt"

        resp = await client.put("/api/fs/write", json={
            "path": str(fp),
            "content": "written",
        })
        assert resp.status == 200
        assert fp.read_text() == "written"

    async def test_api_fs_roots(self, aiohttp_client):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)
        resp = await client.get("/api/fs/roots")
        assert resp.status == 200
        data = await resp.json()
        assert "home" in data

    async def test_api_apps_list_empty(self, aiohttp_client, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        ws, app = await self._make_server()
        client = await aiohttp_client(app)
        resp = await client.get("/api/apps")
        assert resp.status == 200
        data = await resp.json()
        assert data["apps"] == []

    async def test_api_app_register_and_run(self, aiohttp_client, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        ws, app = await self._make_server()
        client = await aiohttp_client(app)

        # Register
        resp = await client.post("/api/app/myapp", json={
            "name": "myapp",
            "description": "test",
            "runtime": "python",
            "script": "print(123)",
        })
        assert resp.status == 200

        # List
        resp = await client.get("/api/apps")
        data = await resp.json()
        assert len(data["apps"]) == 1
        assert data["apps"][0]["name"] == "myapp"

        # Run
        resp = await client.post("/api/app/myapp/run", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert "123" in data["stdout"]

    async def test_api_app_delete(self, aiohttp_client, tmp_path, monkeypatch):
        monkeypatch.setenv("APPS_DIR", str(tmp_path))
        ws, app = await self._make_server()
        client = await aiohttp_client(app)

        await client.post("/api/app/delapp", json={
            "name": "delapp",
            "description": "",
            "runtime": "shell",
            "script": "echo hi",
        })
        resp = await client.delete("/api/app/delapp")
        assert resp.status == 200

        # Verify gone
        resp = await client.get("/api/app/delapp")
        assert resp.status == 404

    async def test_api_term_list_empty(self, aiohttp_client):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)
        resp = await client.get("/api/term")
        assert resp.status == 200
        data = await resp.json()
        assert data["sessions"] == []

    async def test_index_page(self, aiohttp_client):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)
        resp = await client.get("/")
        assert resp.status == 200

    async def test_api_browser_action_no_playwright(self, aiohttp_client):
        ws, app = await self._make_server()
        client = await aiohttp_client(app)
        resp = await client.post("/api/browser-action", json={
            "action": "screenshot",
            "payload": {},
        })
        assert resp.status == 200
        data = await resp.json()
        # playwright not installed in test env → error
        assert "error" in data or "ok" in data
