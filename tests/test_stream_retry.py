"""Bounded retry for interrupted LLM streams (``stream incomplete``).

The runtime rolls a failed turn back, so re-prompting the same text is safe —
unless a tool already ran (history grew), in which case retrying would repeat
side effects. These tests pin that policy for the desktop runner, plus the
CLI-facing helpers.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from senza_agent.llm_retry import (
    DEFAULT_RETRIES,
    MAX_RETRIES,
    conversation_size,
    is_interrupted_stream,
    retry_budget,
    retry_delay_ms,
)

_CUT = "llm provider error: stream incomplete: 115 chunks received, finish_reason=None"


# ══ policy helpers ══════════════════════════════════════════════════════════


class TestPolicy:
    def test_default_budget(self, monkeypatch):
        monkeypatch.delenv("SENZA_AGENT_STREAM_RETRIES", raising=False)
        assert retry_budget() == DEFAULT_RETRIES

    @pytest.mark.parametrize("raw,expected", [
        ("0", 0), ("3", 3), ("99", MAX_RETRIES), ("-2", 0), ("junk", DEFAULT_RETRIES),
    ])
    def test_budget_from_env(self, monkeypatch, raw, expected):
        monkeypatch.setenv("SENZA_AGENT_STREAM_RETRIES", raw)
        assert retry_budget() == expected

    @pytest.mark.parametrize("message,expected", [
        (_CUT, True),
        ("LLM PROVIDER ERROR: STREAM INCOMPLETE: 3 chunks received", True),
        ("llm provider error: unauthorized", False),
        ("boom", False),
        ("", False),
    ])
    def test_marker_match(self, message, expected):
        assert is_interrupted_stream(message) is expected

    def test_backoff_is_bounded(self):
        assert [retry_delay_ms(i) for i in range(5)] == [1000, 2000, 4000, 8000, 8000]

    def test_conversation_size_unavailable(self):
        assert conversation_size(MagicMock()) is None  # non-int result
        assert conversation_size(object()) is None  # no message_count()


# ══ TaskManager ═════════════════════════════════════════════════════════════


class _ScriptedHarness:
    """Harness whose each prompt yields one scripted event list."""

    def __init__(self, scripts, sizes=None):
        self._scripts = list(scripts)
        self._sizes = list(sizes or [])
        self._size = self._sizes[0] if self._sizes else 0
        self.prompts: list[str] = []

    def prompt(self, text, attachments=None):
        self.prompts.append(text)

    def message_count(self):
        if self._sizes:
            self._size = self._sizes.pop(0)
        return self._size

    def events(self, **_kwargs):
        return iter(self._scripts.pop(0))


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr("senza_agent.llm_retry.retry_delay_ms", lambda _attempt: 0)
    monkeypatch.setenv("SENZA_AGENT_STREAM_RETRIES", "2")


async def _run_async(tm, text="do it"):
    await tm._run_task(text, 1000, None)


def _manager(harness):
    from senza_agent.webserver.task import TaskManager
    tm = TaskManager()
    tm.set_harness(harness)
    tm._on_event = lambda _wire: None
    return tm


@pytest.mark.asyncio
async def test_interrupted_stream_is_retried_after_rollback():
    h = _ScriptedHarness(
        [
            [{"type": "text_delta", "text": "partial"}, {"type": "error", "message": _CUT, "error_type": "provider"}],
            [{"type": "agent_end", "new_messages": [{"role": "assistant", "text": "done"}]}, {"type": "settled"}],
        ],
        sizes=[0, 0],
    )
    tm = _manager(h)
    seen: list[dict] = []
    tm._on_event = seen.append

    await _run_async(tm)

    assert h.prompts == ["do it", "do it"]
    types = [w["type"] for w in seen]
    assert "retry_attempt" in types
    assert "error" not in types  # the retry succeeded
    assert types[-1] == "agent_end"  # turn finished; stream ends there


@pytest.mark.asyncio
async def test_no_retry_when_tool_already_ran():
    # History grew during the failed attempt → a tool executed; retrying
    # would duplicate the user message and repeat side effects.
    h = _ScriptedHarness(
        [[{"type": "error", "message": _CUT, "error_type": "provider"}]],
        sizes=[0, 2],
    )
    tm = _manager(h)
    seen: list[dict] = []
    tm._on_event = seen.append

    await _run_async(tm)

    assert h.prompts == ["do it"]
    assert any(w["type"] == "error" and "stream incomplete" in w["message"] for w in seen)
    assert not any(w["type"] == "retry_attempt" for w in seen)


@pytest.mark.asyncio
async def test_non_stream_provider_error_is_not_retried():
    h = _ScriptedHarness(
        [[{"type": "error", "message": "llm provider error: unauthorized", "error_type": "provider"}]],
        sizes=[0, 0],
    )
    tm = _manager(h)
    seen: list[dict] = []
    tm._on_event = seen.append

    await _run_async(tm)

    assert h.prompts == ["do it"]
    assert [w["type"] for w in seen] == ["error"]


@pytest.mark.asyncio
async def test_retry_budget_is_bounded():
    h = _ScriptedHarness(
        [[{"type": "error", "message": _CUT, "error_type": "provider"}] for _ in range(5)],
        sizes=[0] * 5,
    )
    tm = _manager(h)
    seen: list[dict] = []
    tm._on_event = seen.append

    await _run_async(tm)

    assert h.prompts == ["do it"] * 3  # original + 2 retries
    assert [w["type"] for w in seen].count("retry_attempt") == 2
    assert seen[-1]["type"] == "error"


@pytest.mark.asyncio
async def test_raised_interrupted_stream_is_retried():
    class _Raising(_ScriptedHarness):
        def prompt(self, text, attachments=None):
            super().prompt(text, attachments)
            if len(self.prompts) == 1:
                raise RuntimeError(_CUT)

    h = _Raising(
        [[], [{"type": "settled"}]],
        sizes=[0, 0],
    )
    tm = _manager(h)
    seen: list[dict] = []
    tm._on_event = seen.append

    await _run_async(tm)

    assert h.prompts == ["do it", "do it"]
    assert "error" not in [w["type"] for w in seen]


# ══ dashboard rendering ═════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_drops_partial_streaming_block():
    from senza_agent.webserver.qevos_bridge import StateBridge

    sb = StateBridge(MagicMock(), MagicMock())
    sb.broadcast = AsyncMock()

    await sb.on_task_event({"type": "text_delta", "text": "truncated answer"})
    assert [e["type"] for e in sb._current_events] == ["streaming"]

    await sb.on_task_event({
        "type": "retry_attempt", "attempt": 1, "max_retries": 2,
        "delay_ms": 1000, "error": _CUT,
    })
    assert [e["type"] for e in sb._current_events] == []
    assert sb._streaming_idx is None

    await sb.on_task_event({"type": "text_delta", "text": "fresh answer"})
    assert sb._current_events[0]["text"] == "fresh answer"
