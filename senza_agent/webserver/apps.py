"""User-space apps: ``.md`` files with YAML frontmatter + script body.

An app is stored as ``apps/<id>.md`` containing YAML-ish frontmatter
(name/icon/description/runtime/enabled/timeout) followed by the script
body. Supported runtimes: ``python``, ``shell``, ``powershell``.

This module mirrors the QevosAgent ``parseAppFile`` / ``runAppScript``
logic, adapted to Python.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Tuple

_APP_RUNTIMES = ("python", "powershell", "shell")


def _default_apps_dir() -> Path:
    """Return the default apps directory: ``~/.senza-agent/apps/``."""
    return Path.home() / ".senza-agent" / "apps"


def _apps_dir() -> Path:
    """Return the apps directory (env override supported)."""
    return Path(os.environ.get("APPS_DIR", str(_default_apps_dir())))


def parse_app_file(content: str) -> Tuple[dict[str, Any], str]:
    """Parse an app file into ``(meta, script_body)``.

    Mirrors ``dashboard/server.js:parseAppFile`` and
    ``agent/tools/standard.py:_parse_app_file``.
    """
    meta: dict[str, Any] = {
        "name": "",
        "icon": "📦",
        "description": "",
        "runtime": "shell",
        "enabled": True,
        "timeout": 120,
    }
    body = content

    m = re.match(r"^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$", content)
    if m:
        body = m.group(2) or ""
        for raw_line in (m.group(1) or "").splitlines():
            line = re.sub(r"#.*$", "", raw_line).strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and (
                (v[0] == '"' and v.endswith('"')) or (v[0] == "'" and v.endswith("'"))
            ):
                v = v[1:-1]
            if k == "enabled":
                meta["enabled"] = v.lower() not in ("false", "no", "0", "off")
            elif k == "timeout":
                try:
                    n = int(v)
                    if n > 0:
                        meta["timeout"] = n
                except ValueError:
                    pass
            elif k in meta:
                meta[k] = v

    # Strip a single wrapping ```lang ... ``` fence
    fence = re.match(r"^\s*```([a-zA-Z0-9_+-]*)\r?\n([\s\S]*?)\r?\n```\s*$", body)
    if fence:
        body = fence.group(2)
        lang = (fence.group(1) or "").lower()
        if lang in ("python", "py"):
            meta["runtime"] = "python"
        elif lang in ("powershell", "ps1", "ps"):
            meta["runtime"] = "powershell"
        elif lang in ("shell", "sh", "bash", "bat", "cmd"):
            meta["runtime"] = "shell"

    if meta["runtime"] not in _APP_RUNTIMES:
        meta["runtime"] = "shell"

    return meta, body.lstrip()


def list_apps() -> dict[str, Any]:
    """List all registered apps."""
    apps_dir = _apps_dir()
    if not apps_dir.exists():
        return {"apps": [], "apps_dir": str(apps_dir)}
    apps = []
    for p in sorted(apps_dir.glob("*.md")):
        try:
            meta, _ = parse_app_file(p.read_text(encoding="utf-8"))
            apps.append({
                "id": p.stem,
                "name": meta.get("name") or p.stem,
                "runtime": meta.get("runtime"),
                "description": meta.get("description", ""),
                "icon": meta.get("icon", "📦"),
                "enabled": meta.get("enabled", True),
            })
        except Exception:
            continue
    return {"apps": apps, "apps_dir": str(apps_dir)}


def get_app(app_id: str) -> dict[str, Any] | None:
    """Get a single app's raw content."""
    apps_dir = _apps_dir()
    fp = apps_dir / f"{app_id}.md"
    if not fp.is_file():
        return None
    return {"id": app_id, "content": fp.read_text(encoding="utf-8")}


def register_app(
    name: str,
    description: str,
    runtime: str,
    script: str,
    icon: str = "📦",
) -> dict[str, Any]:
    """Register a new app or update an existing one."""
    if runtime not in _APP_RUNTIMES:
        return {"error": f"runtime must be one of {_APP_RUNTIMES}"}

    app_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", name).strip("_") or f"app_{int(time.time())}"
    apps_dir = _apps_dir()
    try:
        apps_dir.mkdir(parents=True, exist_ok=True)

        def _q(s: str) -> str:
            return json.dumps(str(s), ensure_ascii=False)

        front = (
            "---\n"
            f"name: {_q(name)}\n"
            f"icon: {_q(icon or '📦')}\n"
            f"description: {_q(description or '')}\n"
            f"runtime: {runtime}\n"
            "enabled: true\n"
            "---\n\n"
        )
        (apps_dir / f"{app_id}.md").write_text(front + (script or ""), encoding="utf-8")
        return {
            "id": app_id,
            "name": name,
            "runtime": runtime,
            "message": f"App '{name}' registered.",
        }
    except Exception as e:
        return {"error": str(e)}


def delete_app(app_id: str) -> dict[str, Any]:
    """Delete an app by id."""
    apps_dir = _apps_dir()
    fp = apps_dir / f"{app_id}.md"
    if not fp.is_file():
        return {"error": f"app '{app_id}' not found"}
    try:
        fp.unlink()
        return {"ok": True}
    except OSError as e:
        return {"error": str(e)}


def run_app(
    app_id: str,
    run_args: Any = None,
) -> dict[str, Any]:
    """Execute an app's script and return the result.

    Returns ``{ok, code, stdout, stderr, durationMs, timedOut}``.
    """
    apps_dir = _apps_dir()
    fp = apps_dir / f"{app_id}.md"
    if not fp.is_file():
        available = [p.stem for p in apps_dir.glob("*.md")] if apps_dir.exists() else []
        return {"error": f"app '{app_id}' not found. Available: {available}"}

    meta, script = parse_app_file(fp.read_text(encoding="utf-8"))
    runtime = meta["runtime"]
    timeout_secs = int(meta.get("timeout", 120))

    is_win = sys.platform == "win32"

    if runtime == "python":
        ext = ".py"
        cmd_parts = [sys.executable]
    elif runtime == "powershell":
        ext = ".ps1"
        cmd_parts = ["powershell" if is_win else "pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    else:  # shell
        ext = ".bat" if is_win else ".sh"
        if is_win:
            cmd_parts = ["cmd", "/c"]
        else:
            cmd_parts = ["bash"]

    tmp = Path(tempfile.gettempdir()) / f"senza_app_{int(time.time() * 1000)}{ext}"
    try:
        tmp.write_text(script, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(e), "durationMs": 0, "timedOut": False}

    cmd_parts.append(str(tmp))
    t0 = time.time()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if run_args is not None:
        env["SENZA_RUN_ARGS"] = json.dumps(run_args)

    try:
        proc = subprocess.run(
            cmd_parts,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            env=env,
        )
        duration_ms = int((time.time() - t0) * 1000)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        # Cap output at 200KB
        if len(stdout) > 200_000:
            stdout = stdout[-200_000:]
        if len(stderr) > 200_000:
            stderr = stderr[-200_000:]
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "durationMs": duration_ms,
            "timedOut": False,
        }
    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - t0) * 1000)
        return {
            "ok": False,
            "code": -1,
            "stdout": "",
            "stderr": f"timed out after {timeout_secs}s",
            "durationMs": duration_ms,
            "timedOut": True,
        }
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        return {
            "ok": False,
            "code": -1,
            "stdout": "",
            "stderr": str(e),
            "durationMs": duration_ms,
            "timedOut": False,
        }
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
