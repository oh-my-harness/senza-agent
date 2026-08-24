"""Tests for the ReplCommandHandler (interrupt.py)."""
from __future__ import annotations

import io
from contextlib import redirect_stdout

from senza_agent.behavior.state import AgentState
from senza_agent.interrupt import ReplCommandHandler


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_state(**kw) -> AgentState:
    """Create an AgentState with sensible test defaults."""
    state = AgentState()
    state.turn_count = kw.get("turn_count", 3)
    state.goal = kw.get("goal", "test goal")
    state.tools = kw.get("tools", ["shell", "read_file"])
    state.meta = kw.get("meta", {})
    if "short_term" not in state.meta:
        state.meta["short_term"] = kw.get("short_term", [])
    return state


def _capture(handler: ReplCommandHandler, cmd: str) -> str:
    """Run process_command and capture stdout; returns (result, output)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = handler.process_command(cmd)
    return result, buf.getvalue()


# ── A fake harness for steer/abort ──────────────────────────────────────────


class FakeHarness:
    def __init__(self):
        self.steered: list[str] = []
        self.aborted: int = 0

    def steer(self, text: str) -> None:
        self.steered.append(text)

    def abort(self) -> None:
        self.aborted += 1


# ── /help ────────────────────────────────────────────────────────────────────


def test_help_returns_continue_and_prints():
    h = ReplCommandHandler(state=_make_state())
    result, out = _capture(h, "/help")
    assert result == "continue"
    assert "/help" in out or "help" in out.lower()


# ── /status ──────────────────────────────────────────────────────────────────


def test_status_returns_continue_and_prints():
    state = _make_state(turn_count=7)
    state.meta["_current_tool"] = "shell"
    state.meta["scratchpad"] = "doing work"
    h = ReplCommandHandler(state=state)
    result, out = _capture(h, "/status")
    assert result == "continue"
    assert "7" in out  # iteration count
    assert "shell" in out  # current tool


def test_status_shows_idle_when_no_tool():
    state = _make_state()
    h = ReplCommandHandler(state=state)
    result, out = _capture(h, "/status")
    assert result == "continue"
    assert "idle" in out.lower() or "空闲" in out


def test_status_with_no_state():
    h = ReplCommandHandler(state=None)
    result, out = _capture(h, "/status")
    assert result == "continue"
    assert "no state" in out.lower()


# ── /log ─────────────────────────────────────────────────────────────────────


def test_log_returns_continue_and_prints():
    state = _make_state(
        short_term=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": '{"thought": "thinking", "tool": "shell", "args": {}}'},
        ]
    )
    h = ReplCommandHandler(state=state)
    result, out = _capture(h, "/log")
    assert result == "continue"
    assert "shell" in out or "工具" in out or "Tool" in out


def test_log_with_n():
    """Even if history has 5 items, /log 2 should only show last 2."""
    state = _make_state(
        short_term=[
            {"role": "user", "content": f"msg{i}"} for i in range(5)
        ]
    )
    h = ReplCommandHandler(state=state)
    result, out = _capture(h, "/log 2")
    assert result == "continue"
    # Should mention total=5
    assert "5" in out


# ── /exit /quit ──────────────────────────────────────────────────────────────


def test_exit_returns_stop():
    h = ReplCommandHandler(state=_make_state())
    result, _ = _capture(h, "/exit")
    assert result == "stop"


def test_quit_returns_stop():
    h = ReplCommandHandler(state=_make_state())
    result, _ = _capture(h, "/quit")
    assert result == "stop"


# ── /stop ────────────────────────────────────────────────────────────────────


def test_stop_returns_continue_and_calls_abort():
    harness = FakeHarness()
    h = ReplCommandHandler(harness=harness, state=_make_state())
    result, _ = _capture(h, "/stop")
    assert result == "continue"
    assert harness.aborted == 1


def test_stop_without_harness():
    h = ReplCommandHandler(state=_make_state())
    result, _ = _capture(h, "/stop")
    assert result == "continue"


# ── /inject ──────────────────────────────────────────────────────────────────


def test_inject_calls_harness_steer():
    harness = FakeHarness()
    h = ReplCommandHandler(harness=harness, state=_make_state())
    result, out = _capture(h, "/inject hello world")
    assert result == "continue"
    assert harness.steered == ["hello world"]


def test_inject_without_harness_records_in_meta():
    state = _make_state(turn_count=5)
    h = ReplCommandHandler(harness=None, state=state)
    result, _ = _capture(h, "/inject fix the bug")
    assert result == "continue"
    injections = state.meta.get("_user_injections", [])
    assert len(injections) == 1
    assert injections[0]["content"] == "fix the bug"
    assert injections[0]["iter"] == 5
    assert injections[0]["source"] == "inject_cmd"


def test_inject_no_arg_shows_usage():
    h = ReplCommandHandler(state=_make_state())
    result, out = _capture(h, "/inject")
    assert result == "continue"
    assert "用法" in out or "Usage" in out


# ── /+N ──────────────────────────────────────────────────────────────────────


def test_add_iterations():
    state = _make_state()
    h = ReplCommandHandler(state=state)
    result, _ = _capture(h, "/+5")
    assert result == "continue"
    assert state.meta["_add_iterations"] == 5


def test_add_iterations_accumulates():
    state = _make_state()
    h = ReplCommandHandler(state=state)
    _capture(h, "/+5")
    _capture(h, "/+10")
    assert state.meta["_add_iterations"] == 15


def test_add_iterations_invalid():
    state = _make_state()
    h = ReplCommandHandler(state=state)
    result, out = _capture(h, "/+abc")
    assert result == "continue"
    assert "用法" in out or "Usage" in out


# ── /compress ────────────────────────────────────────────────────────────────


def test_compress_default():
    state = _make_state(short_term=[{"role": "user", "content": "x"}] * 10)
    h = ReplCommandHandler(state=state)
    result, _ = _capture(h, "/compress")
    assert result == "continue"
    assert state.meta["_compress_requested"] == 8


def test_compress_with_n():
    state = _make_state(short_term=[{"role": "user", "content": "x"}] * 10)
    h = ReplCommandHandler(state=state)
    result, _ = _capture(h, "/compress 5")
    assert result == "continue"
    assert state.meta["_compress_requested"] == 5


def test_compress_clamps_to_min_2():
    state = _make_state()
    h = ReplCommandHandler(state=state)
    result, _ = _capture(h, "/compress 1")
    assert result == "continue"
    assert state.meta["_compress_requested"] == 2


# ── /newtask ─────────────────────────────────────────────────────────────────


def test_newtask_queues_input():
    h = ReplCommandHandler(state=_make_state())
    result, out = _capture(h, "/newtask do something new")
    assert result == "continue"
    val = h.get_user_input()
    assert val == "do something new"


def test_newtask_no_arg_shows_usage():
    h = ReplCommandHandler(state=_make_state())
    result, out = _capture(h, "/newtask")
    assert result == "continue"
    assert "用法" in out or "Usage" in out


# ── /pause ───────────────────────────────────────────────────────────────────


def test_pause_returns_pause():
    h = ReplCommandHandler(state=_make_state())
    result, _ = _capture(h, "/pause")
    assert result == "pause"


def test_pause_sentinel_returns_pause():
    h = ReplCommandHandler(state=_make_state())
    result, _ = _capture(h, "/__pause__")
    assert result == "pause"


# ── Unknown command ──────────────────────────────────────────────────────────


def test_unknown_command_returns_continue():
    h = ReplCommandHandler(state=_make_state())
    result, out = _capture(h, "/bogus")
    assert result == "continue"
    assert "未知" in out or "Unknown" in out


# ── _capture_pending_action ──────────────────────────────────────────────────


def test_capture_pending_action_from_assistant():
    state = _make_state(
        short_term=[
            {"role": "user", "content": "do X"},
            {"role": "assistant", "content": '{"thought": "I will use shell", "tool": "shell", "args": {"cmd": "ls"}}'},
        ]
    )
    h = ReplCommandHandler(state=state)
    action = h._capture_pending_action()
    assert action is not None
    assert action["tool"] == "shell"
    assert action["thought"] == "I will use shell"


def test_capture_pending_action_fallback_to_current_tool():
    state = _make_state(short_term=[])
    state.meta["_current_tool"] = "read_file"
    h = ReplCommandHandler(state=state)
    action = h._capture_pending_action()
    assert action is not None
    assert action["tool"] == "read_file"


def test_capture_pending_action_returns_none_when_empty():
    state = _make_state(short_term=[])
    h = ReplCommandHandler(state=state)
    action = h._capture_pending_action()
    assert action is None


# ── poll_command / wait_command ──────────────────────────────────────────────


def test_poll_command_returns_none_when_empty():
    h = ReplCommandHandler(state=_make_state())
    assert h.poll_command() is None


def test_wait_command_timeout():
    h = ReplCommandHandler(state=_make_state())
    assert h.wait_command(timeout=0.05) is None
