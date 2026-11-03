"""Tests for senza_agent.mcp_lifecycle.

These do not spawn real MCP servers: ``senza.McpManager`` is replaced with a
fake so the lifecycle logic can be exercised deterministically.
"""
import json
import sys

import pytest

from senza_agent import mcp_lifecycle


class FakeManager:
    """Minimal stand-in for senza.McpManager."""

    def __init__(self, statuses=None, errors=None):
        self.statuses = statuses or {}
        self._errors = errors or {}
        self.loaded = None
        self.disconnected = 0
        self.status_calls = 0

    def load_config_file(self, path):
        self.loaded = path

    def get_status(self, name):
        self.status_calls += 1
        return self.statuses.get(name, "connected")

    def errors(self):
        return self._errors

    def list_tools(self):
        return []

    def disconnect_all(self):
        self.disconnected += 1


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Keep module-level state isolated between tests."""
    monkeypatch.setattr(mcp_lifecycle, "_MANAGERS", [])
    monkeypatch.setattr(mcp_lifecycle, "_teardown_installed", False)
    monkeypatch.setattr(mcp_lifecycle, "_job_handle", None)
    # Never touch real signal handlers or job objects in tests.
    monkeypatch.setattr(mcp_lifecycle, "_install_kill_on_close_job", lambda: None)
    yield


def _write_config(tmp_path, servers):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return str(p)


def test_server_names_reads_config(tmp_path):
    cfg = _write_config(tmp_path, {"a": {}, "b": {}})
    assert mcp_lifecycle._server_names(cfg) == ["a", "b"]


def test_server_names_missing_file_is_empty():
    assert mcp_lifecycle._server_names("does-not-exist.json") == []


def test_create_manager_waits_until_ready(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path, {"srv": {}})
    fake = FakeManager(statuses={"srv": "connected"})
    monkeypatch.setattr(mcp_lifecycle.senza, "McpManager", lambda: fake)

    manager = mcp_lifecycle.create_manager(cfg, timeout=5)

    assert manager is fake
    assert fake.loaded == cfg
    assert fake.status_calls >= 1
    assert mcp_lifecycle._MANAGERS == [fake]


def test_create_manager_polls_pending_states(tmp_path, monkeypatch):
    """A server that starts 'connecting' must be polled until it settles."""
    cfg = _write_config(tmp_path, {"srv": {}})

    class Flaky(FakeManager):
        def get_status(self, name):
            self.status_calls += 1
            return "connecting" if self.status_calls < 3 else "connected"

    fake = Flaky()
    monkeypatch.setattr(mcp_lifecycle.senza, "McpManager", lambda: fake)
    monkeypatch.setattr(mcp_lifecycle.time, "sleep", lambda _s: None)

    mcp_lifecycle.create_manager(cfg, timeout=5)
    assert fake.status_calls == 3


def test_create_manager_reports_errors(tmp_path, monkeypatch, capsys):
    cfg = _write_config(tmp_path, {"srv": {}})
    fake = FakeManager(errors={"srv": "boom"})
    monkeypatch.setattr(mcp_lifecycle.senza, "McpManager", lambda: fake)

    mcp_lifecycle.create_manager(cfg, timeout=5)
    assert "boom" in capsys.readouterr().err


def test_disconnect_all_is_idempotent():
    a, b = FakeManager(), FakeManager()
    mcp_lifecycle._MANAGERS.extend([a, b])

    mcp_lifecycle.disconnect_all()
    assert a.disconnected == 1 and b.disconnected == 1
    assert mcp_lifecycle._MANAGERS == []

    mcp_lifecycle.disconnect_all()  # no-op, must not raise
    assert a.disconnected == 1


def test_disconnect_all_survives_manager_errors():
    class Boom(FakeManager):
        def disconnect_all(self):
            raise RuntimeError("nope")

    mcp_lifecycle._MANAGERS.append(Boom())
    mcp_lifecycle.disconnect_all()  # must not propagate
    assert mcp_lifecycle._MANAGERS == []


def test_install_teardown_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp_lifecycle.atexit, "register", lambda fn: calls.append(fn))

    mcp_lifecycle.install_teardown()
    mcp_lifecycle.install_teardown()

    assert calls == [mcp_lifecycle.disconnect_all]


def test_install_teardown_registers_atexit(monkeypatch):
    registered = []
    monkeypatch.setattr(mcp_lifecycle.atexit, "register", lambda fn: registered.append(fn))
    mcp_lifecycle.install_teardown()
    assert mcp_lifecycle.disconnect_all in registered
