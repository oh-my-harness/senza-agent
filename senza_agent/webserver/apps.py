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

_APP_RUNTIMES = ("python", "powershell", "shell", "web")


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
        "entry": "",
        "sidecar": "",
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


def get_app_panel_html(app_id: str, root: str = "") -> str | None:
    """Return the HTML for a ``runtime: web`` app panel.

    Reads the app ``.md`` file, extracts the HTML body (or the ``entry`` file
    from the project folder), and wraps it with:
    - ``window.__QEVOS__`` config injection
    - ``/qevos-theme.css`` stylesheet link
    - ``/qevos-bridge.js`` script tag

    Returns None if the app doesn't exist or isn't a web app.
    """
    apps_dir = _apps_dir()
    fp = apps_dir / f"{app_id}.md"
    if not fp.is_file():
        return None
    meta, body = parse_app_file(fp.read_text(encoding="utf-8"))
    if meta.get("runtime") != "web":
        return None

    # If entry file is specified, read from project folder
    entry = meta.get("entry")
    if entry and root:
        entry_path = Path(root) / entry
        if entry_path.is_file():
            body = entry_path.read_text(encoding="utf-8")

    # Build the __QEVOS__ config
    config_parts = [f'"app": {json.dumps(app_id)}']
    if root:
        config_parts.append(f'"root": {json.dumps(root)}')
    qevos_config = "window.__QEVOS__ = {" + ", ".join(config_parts) + "};"

    # Assemble the full HTML document
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>{qevos_config}</script>
<link rel="stylesheet" href="/qevos-theme.css">
</head>
<body>
{body}
<script src="/qevos-bridge.js"></script>
</body>
</html>"""
    return html


def _app_data_dir(app_id: str, root: str = "") -> Path:
    """Return the data directory for an app.

    If ``root`` is set, files are scoped to that project folder.
    Otherwise, files go to ``~/.senza-agent/app-data/<app_id>/``.
    """
    if root:
        return Path(root)
    return Path.home() / ".senza-agent" / "app-data" / app_id


def read_app_file(app_id: str, rel_path: str, root: str = "") -> dict[str, Any]:
    """Read a file from an app's data directory."""
    base = _app_data_dir(app_id, root)
    fp = (base / rel_path).resolve()
    # Prevent path traversal
    if not str(fp).startswith(str(base.resolve())):
        return {"error": "path traversal denied"}
    if not fp.is_file():
        return {"error": "not found", "exists": False}
    try:
        content = fp.read_text(encoding="utf-8")
        return {"content": content, "exists": True}
    except Exception as e:
        return {"error": str(e)}


def read_app_file_binary(app_id: str, rel_path: str, root: str = "") -> bytes | None:
    """Read a file as raw bytes from an app's data directory."""
    base = _app_data_dir(app_id, root)
    fp = (base / rel_path).resolve()
    if not str(fp).startswith(str(base.resolve())):
        return None
    if not fp.is_file():
        return None
    return fp.read_bytes()


def write_app_file(app_id: str, rel_path: str, content: str = "", content_b64: str = "", root: str = "") -> dict[str, Any]:
    """Write a file to an app's data directory."""
    base = _app_data_dir(app_id, root)
    fp = (base / rel_path).resolve()
    if not str(fp).startswith(str(base.resolve())):
        return {"error": "path traversal denied"}
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        if content_b64:
            import base64
            fp.write_bytes(base64.b64decode(content_b64))
        else:
            fp.write_text(content, encoding="utf-8")
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def delete_app_file(app_id: str, rel_path: str, root: str = "") -> dict[str, Any]:
    """Delete a file from an app's data directory."""
    base = _app_data_dir(app_id, root)
    fp = (base / rel_path).resolve()
    if not str(fp).startswith(str(base.resolve())):
        return {"error": "path traversal denied"}
    if not fp.is_file():
        return {"error": "not found"}
    try:
        fp.unlink()
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def list_app_files(app_id: str, dir_path: str = "", root: str = "") -> dict[str, Any]:
    """List files in an app's data directory (recursive under dir)."""
    base = _app_data_dir(app_id, root)
    if dir_path:
        search_base = (base / dir_path).resolve()
        if not str(search_base).startswith(str(base.resolve())):
            return {"error": "path traversal denied"}
    else:
        search_base = base.resolve()
    if not search_base.is_dir():
        return {"files": []}
    files = []
    for p in sorted(search_base.rglob("*")):
        if p.is_file():
            rel = p.relative_to(base)
            files.append({
                "path": str(rel),
                "type": "file",
                "size": p.stat().st_size,
            })
    return {"files": files}
