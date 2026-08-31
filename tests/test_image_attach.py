"""Tests for Qevos-style image recognition plumbing (senza-sdk >= 1.3.0).

Covers:
  - tool_load_image / tool_load_video returning mixed [caption, Attachment] lists
  - _sdk_compat.stream_prompt forwarding attachments to obj.prompt
  - TaskManager.start_task/_run_task attachment passthrough
  - _api_inject_image endpoint (idle → start_task, running → steer, bad payload → 400)
  - _attachment_from_payload parsing (data URL, raw base64, url, path, rejects)
"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from senza_agent.webserver.qevos_bridge import (
    QevosAPI,
    StateBridge,
    _attachment_from_payload,
)

# 1x1 transparent PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_B64 = base64.b64encode(_PNG).decode()


# ══ _attachment_from_payload ═══════════════════════════════════════════════


class TestAttachmentFromPayload:
    def test_data_url_with_mime(self):
        att = _attachment_from_payload({"image": f"data:image/webp;base64,{_PNG_B64}"})
        assert repr(att).startswith("Attachment(image_base64")
        assert "image/webp" in repr(att)

    def test_data_url_defaults_to_png(self):
        att = _attachment_from_payload({"image": f"data:;base64,{_PNG_B64}"})
        assert "image/png" in repr(att)

    def test_raw_base64(self):
        att = _attachment_from_payload({"image": _PNG_B64})
        assert "image/png" in repr(att)

    def test_url_passthrough(self):
        att = _attachment_from_payload({"url": "https://example.com/x.jpg"})
        assert "image_url" in repr(att)

    def test_local_path(self, tmp_path):
        p = tmp_path / "pic.png"
        p.write_bytes(_PNG)
        att = _attachment_from_payload({"path": str(p)})
        assert "image_base64" in repr(att)

    def test_relative_path_resolves_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rel.png").write_bytes(_PNG)
        att = _attachment_from_payload({"path": "rel.png"})
        assert "image_base64" in repr(att)

    @pytest.mark.parametrize("body", [
        {"image": "not!!base64!!"},
        {"url": "ftp://example.com/x"},
        {"path": "/definitely/not/here.png"},
        {},
    ])
    def test_rejects(self, body):
        with pytest.raises(ValueError):
            _attachment_from_payload(body)


# ══ tool_load_image / tool_load_video ══════════════════════════════════════


class TestToolLoadImage:
    def test_local_file_returns_caption_and_attachment(self, tmp_path):
        from senza_agent.tools.standard import tool_load_image
        p = tmp_path / "dot.png"
        p.write_bytes(_PNG)
        result = tool_load_image(str(p), caption="a dot")
        assert isinstance(result, list)
        assert result[0] == "a dot"
        assert "image_base64" in repr(result[-1])

    def test_no_caption_single_attachment(self, tmp_path):
        from senza_agent.tools.standard import tool_load_image
        p = tmp_path / "dot.png"
        p.write_bytes(_PNG)
        result = tool_load_image(str(p))
        assert len(result) == 1
        assert "image_base64" in repr(result[0])

    def test_empty_path_is_error_dict(self):
        from senza_agent.tools.standard import tool_load_image
        out = tool_load_image("")
        assert out["status"] == "error"

    def test_missing_file_is_error_dict(self):
        from senza_agent.tools.standard import tool_load_image
        out = tool_load_image("/no/such/file.png")
        assert out["status"] == "error"

    def test_vision_unsupported_rejects(self, monkeypatch, tmp_path):
        import senza_agent.tools.standard as std
        p = tmp_path / "dot.png"
        p.write_bytes(_PNG)
        monkeypatch.setattr(std._state, "vision_supported", False)
        out = std.tool_load_image(str(p))
        assert out["status"] == "error"
        assert "multimodal" in out["error"]


class TestToolLoadVideo:
    def test_missing_dependency_is_error_dict(self, monkeypatch):
        import senza_agent.tools.standard as std
        monkeypatch.setitem(__import__("sys").modules, "cv2", None)
        out = std.tool_load_video("/nonexistent.mp4")
        assert out["status"] == "error"

    def test_missing_file_is_error_dict(self, monkeypatch):
        import senza_agent.tools.standard as std
        cv2_mock = MagicMock()
        monkeypatch.setitem(__import__("sys").modules, "cv2", cv2_mock)
        out = std.tool_load_video("/nonexistent.mp4")
        assert out["status"] == "error"


# ══ _sdk_compat.stream_prompt attachments ══════════════════════════════════


class TestStreamPromptAttachments:
    @pytest.fixture
    def fake_harness(self):
        """Harness whose events() yields one agent_end event, sync iterator."""
        h = MagicMock()
        h.events = MagicMock(return_value=iter([{"type": "agent_end"}]))
        h.prompt = MagicMock(return_value=None)
        return h

    async def test_forwards_attachments_positionally(self, fake_harness):
        from senza_agent._sdk_compat import stream_prompt
        att = _attachment_from_payload({"image": _PNG_B64})
        events = []
        async for ev in stream_prompt(fake_harness, "hi", attachments=[att]):
            events.append(ev)
        assert events[-1]["type"] == "agent_end"
        args, kwargs = fake_harness.prompt.call_args
        assert args[0] == "hi"
        assert args[1] == [att]

    async def test_none_attachments_still_passed(self, fake_harness):
        from senza_agent._sdk_compat import stream_prompt
        events = []
        async for ev in stream_prompt(fake_harness, "hi"):
            events.append(ev)
        args, _ = fake_harness.prompt.call_args
        assert args[1] is None


# ══ TaskManager plumbing ═══════════════════════════════════════════════════


class TestTaskManagerAttachments:
    async def test_start_task_forwards_attachments_to_run(self, monkeypatch):
        from senza_agent.webserver.task import TaskManager
        tm = TaskManager()
        tm.set_harness(MagicMock())
        captured = {}

        async def fake_run(text, timeout_ms, attachments=None):
            captured["text"] = text
            captured["attachments"] = attachments

        monkeypatch.setattr(tm, "_run_task", fake_run)
        att = _attachment_from_payload({"image": _PNG_B64})
        res = await tm.start_task("look", attachments=[att])
        assert res["ok"] is True
        # ensure_future schedules; yield once so fake_run records.
        import asyncio
        await asyncio.sleep(0)
        assert captured["attachments"] == [att]

    async def test_run_task_passes_attachments_to_stream_prompt(self, monkeypatch):
        from senza_agent.webserver.task import TaskManager
        import senza_agent.webserver.task as task_mod
        tm = TaskManager()
        tm.set_harness(MagicMock())
        got = {}

        def fake_stream(harness, text, timeout_ms=0, max_consecutive_timeouts=0, attachments=None):
            got["attachments"] = attachments

            async def _gen():
                yield {"type": "agent_end"}
            return _gen()

        monkeypatch.setattr(
            task_mod, "stream_prompt", fake_stream, raising=False)
        import senza_agent._sdk_compat as compat
        monkeypatch.setattr(compat, "stream_prompt", fake_stream)
        att = _attachment_from_payload({"image": _PNG_B64})
        await tm._run_task("look", 1000, [att])
        assert got["attachments"] == [att]


# ══ /api/inject-image endpoint ═════════════════════════════════════════════


@pytest.fixture
def api():
    """QevosAPI with an idle task manager and a mocked state bridge."""
    sb = MagicMock(spec=StateBridge)
    sb.state = {"meta": {}}
    sb.on_task_start = AsyncMock()
    from senza_agent.webserver.task import TaskManager
    tm = TaskManager()
    tm.set_harness(MagicMock())
    tm._harness.steer = MagicMock()
    return QevosAPI(sb, tm), sb, tm


def _make_app(api):
    app = web.Application()
    app.router.add_post("/api/inject-image", api._api_inject_image)
    return app


@pytest.fixture
def aiohttp_client_factory():
    from aiohttp.test_utils import TestClient, TestServer

    async def _client(api):
        app = _make_app(api)
        client = TestClient(TestServer(app))
        await client.start_server()
        return client
    return _client


class TestInjectImageEndpoint:
    async def test_idle_starts_task_with_attachment(self, api, aiohttp_client_factory):
        api, sb, tm = api
        started = {}

        async def fake_start(text, timeout_ms=300000, attachments=None):
            started["text"] = text
            started["attachments"] = attachments
            return {"ok": True}

        tm.start_task = fake_start
        client = await aiohttp_client_factory(api)
        try:
            r = await client.post("/api/inject-image", json={
                "image": f"data:image/png;base64,{_PNG_B64}", "text": "what is this",
            })
            body = await r.json()
            assert r.status == 200 and body["ok"] is True
            assert started["text"] == "what is this"
            assert len(started["attachments"]) == 1
        finally:
            await client.close()

    async def test_running_steers(self, api, aiohttp_client_factory):
        api, sb, tm = api
        tm._running = True
        client = await aiohttp_client_factory(api)
        try:
            r = await client.post("/api/inject-image", json={
                "image": _PNG_B64, "text": "look again",
            })
            body = await r.json()
            assert body == {"ok": True, "mode": "steer"}
            args, kwargs = tm._harness.steer.call_args
            assert args[0] == "look again"
            assert len(kwargs["attachments"]) == 1
        finally:
            await client.close()

    async def test_bad_payload_400(self, api, aiohttp_client_factory):
        api, sb, tm = api
        client = await aiohttp_client_factory(api)
        try:
            r = await client.post("/api/inject-image", json={"image": "!!!"})
            assert r.status == 400
            body = await r.json()
            assert "base64" in body["error"]
        finally:
            await client.close()

    async def test_empty_body_400(self, api, aiohttp_client_factory):
        api, sb, tm = api
        client = await aiohttp_client_factory(api)
        try:
            r = await client.post("/api/inject-image", json={})
            assert r.status == 400
        finally:
            await client.close()

    async def test_awaiting_input_rejected(self, api, aiohttp_client_factory):
        api, sb, tm = api
        sb.state = {"meta": {"awaiting_input": True}}
        client = await aiohttp_client_factory(api)
        try:
            r = await client.post("/api/inject-image", json={"image": _PNG_B64})
            body = await r.json()
            assert body["ok"] is False
            assert "ask_user" in body["error"]
        finally:
            await client.close()

    async def test_running_with_old_sdk_typeerror(self, api, aiohttp_client_factory):
        api, sb, tm = api
        tm._running = True
        tm._harness.steer = MagicMock(side_effect=TypeError("unexpected keyword"))
        client = await aiohttp_client_factory(api)
        try:
            r = await client.post("/api/inject-image", json={"image": _PNG_B64})
            body = await r.json()
            assert body["ok"] is False
            assert "1.3.0" in body["error"]
        finally:
            await client.close()
