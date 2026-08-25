"""Async background job manager.

Ported from QevosAgent's ``agent/core/async_manager.py``.

Runs shell commands in background threads so the main loop can poll partial
output without blocking.  The original bound its lifecycle to
``state.meta["_async_manager"]``; here we use a module-level singleton so the
manager survives across tool calls without an ``AgentState``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import IO, Optional


class JobStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id: str
    command: str
    start_time: float
    proc: Optional[subprocess.Popen]

    _stdout_lines: list = field(default_factory=list)
    _stderr_lines: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    status: JobStatus = JobStatus.RUNNING
    returncode: Optional[int] = None
    end_time: Optional[float] = None

    pid: Optional[int] = None
    retired: bool = False
    reclaimed: bool = False
    _probed_at: float = 0.0

    _reader_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _timeout_timer: Optional[threading.Timer] = field(default=None, repr=False)

    def stdout_snapshot(self) -> str:
        with self._lock:
            return "".join(self._stdout_lines)

    def stderr_snapshot(self) -> str:
        with self._lock:
            return "".join(self._stderr_lines)

    def elapsed(self) -> float:
        end = self.end_time or time.time()
        return end - self.start_time


# ── Process-tree termination (cross-platform) ──────────────────────────────


def _kill_tree(pid: Optional[int]) -> None:
    if not pid or pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(f"taskkill /F /T /PID {pid}", shell=True, capture_output=True)
    else:
        try:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            pass


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            return False
        return f'"{int(pid)}"' in (out.stdout or "")
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _pid_cmdline(pid: Optional[int]) -> Optional[str]:
    if not pid or pid <= 0:
        return None
    if os.name == "nt":
        probes = [
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
            ["wmic", "process", "where", f"processid={int(pid)}", "get", "commandline", "/value"],
        ]
        for probe in probes:
            try:
                res = subprocess.run(probe, capture_output=True, text=True, timeout=20)
            except Exception:
                continue
            text = (res.stdout or "").strip()
            if text:
                return text
        return None
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        text = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if text:
            return text
    except Exception:
        pass
    try:
        res = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "args="],
            capture_output=True, text=True, timeout=15,
        )
        return (res.stdout or "").strip() or None
    except Exception:
        return None


def _identity_matches(pid: Optional[int], command: str) -> Optional[bool]:
    cmdline = _pid_cmdline(pid)
    if cmdline is None:
        return None
    tokens = sorted(re.findall(r"[A-Za-z0-9_./\\-]{4,}", command or ""), key=len, reverse=True)[:3]
    if not tokens:
        return None
    low = cmdline.lower()
    return all(tok.lower() in low for tok in tokens)


# ── Main class ──────────────────────────────────────────────────────────────


class AsyncJobManager:
    """Background task manager — subprocess + threading model."""

    def __init__(self, jobs_dir: Optional[Path] = None) -> None:
        self._jobs: dict = {}
        self._global_lock = threading.Lock()
        self._registry_lock = threading.Lock()
        self._jobs_dir: Optional[Path] = Path(jobs_dir) if jobs_dir else None
        if self._jobs_dir:
            self._jobs_dir.mkdir(parents=True, exist_ok=True)

    # ── start ───────────────────────────────────────────────────────────────

    def start_shell(self, command: str, timeout: Optional[int] = None) -> str:
        job_id = f"job_{uuid.uuid4().hex[:8]}"

        popen_kwargs: dict = {
            "shell": True,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(command, **popen_kwargs)
        except Exception as e:
            dummy = Job(
                job_id=job_id, command=command, start_time=time.time(),
                proc=None, status=JobStatus.FAILED, end_time=time.time(), returncode=-1,
            )
            dummy._stderr_lines.append(str(e))
            with self._global_lock:
                self._jobs[job_id] = dummy
            self._flush_registry()
            return job_id

        job = Job(job_id=job_id, command=command, start_time=time.time(), proc=proc, pid=proc.pid)

        reader = threading.Thread(target=self._reader, args=(job,), daemon=True)
        reader.start()
        job._reader_thread = reader

        if timeout and timeout > 0:
            timer = threading.Timer(timeout, self._on_timeout, args=(job_id,))
            timer.daemon = True
            timer.start()
            job._timeout_timer = timer

        with self._global_lock:
            self._jobs[job_id] = job
        self._flush_registry()
        return job_id

    # ── background reader thread ────────────────────────────────────────────

    def _reader(self, job: Job) -> None:
        job_file: Optional[IO] = None
        if self._jobs_dir:
            try:
                job_file = open(
                    self._jobs_dir / f"{job.job_id}.txt",
                    "w", encoding="utf-8", errors="replace", buffering=1,
                )
                job_file.write(f"$ {job.command}\n")
                job_file.flush()
            except Exception:
                job_file = None

        def _drain(stream, lines, lock, prefix: str = ""):
            try:
                for line in stream:
                    with lock:
                        lines.append(line)
                        if job_file:
                            try:
                                job_file.write(prefix + line)
                                job_file.flush()
                            except Exception:
                                pass
            except Exception:
                pass

        t_out = threading.Thread(target=_drain, args=(job.proc.stdout, job._stdout_lines, job._lock, ""), daemon=True)
        t_err = threading.Thread(target=_drain, args=(job.proc.stderr, job._stderr_lines, job._lock, "[STDERR] "), daemon=True)
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()

        job.proc.wait()
        job.returncode = job.proc.returncode

        for stream in (job.proc.stdout, job.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

        with job._lock:
            if job.status == JobStatus.RUNNING:
                job.status = JobStatus.DONE if job.returncode == 0 else JobStatus.FAILED
            job.end_time = time.time()

        if job_file:
            try:
                job_file.write(f"\n[Exit {job.returncode}]\n")
                job_file.close()
            except Exception:
                pass

        if job._timeout_timer:
            job._timeout_timer.cancel()

        self._flush_registry()

    def _on_timeout(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return
        with job._lock:
            job.status = JobStatus.CANCELLED
        _kill_tree(job.pid)
        try:
            if job.proc is not None:
                job.proc.kill()
        except Exception:
            pass
        self._flush_registry()

    # ── query / wait ─────────────────────────────────────────────────────────

    def peek(self, job_id: str, wait_secs: float = 0.0) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            return {"error": f"job_id '{job_id}' does not exist or has been cleaned up"}

        if wait_secs > 0 and job.status == JobStatus.RUNNING:
            deadline = time.time() + wait_secs
            while time.time() < deadline and job.status == JobStatus.RUNNING:
                time.sleep(0.2)
                if job.reclaimed:
                    self._refresh_reclaimed(job)

        if job.reclaimed:
            self._refresh_reclaimed(job)

        stdout = job.stdout_snapshot().strip()
        stderr = job.stderr_snapshot().strip()
        output = stdout
        if stderr:
            output += f"\n[STDERR]: {stderr}"
        if not output:
            output = self._read_archived_output(job).strip()

        info = {
            "job_id": job_id,
            "status": job.status.value,
            "output": output or "(no output yet)",
            "returncode": job.returncode,
            "elapsed_s": round(job.elapsed(), 1),
            "command": job.command,
        }
        if job.reclaimed:
            info["reclaimed"] = True
            info["pid"] = job.pid
        return info

    # ── archived output ──────────────────────────────────────────────────────

    def _job_file(self, job_id: str) -> Optional[Path]:
        return (self._jobs_dir / f"{job_id}.txt") if self._jobs_dir else None

    def _read_archived_output(self, job: Job, tail_chars: int = 20000) -> str:
        path = self._job_file(job.job_id)
        if path is None or not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""
        return text if len(text) <= tail_chars else "…(truncated)\n" + text[-tail_chars:]

    def _refresh_reclaimed(self, job: Job, min_interval: float = 3.0) -> None:
        if job.status != JobStatus.RUNNING:
            return
        now = time.time()
        if now - job._probed_at < min_interval:
            return
        job._probed_at = now
        if _pid_alive(job.pid):
            return
        with job._lock:
            job.status = JobStatus.DONE
            job.end_time = time.time()
        self._flush_registry()

    # ── cancel ───────────────────────────────────────────────────────────────

    def cancel(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            return {"error": f"job_id '{job_id}' does not exist"}
        if job.reclaimed:
            self._refresh_reclaimed(job)
        if job.status != JobStatus.RUNNING:
            return {"error": f"job {job_id} already finished (status: {job.status.value})"}

        if job.reclaimed:
            verdict = _identity_matches(job.pid, job.command)
            if verdict is not True:
                reason = "PID no longer runs this command" if verdict is False else "cannot read process cmdline"
                return {"error": f"refuse to cancel reclaimed job {job_id}: {reason}"}

        with job._lock:
            job.status = JobStatus.CANCELLED

        if job._timeout_timer:
            job._timeout_timer.cancel()

        _kill_tree(job.pid)
        try:
            if job.proc is not None:
                job.proc.kill()
        except Exception:
            pass

        self._flush_registry()
        return {"job_id": job_id, "cancelled": True}

    # ── list ─────────────────────────────────────────────────────────────────

    def list_jobs(self) -> list:
        with self._global_lock:
            jobs = list(self._jobs.values())
        for j in jobs:
            if j.reclaimed:
                self._refresh_reclaimed(j)
        out = []
        for j in jobs:
            entry = {
                "job_id": j.job_id,
                "status": j.status.value,
                "command": j.command[:100],
                "elapsed_s": round(j.elapsed(), 1),
                "returncode": j.returncode,
            }
            if j.retired:
                entry["archived"] = True
            if j.reclaimed:
                entry["reclaimed"] = True
                entry["pid"] = j.pid
            out.append(entry)
        return out

    # ── drain completed (for hook notification) ───────────────────────────

    def drain_completed(self) -> list:
        """Return [(job_id, output_summary)] for jobs that finished since last drain.

        Each job is returned at most once. Output is truncated to 500 chars.
        """
        with self._global_lock:
            jobs = list(self._jobs.values())
        result = []
        for job in jobs:
            if job.status == JobStatus.RUNNING:
                continue
            if getattr(job, "_drained", False):
                continue
            job._drained = True
            stdout = job.stdout_snapshot().strip()
            stderr = job.stderr_snapshot().strip()
            output = stdout
            if stderr:
                output += f"\n[STDERR]: {stderr}"
            output = output[:500] if output else "(no output)"
            result.append((job.job_id, output))
        return result

    # ── cleanup ──────────────────────────────────────────────────────────────

    def cleanup(self, max_age_secs: int = 300) -> int:
        cutoff = time.time() - max_age_secs
        retired = 0
        with self._global_lock:
            targets = [
                j for j in self._jobs.values()
                if not j.retired and j.status != JobStatus.RUNNING
                and j.end_time and j.end_time < cutoff
            ]
        for job in targets:
            with job._lock:
                job._stdout_lines = []
                job._stderr_lines = []
                job.retired = True
            retired += 1
        if retired:
            self._flush_registry()
        return retired

    # ── registry (cross-process task ledger) ─────────────────────────────────

    def _flush_registry(self) -> None:
        if not self._jobs_dir:
            return
        try:
            with self._registry_lock:
                self._write_registry()
        except Exception:
            pass

    def _write_registry(self) -> None:
        try:
            with self._global_lock:
                jobs = list(self._jobs.values())
            payload = {
                "owner_pid": os.getpid(),
                "updated_at": time.time(),
                "jobs": [
                    {
                        "job_id": j.job_id, "command": j.command, "pid": j.pid,
                        "status": j.status.value, "returncode": j.returncode,
                        "start_time": j.start_time, "end_time": j.end_time,
                        "reclaimed": j.reclaimed,
                    }
                    for j in jobs
                ],
            }
            path = self._jobs_dir / "index.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    def load_registry(self) -> int:
        if not self._jobs_dir:
            return 0
        path = self._jobs_dir / "index.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        if payload.get("owner_pid") == os.getpid():
            return 0
        claimed = 0
        for rec in payload.get("jobs", []) or []:
            try:
                job_id = str(rec.get("job_id") or "")
                pid = rec.get("pid")
                if not job_id or job_id in self._jobs:
                    continue
                if rec.get("status") != JobStatus.RUNNING.value:
                    continue
                if not _pid_alive(pid):
                    continue
                job = Job(
                    job_id=job_id, command=str(rec.get("command") or ""),
                    start_time=float(rec.get("start_time") or time.time()),
                    proc=None, pid=int(pid), reclaimed=True, retired=True,
                )
                with self._global_lock:
                    self._jobs[job_id] = job
                claimed += 1
            except Exception:
                continue
        if claimed:
            self._flush_registry()
        return claimed

    def cancel_all_running(self) -> int:
        count = 0
        with self._global_lock:
            running = [j for j in self._jobs.values() if j.status == JobStatus.RUNNING]
        for job in running:
            result = self.cancel(job.job_id)
            if result.get("cancelled"):
                count += 1
        self._flush_registry()
        return count


# ── Module-level singleton ──────────────────────────────────────────────────

_default_manager: Optional[AsyncJobManager] = None
_manager_lock = threading.Lock()


def get_manager() -> AsyncJobManager:
    """Lazily initialise the module-level AsyncJobManager singleton."""
    global _default_manager
    if _default_manager is None:
        with _manager_lock:
            if _default_manager is None:
                jobs_dir = None
                rd = os.environ.get("RUN_DIR") or os.environ.get("SENZA_AGENT_RUN_DIR")
                if rd:
                    jobs_dir = Path(rd) / "jobs"
                _default_manager = AsyncJobManager(jobs_dir=jobs_dir)
                try:
                    claimed = _default_manager.load_registry()
                    if claimed:
                        print(f"[jobs] reclaimed {claimed} background task(s) from previous process")
                except Exception:
                    pass
    return _default_manager
