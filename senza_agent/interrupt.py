"""User interrupt REPL handler — ported from QevosAgent's user_interrupt.py.

Reads stdin in a background thread and supports slash commands for inspecting
and steering the agent during a run:

  /help, /status, /log [N], /newtask <text>, /stop, /exit, /quit,
  /pause, /inject <text>, /compress [N], /+N

``/inject`` and ``/pause`` delegate to ``harness.steer()`` / ``harness.abort()``
when a Senza harness is available. ``/status`` and ``/log`` read from
:class:`~senza_agent.behavior.state.AgentState`.

The handler is optional — if stdin is not a TTY and no terminal is attached,
``start()`` is a no-op.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from typing import Optional

from .i18n import t

BLUE = "\033[94m"
RESET = "\033[0m"

# Commands that can be handled immediately in the reader thread (no state needed)
_IMMEDIATE_CMDS = {"/help"}


class ReplCommandHandler:
    """Background stdin reader supporting TTY char-mode and pipe line-mode.

    Attributes:
        pause_requested: set True the moment ``/`` is pressed as the first
            character of a line, so the main loop can pause after the current
            tool call and give the user time to finish typing.
        force_stop: set True when ``/stop`` is submitted, so a polling tool
            thread can notice and abandon its wait.
    """

    def __init__(self, harness=None, state=None):
        self._harness = harness  # Senza AgentHarness for steer/abort
        self._state = state      # AgentState for status/log
        self._cmd_queue: queue.Queue[str] = queue.Queue()
        self._input_queue: queue.Queue[Optional[str]] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # Immediate flag: True from the moment '/' is pressed until the full
        # command is submitted.
        self.pause_requested: bool = False
        # Force-stop flag: set when /stop is submitted.
        self.force_stop: bool = False
        self._is_tty: bool = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background stdin reader thread.

        No-op if stdin is not a TTY and not a pipe (e.g. when running inside
        an environment without a real stdin). On Unix TTYs uses cbreak mode
        for immediate ``/`` detection; on pipes falls back to line mode.
        """
        if self._running:
            return
        # If stdin is neither a TTY nor readable, do nothing.
        if not self._stdin_available():
            return
        self._running = True
        target = self._read_loop_tty_unix if self._is_tty else self._read_loop_pipe
        self._thread = threading.Thread(target=target, daemon=True, name="user-interrupt")
        self._thread.start()

    def stop(self) -> None:
        """Stop the reader thread and unblock any pending get_user_input()."""
        self._running = False
        self._input_queue.put(None)

    def _stdin_available(self) -> bool:
        """Return True if stdin can be read at all."""
        try:
            if sys.stdin is None:
                return False
            # TTY → always available. Non-TTY (pipe) → only if fileno() works.
            if self._is_tty:
                return True
            sys.stdin.fileno()
            return True
        except (ValueError, OSError):
            return False

    # ── Background reader threads ────────────────────────────────────────────

    def _read_loop_tty_unix(self) -> None:
        """Unix TTY: setcbreak char-by-char reading, '/' detected immediately."""
        try:
            import termios
            import tty
        except ImportError:
            self._read_loop_pipe()
            return
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            buf: list[str] = []
            while self._running:
                ch = sys.stdin.read(1)
                if not ch:
                    self._input_queue.put(None)
                    break
                buf = self._handle_char_unix(ch, buf)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _handle_char_unix(self, ch: str, buf: list) -> list:
        """Unix TTY single-char handling, returns the updated buffer."""
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._finish_line("".join(buf))
            return []
        if ch in ("\x08", "\x7f"):  # Backspace / Delete
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            return buf
        if ch == "\x03":  # Ctrl+C
            raise KeyboardInterrupt
        if not ch.isprintable():
            return buf
        if not buf and ch == "/":
            self._on_slash_pressed()
        buf.append(ch)
        sys.stdout.write(ch)
        sys.stdout.flush()
        return buf

    def _read_loop_pipe(self) -> None:
        """Pipe / non-TTY: read line by line (no immediate '/' detection)."""
        while self._running:
            try:
                line = sys.stdin.readline()
            except Exception:
                break
            if not line:
                self._input_queue.put(None)
                break
            self._finish_line(line.rstrip("\n"))

    # ── Core dispatch ────────────────────────────────────────────────────────

    def _on_slash_pressed(self) -> None:
        """Called the instant the user presses '/' as the first char.

        Immediately enqueues a ``/__pause__`` sentinel so the main loop is
        guaranteed to see the pause request even if _finish_line clears
        pause_requested before the loop checks it.
        """
        if self.pause_requested:
            return
        self.pause_requested = True
        self._cmd_queue.put("/__pause__")
        print(f"\n{BLUE}{t('interrupt.pause_detected')}{RESET}", flush=True)

    def _finish_line(self, line: str) -> None:
        """Called after the user presses Enter; dispatches the complete line."""
        line = line.strip()

        if not line.startswith("/"):
            # Plain text → route to ask_user / input queue
            self.pause_requested = False
            self._input_queue.put(line)
            return

        if line == "/":
            line = "/help"

        self.pause_requested = False
        parts = line.split(None, 1)
        name = parts[0].lower()

        if name in _IMMEDIATE_CMDS:
            self._handle_immediate(name)
        else:
            if name == "/stop":
                self.force_stop = True
            self._ack_deferred(name)
            self._cmd_queue.put(line)

    def _handle_immediate(self, name: str) -> None:
        """Execute a command immediately in the reader thread (no state)."""
        if name == "/help":
            print(f"\n{BLUE}{t('interrupt.help')}{RESET}", flush=True)

    def _ack_deferred(self, name: str) -> None:
        """Print an immediate acknowledgement for a deferred command."""
        print(f"\n{BLUE}{t('interrupt.ack', name=name)}{RESET}", flush=True)

    # ── Public API for the main loop ─────────────────────────────────────────

    def poll_command(self) -> Optional[str]:
        """Non-blocking: return the next deferred command, or None."""
        try:
            return self._cmd_queue.get_nowait()
        except queue.Empty:
            return None

    def wait_command(self, timeout: float = 0.1) -> Optional[str]:
        """Block up to *timeout* seconds for a command, or return None."""
        try:
            return self._cmd_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def get_user_input(self, prompt: str = "") -> Optional[str]:
        """Block waiting for non-command text (replaces input()). None = EOF."""
        if prompt:
            print(prompt, end="", flush=True)
        return self._input_queue.get()

    # ── Command processing (called at iteration boundaries or pause) ─────────

    def process_command(self, cmd: str) -> str:
        """Parse and handle one deferred command.

        Returns ``"continue"``, ``"stop"``, or ``"pause"``.
        """
        parts = cmd.strip().split(None, 1)
        name = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if name == "/__pause__":
            return "pause"

        if name == "/pause":
            print(f"\n{BLUE}{t('interrupt.pause')}{RESET}", flush=True)
            return "pause"

        if name == "/stop":
            print(f"\n{BLUE}{t('interrupt.stop')}{RESET}", flush=True)
            if self._harness is not None:
                try:
                    self._harness.abort()
                except Exception:
                    pass
            return "continue"

        if name in ("/exit", "/quit"):
            print(f"\n{BLUE}{t('interrupt.exit')}{RESET}", flush=True)
            return "stop"

        if name == "/newtask":
            if not arg:
                print(f"\n{BLUE}{t('interrupt.newtask_usage')}{RESET}", flush=True)
                return "continue"
            self._input_queue.put(arg.strip())
            print(f"\n{BLUE}{t('interrupt.newtask_done', arg=arg.strip()[:80])}{RESET}", flush=True)
            return "continue"

        if name == "/inject":
            if not arg:
                print(f"\n{BLUE}{t('interrupt.inject_usage')}{RESET}", flush=True)
                return "continue"
            self._do_inject(arg)
            print(f"\n{BLUE}{t('interrupt.inject_done')}{RESET}", flush=True)
            return "continue"

        if name == "/compress":
            try:
                keep = int(arg) if arg.strip() else 8
                keep = max(2, keep)
            except ValueError:
                keep = 8
            before = len(self._get_short_term())
            if self._state is not None:
                self._state.meta["_compress_requested"] = keep
            print(f"\n{BLUE}{t('interrupt.compress', keep=keep, before=before)}{RESET}", flush=True)
            return "continue"

        if name == "/status":
            self._print_status()
            return "continue"

        if name == "/log":
            try:
                n = int(arg) if arg.strip() else 5
            except ValueError:
                n = 5
            self._print_log(n)
            return "continue"

        if name.startswith("/+"):
            suffix = name[2:]
            if suffix.isdigit() and int(suffix) > 0:
                n = int(suffix)
                if self._state is not None:
                    self._state.meta["_add_iterations"] = (
                        self._state.meta.get("_add_iterations", 0) + n
                    )
                    total = self._state.meta["_add_iterations"]
                else:
                    total = n
                print(
                    f"\n{BLUE}{t('interrupt.add_iters', n=n, total=total)}{RESET}",
                    flush=True,
                )
                return "continue"
            else:
                print(f"\n{BLUE}{t('interrupt.add_iters_usage')}{RESET}", flush=True)
                return "continue"

        print(f"\n{BLUE}{t('interrupt.unknown_cmd', name=name)}{RESET}", flush=True)
        return "continue"

    # ── /inject implementation ───────────────────────────────────────────────

    def _do_inject(self, arg: str) -> None:
        """Inject *arg* into the agent context.

        If a Senza harness is available, use ``harness.steer(text)``.
        Otherwise, append to ``state.meta['_user_injections']`` so the advisor
        can pick it up.
        """
        if self._harness is not None:
            try:
                self._harness.steer(arg)
                return
            except Exception:
                pass  # fall through to meta-based injection

        if self._state is None:
            return

        try:
            from datetime import datetime, timezone

            _pending = self._capture_pending_action()
            _inj_list = self._state.meta.setdefault("_user_injections", [])
            _inj_list.append({
                "iter": int(getattr(self._state, "turn_count", 0) or 0),
                "ts": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "content": arg,
                "source": "inject_cmd",
                "agent_pending_action": _pending,
            })
        except Exception:
            pass

    # ── Helpers for reading state ────────────────────────────────────────────

    def _get_short_term(self) -> list:
        """Return the short-term message list from state.meta or empty."""
        if self._state is None:
            return []
        st = self._state.meta.get("short_term")
        if st is None:
            return []
        return list(st)

    # ── Pending-action capture (for inject tracking) ─────────────────────────

    def _capture_pending_action(self) -> Optional[dict]:
        """Capture the agent's most recent action at the moment of interrupt.

        Scans ``short_term`` in reverse for the last assistant message and
        parses it. Falls back to the current tool name. Returns None if no
        action can be determined.
        """
        import json as _json

        short_term = self._get_short_term()
        try:
            for _m in reversed(short_term):
                if not isinstance(_m, dict) or _m.get("role") != "assistant":
                    continue
                _c = _m.get("content")
                try:
                    _obj = _json.loads(_c) if isinstance(_c, str) else _c
                except Exception:
                    return {"raw": str(_c)[:500]}
                if not isinstance(_obj, dict):
                    return {"raw": str(_c)[:500]}
                return {
                    "thought": (_obj.get("thought") or "")[:500],
                    "tool": _obj.get("tool") or "",
                    "args": _obj.get("args") or {},
                    "final_answer": (_obj.get("final_answer") or "")[:500],
                }
        except Exception:
            pass
        # Fallback: at least record the current tool name
        if self._state is not None:
            try:
                cur = self._state.meta.get("_current_tool")
            except Exception:
                cur = None
            return {"tool": cur} if cur else None
        return None

    # ── Read-only status display (safe from any thread) ──────────────────────

    def _print_status(self) -> None:
        """Print the agent's current running state (/status command)."""
        import time as _time

        CYAN = "\033[96m"
        GRAY = "\033[90m"
        RESET = "\033[0m"

        state = self._state
        if state is None:
            print(f"\n{CYAN}{'─' * 56}{RESET}", flush=True)
            print(f"{GRAY}  (no state available){RESET}", flush=True)
            print(f"{CYAN}{'─' * 56}{RESET}", flush=True)
            return

        iteration = getattr(state, "turn_count", 0) or 0
        tools = getattr(state, "tools", []) or []
        # senza-agent has no long_term; use meta length as a stand-in count
        long_term = state.meta.get("long_term", []) or []
        # short_term for display
        short_term = self._get_short_term()

        lines = [f"\n{CYAN}{'─' * 56}{RESET}"]
        lines.append(
            f"{CYAN}{t('status.header', i=iteration, tools=len(tools), lt=len(long_term))}{RESET}"
        )

        cur_tool = state.meta.get("_current_tool")
        cur_start = state.meta.get("_current_tool_start")
        if cur_tool:
            elapsed = f"{_time.time() - cur_start:.0f}s" if cur_start else "?"
            lines.append(
                f"{CYAN}{t('status.current_tool', tool=cur_tool, elapsed=elapsed)}{RESET}"
            )
        else:
            lines.append(f"{GRAY}{t('status.idle')}{RESET}")

        scratchpad = (state.meta.get("scratchpad") or "").strip()
        if len(scratchpad) > 400:
            sp_preview = scratchpad[:400] + t("status.truncated")
        else:
            sp_preview = scratchpad
        lines.append(f"{CYAN}{t('status.scratchpad')}{RESET}")
        for ln in sp_preview.splitlines():
            lines.append(f"  {ln}")

        lines.append(f"{CYAN}{'─' * 56}{RESET}")
        print("\n".join(lines), flush=True)

    def _print_log(self, n: int = 5) -> None:
        """Print the last *n* short-term execution records (/log command)."""
        import json as _json

        YELLOW = "\033[93m"
        GREEN = "\033[92m"
        GRAY = "\033[90m"
        CYAN = "\033[96m"
        RESET = "\033[0m"

        history = self._get_short_term()
        recent = history[-n:] if len(history) > n else history

        print(f"\n{CYAN}{'─' * 56}{RESET}", flush=True)
        print(
            f"{CYAN}{t('log.header', n=len(recent), total=len(history))}{RESET}",
            flush=True,
        )

        for i, msg in enumerate(recent, start=len(history) - len(recent)):
            role = msg.get("role", "?")
            content = msg.get("content", "")

            if role == "assistant":
                try:
                    obj = _json.loads(content) if isinstance(content, str) else content
                    thought = obj.get("thought", "")
                    tool = obj.get("tool", "")
                    ans = obj.get("final_answer", "")
                    if tool:
                        label = f"{YELLOW}{t('log.tool', i=i, tool=tool)}{RESET}"
                        detail = _json.dumps(obj.get("args", {}), ensure_ascii=False)
                        detail = detail[:200] + "..." if len(detail) > 200 else detail
                    elif ans:
                        label = f"{GREEN}{t('log.done', i=i)}{RESET}"
                        detail = ans[:200] + "..." if len(ans) > 200 else ans
                    else:
                        label = f"{CYAN}{t('log.thought', i=i)}{RESET}"
                        detail = (thought[:200] + "...") if len(thought) > 200 else thought
                    print(f"  {label}", flush=True)
                    if detail:
                        print(f"    {detail}", flush=True)
                    continue
                except Exception:
                    pass

            preview = str(content)
            if len(preview) > 300:
                preview = preview[:300] + "..."
            color = GRAY if role == "user" else CYAN
            tag = (
                t("log.result_tag")
                if "[工具结果]" in preview or "[系统]" in preview
                else f"{'👤' if role == 'user' else '🤖'} {role}"
            )
            first_line = preview.splitlines()[0] if preview else ""
            print(f"  {color}[#{i}] {tag}: {first_line}{RESET}", flush=True)

        print(f"{CYAN}{'─' * 56}{RESET}", flush=True)
