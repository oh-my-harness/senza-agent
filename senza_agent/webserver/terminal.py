"""PTY terminal sessions backed by ``ptyprocess``.

Each terminal is a named, shareable session: the agent (over HTTP API) and
browser clients (over WebSocket) drive the SAME shell and watch each other.
Each session keeps a ring buffer of recent output so a (re)attaching client
or the agent can read history.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ptyprocess is the pure-Python PTY library; falls back to subprocess if unavailable.
try:
    import ptyprocess
except ImportError:
    ptyprocess = None  # type: ignore[assignment]


_TERM_BUF_MAX = 200_000  # chars of output kept per session
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI/VT escape sequences so the model reads clean text."""
    return _ANSI_RE.sub("", text or "").replace("\r\n", "\n").replace("\r", "")


def _default_shell() -> str:
    if os.environ.get("TERMINAL_SHELL"):
        return os.environ["TERMINAL_SHELL"]
    if os.name == "nt":
        return "powershell.exe"
    return os.environ.get("SHELL", "bash")


@dataclass
class TerminalSession:
    """A single PTY-backed terminal session."""

    id: str
    pty: Any
    title: str = "Terminal"
    cwd: str = ""
    cols: int = 80
    rows: int = 24
    buf: str = ""
    total_seq: int = 0
    owner: str = "user"  # 'user' | 'agent'
    alive: bool = True
    created_at: float = field(default_factory=time.time)

    def to_public(self) -> dict[str, Any]:
        """Return public metadata (no buffer contents)."""
        return {
            "id": self.id,
            "title": self.title,
            "owner": self.owner,
            "alive": self.alive,
            "cwd": self.cwd,
            "cols": self.cols,
            "rows": self.rows,
        }


class TerminalManager:
    """Manages PTY terminal sessions.

    Creates named sessions that persist across browser tab reconnects.
    The agent interacts via HTTP API (input/output/owner); browsers
    connect via WebSocket for live two-way PTY access.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, TerminalSession] = {}
        self._seq: int = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"t{self._seq}-{int(time.time() * 1000):x}"

    def create_session(
        self,
        title: str = "Terminal",
        cols: int = 80,
        rows: int = 24,
        cwd: str = "",
    ) -> dict[str, Any]:
        """Create a new PTY-backed terminal session.

        Returns ``{id, title, ...}`` on success, or ``{error}`` on failure.
        """
        if ptyprocess is None:
            return {"error": "ptyprocess is not installed (pip install ptyprocess)"}

        start_cwd = cwd or os.environ.get("TERMINAL_CWD") or os.path.expanduser("~")
        shell = _default_shell()

        try:
            proc = ptyprocess.PtyProcess.spawn(
                [shell],
                cwd=start_cwd,
                env={
                    **os.environ,
                    "TERM": "xterm-256color",
                },
                dimensions=(rows, cols),
            )
        except Exception as e:
            return {"error": str(e)}

        sess = TerminalSession(
            id=self._next_id(),
            pty=proc,
            title=title,
            cwd=start_cwd,
            cols=cols,
            rows=rows,
        )
        self._sessions[sess.id] = sess
        return {"id": sess.id, "title": sess.title}

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return public metadata for all sessions."""
        return [s.to_public() for s in self._sessions.values()]

    def get_session(self, sid: str) -> Optional[TerminalSession]:
        return self._sessions.get(sid)

    def read_since(self, sess: TerminalSession, since: int = 0) -> dict[str, Any]:
        """Read output recorded at/after absolute char offset ``since``."""
        # Drain available output from the PTY without blocking
        self._drain(sess)
        buf_start = sess.total_seq - len(sess.buf)
        from_idx = max(0, (int(since) or 0) - buf_start)
        return {
            "data": sess.buf[from_idx:],
            "seq": sess.total_seq,
            "alive": sess.alive,
            "owner": sess.owner,
        }

    def write_input(self, sess: TerminalSession, data: str) -> dict[str, Any]:
        """Write input to the PTY."""
        if not sess.alive:
            return {"error": "session is not alive"}
        try:
            sess.pty.write(data.encode("utf-8"))
            return {"ok": True, "seq": sess.total_seq}
        except Exception as e:
            return {"error": str(e)}

    def resize(self, sess: TerminalSession, cols: int, rows: int) -> dict[str, Any]:
        """Resize the PTY."""
        try:
            sess.pty.setwinsize(rows, cols)
            sess.cols = cols
            sess.rows = rows
            return {"ok": True}
        except Exception:
            return {"ok": False}

    def set_owner(self, sid: str, who: str) -> bool:
        """Set the 'mic' owner: 'agent' or 'user'."""
        sess = self._sessions.get(sid)
        if not sess:
            return False
        sess.owner = "agent" if who == "agent" else "user"
        return True

    def kill_session(self, sid: str) -> bool:
        """Kill and remove a session."""
        sess = self._sessions.get(sid)
        if not sess:
            return False
        try:
            sess.pty.terminate(force=True)
        except Exception:
            pass
        sess.alive = False
        del self._sessions[sid]
        return True

    def _drain(self, sess: TerminalSession) -> None:
        """Non-blocking read of available PTY output into the ring buffer."""
        if not sess.alive:
            return
        try:
            import select
            fd = sess.pty.fd
            while True:
                r, _, _ = select.select([fd], [], [], 0)
                if not r:
                    break
                try:
                    data = sess.pty.read(4096)
                except (OSError, EOFError):
                    sess.alive = False
                    break
                if not data:
                    break
                text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
                sess.buf += text
                sess.total_seq += len(text)
                if len(sess.buf) > _TERM_BUF_MAX:
                    sess.buf = sess.buf[-_TERM_BUF_MAX:]
        except Exception:
            pass

    def cleanup(self) -> None:
        """Kill all sessions on shutdown."""
        for sid in list(self._sessions.keys()):
            self.kill_session(sid)
