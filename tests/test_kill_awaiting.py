"""Kill while paused must not leave the dashboard input dead.

``meta.awaiting_input`` is only cleared by an ``ask_user`` tool_result. An
aborted run never emits one, so the stale flag made ``/api/inject`` reject
every follow-up with "Agent is preparing a question, please wait a moment
and retry." — the command bar looked normal but typed input went nowhere.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from senza_agent.webserver.qevos_bridge import QevosAPI, StateBridge
from senza_agent.webserver.task import TaskManager


@pytest.fixture
def api():
    tm = TaskManager()
    tm.set_harness(MagicMock())
    sb = StateBridge(tm, MagicMock())
    sb._state["meta"] = {"awaiting_input": "Which file?"}
    sb._state["status"] = {"status": "paused"}
    sb._state["agentAlive"] = True
    sb.broadcast = AsyncMock()
    sb.on_followup = AsyncMock()
    return QevosAPI(sb, tm), sb, tm


def _client_factory(paths):
    from aiohttp.test_utils import TestClient, TestServer

    def _make(api):
        app = web.Application()
        for route in paths:
            app.router.add_post(route, getattr(api, "_api_" + route.rsplit("/", 1)[-1]))
        return TestClient(TestServer(app))

    async def _client(api):
        client = _make(api)
        await client.start_server()
        return client
    return _client


class TestKillClearsPausedState:
    async def test_api_kill_drops_stale_awaiting_flag(self, api):
        api, sb, tm = api
        tm.abort_task = AsyncMock(return_value={"ok": True})
        client = await _client_factory(["/api/kill"])(api)
        try:
            r = await client.post("/api/kill")
            assert r.status == 200
            assert (await r.json()) == {"ok": True}
        finally:
            await client.close()
        assert "awaiting_input" not in sb.state["meta"]
        assert sb.state["status"] == {"status": "idle"}
        sb.broadcast.assert_awaited()


class TestInjectAfterDeadRun:
    async def test_stale_flag_does_not_reject_followup(self, api):
        api, sb, tm = api
        started = {}

        async def fake_start(text, timeout_ms=300000, attachments=None):
            started["text"] = text
            return {"ok": True}

        tm.start_task = fake_start
        assert tm.is_running is False
        client = await _client_factory(["/api/inject"])(api)
        try:
            r = await client.post("/api/inject", json={"command": "/inject new goal"})
            body = await r.json()
        finally:
            await client.close()
        assert body == {"ok": True}
        assert started["text"] == "new goal"
        sb.on_followup.assert_awaited_once_with("new goal")
        assert "awaiting_input" not in sb.state["meta"]


class TestTaskEndClearsPausedState:
    async def test_on_task_end_drops_awaiting_flag(self):
        sb = StateBridge(MagicMock(), MagicMock())
        sb._state["meta"] = {"goal": "g", "awaiting_input": "Which file?"}
        sb._state["activeRunId"] = None
        sb.broadcast = AsyncMock()
        sb._refresh_runs = MagicMock()
        await sb.on_task_end({})
        assert "awaiting_input" not in sb._state["meta"]
        assert sb._state["meta"]["goal"] == "g"
        assert sb._state["status"] == {"status": "idle"}


# ══ Kill must actually stop the run before the next message arrives ═════════


class TestKillThenRelaunch:
    async def test_start_right_after_kill_is_accepted(self, monkeypatch):
        """``/api/kill`` must not leave ``_running`` True for the next goal."""
        import senza

        stop = threading.Event()

        class Harness:
            def prompt(self, text, attachments=None):
                stop.wait(5)

            def abort(self):
                stop.set()

            def get_messages(self):
                return []

        def fake_stream(harness, **kwargs):
            async def gen():
                return
                yield  # pragma: no cover

            return gen()

        monkeypatch.setattr(senza, "stream_events", fake_stream)
        tm = TaskManager()
        tm.set_harness(Harness())
        assert (await tm.start_task("first"))["ok"]
        assert tm.is_running

        result = await tm.abort_task(wait_timeout=5)

        assert result == {"ok": True, "stopped": True}
        assert tm.is_running is False
        assert (await tm.start_task("second"))["ok"] is True
        await asyncio.sleep(0.2)


class TestLaunchWhileStopping:
    async def test_launch_rejected_without_resetting_bridge_state(self, api):
        api, sb, tm = api
        tm._running = True
        sb.on_task_start = AsyncMock()
        client = await _client_factory(["/api/launch"])(api)
        try:
            r = await client.post("/api/launch", json={"goal": "next goal"})
            body = await r.json()
        finally:
            await client.close()
        assert r.status == 409
        assert body["ok"] is False
        sb.on_task_start.assert_not_awaited()


class TestInjectWhileStopping:
    async def test_running_task_is_not_treated_as_followup(self, api):
        api, sb, tm = api
        sb._state["meta"].pop("awaiting_input")   # not a pause, just still stopping
        sb._state["status"] = {"status": "running"}
        tm._running = True
        client = await _client_factory(["/api/inject"])(api)
        try:
            r = await client.post("/api/inject", json={"command": "/inject next"})
            body = await r.json()
        finally:
            await client.close()
        assert body["ok"] is False
        sb.on_followup.assert_not_awaited()
