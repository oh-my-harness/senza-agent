"""MCP server lifecycle: creation, readiness wait, and guaranteed teardown.

Workarounds for senza-sdk 1.3.0 defects (see issue #1):

* ``HarnessBuilder.mcp_config_file()`` / ``mcp_server()`` are silent no-ops.
  They return ``self`` but wire zero tools into the harness.
* ``HarnessBuilder.session_repo()`` silently drops every MCP tool, in any
  call order, and re-attaching the manager does not recover them.
* The manager created internally by ``mcp_config_file()`` is never
  disconnected, so npx/node MCP child processes outlive the app.

The only working path is an explicit ``McpManager`` + ``with_mcp_manager()``,
plus explicit teardown.  This module owns that lifecycle.
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import time
from typing import Any

import senza

# Servers in these states have not finished starting yet.
_PENDING_STATES = ("connecting", "starting", "pending", "initializing")

# Managers we created.  Kept alive so they are not garbage-collected mid-run
# and so teardown can reach them.
_MANAGERS: list = []
_teardown_installed = False
_job_handle: Any = None


def _server_names(config_path: str) -> list:
    try:
        with open(config_path, encoding="utf-8") as f:
            return list((json.load(f).get("mcpServers") or {}).keys())
    except Exception:
        return []


def create_manager(config_path: str, timeout: float = 180.0) -> Any:
    """Create a connected ``McpManager`` for *config_path*.

    Teardown handlers are installed *before* ``load_config_file`` so that any
    child process it spawns is already covered by the kill-on-close job.
    """
    install_teardown()
    manager = senza.McpManager()
    manager.load_config_file(config_path)

    deadline = time.monotonic() + timeout
    for name in _server_names(config_path):
        while time.monotonic() < deadline:
            try:
                status = manager.get_status(name)
            except Exception:
                break
            if status not in _PENDING_STATES:
                break
            time.sleep(0.25)

    try:
        errors = manager.errors()
    except Exception:
        errors = {}
    if errors:
        print(f"Warning: MCP server errors: {errors}", file=sys.stderr)

    _MANAGERS.append(manager)
    return manager


def disconnect_all() -> None:
    """Disconnect every MCP server we started.  Safe to call repeatedly."""
    while _MANAGERS:
        manager = _MANAGERS.pop()
        try:
            manager.disconnect_all()
        except Exception:
            pass


def install_teardown() -> None:
    """Register exit hooks exactly once."""
    global _teardown_installed
    if _teardown_installed:
        return
    _teardown_installed = True

    atexit.register(disconnect_all)

    for signame in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            previous = signal.getsignal(sig)
            signal.signal(sig, _make_handler(sig, previous))
        except (ValueError, OSError):
            pass  # not in the main thread, or signal unsupported here

    _install_kill_on_close_job()


def _make_handler(sig, previous):
    def _handler(signum, frame):
        disconnect_all()
        try:
            signal.signal(sig, previous if callable(previous) else signal.SIG_DFL)
        except Exception:
            pass
        if callable(previous) and previous not in (signal.SIG_DFL, signal.SIG_IGN):
            return previous(signum, frame)
        raise SystemExit(128 + signum)

    return _handler


def _install_kill_on_close_job() -> None:
    """Windows: put this process in a job that kills children when it dies.

    Child processes inherit job membership, so every MCP server spawned later
    is terminated by the OS even if this process is hard-killed -- which skips
    both ``atexit`` and signal handlers.
    """
    global _job_handle
    if sys.platform != "win32" or _job_handle is not None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _Basic),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _Extended()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = 0x2000
        if not k32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            return
        handle = k32.OpenProcess(0x1F0FFF, False, os.getpid())
        if not handle or not k32.AssignProcessToJobObject(job, handle):
            return
        _job_handle = job  # keep the handle open for the process lifetime
    except Exception as e:
        print(f"Warning: could not install kill-on-close job: {e}", file=sys.stderr)
