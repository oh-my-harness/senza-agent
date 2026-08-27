"""Web UI tool wrappers for the Senza agent.

These tools call the senza-agent web server (port 8090) over HTTP to provide:
- ``web_show`` / ``web_notify`` — render panels and chat notifications
- ``web_interact`` — browser automation (screenshot, click, fill, etc.)
- ``terminal_open`` / ``terminal_run`` / ``terminal_send`` / ``terminal_read``
  / ``terminal_list`` — PTY terminal sessions
- ``file_tab`` — manage file browser tabs
- ``register_app`` / ``list_apps`` / ``run_app`` — user-space apps

All tools return ``{"status": "ok"|"error", "output": ..., "error": ...}`` dicts,
matching the convention used by ``senza_agent.tools.standard``.

The web server port defaults to 8090 and can be overridden via the
``SENZA_WEB_PORT`` environment variable.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from senza_agent.tools.registry import _adapt


# ══ Helpers ════════════════════════════════════════════════════════════════

def _web_port() -> str:
    """Return the web server port (env override supported)."""
    return os.environ.get("SENZA_WEB_PORT", "8090")


def _web_url(path: str = "") -> str:
    return f"http://localhost:{_web_port()}{path}"


def _api(
    method: str,
    path: str,
    body: Any = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Call the web server API. Returns parsed JSON dict."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        _web_url(path),
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")
        try:
            return json.loads(err_body)
        except (json.JSONDecodeError, ValueError):
            return {"error": f"HTTP {e.code}: {err_body}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _ok(output: Any = None, **extra) -> dict[str, Any]:
    """Build a success result matching standard.py contract."""
    result = {"status": "ok"}
    if output is not None or not extra:
        result["output"] = output
    result.update(extra)
    return result


def _err(error: str, **extra) -> dict[str, Any]:
    """Build an error result matching standard.py contract."""
    result = {"status": "error", "error": error}
    result.update(extra)
    return result


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "").replace("\r\n", "\n").replace("\r", "")


def _run_dir() -> Optional[str]:
    """Return the current run directory from env."""
    return os.environ.get("RUN_DIR")


# ══ web_show ═══════════════════════════════════════════════════════════════

def tool_web_show(
    content: str = "",
    content_type: str = "html",
    display_id: str = "default",
    title: str = "",
    mode: str = "replace",
) -> dict[str, Any]:
    """Display content in a web panel. The browser shows it in real-time.

    content_type: html | markdown | table | chart | text | image
    mode: replace (overwrite) | append (add to existing)
    """
    if not content:
        return _err("content is required")

    # If content looks like a file path, try to read it
    content_resolved = _resolve_content(content, content_type)
    if content_resolved is not None:
        content = content_resolved

    result = _api("POST", "/api/show", {
        "content": content,
        "content_type": content_type,
        "display_id": display_id,
        "title": title,
        "mode": mode,
    })

    if "error" in result:
        return _err(result["error"])

    url = f"{_web_url()}/#panel-{display_id}"
    return _ok({
        "url": url,
        "display_id": display_id,
        "content_type": content_type,
        "port": _web_port(),
    })


def _resolve_content(content: str, content_type: str) -> Optional[str]:
    """If content is a file path, read and return its contents."""
    if content_type not in ("html", "markdown", "text", "chart", "image"):
        return None
    s = content.strip()
    if not s or "\n" in s or len(s) > 1024:
        return None
    if "<" in s and ">" in s:
        return None  # Looks like HTML content, not a path
    if not re.search(r"\.[A-Za-z0-9]{1,8}$", s):
        return None

    cand = s.strip('"').strip("'")
    p = Path(cand)
    if not p.is_absolute():
        run_dir = _run_dir()
        if run_dir:
            p = Path(run_dir) / cand

    try:
        if not p.is_file():
            return None
    except OSError:
        return None

    if content_type == "image":
        return str(p)  # The browser can load it via the file path

    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


# ══ web_notify ═════════════════════════════════════════════════════════════

def tool_web_notify(
    message: str = "",
    display_id: str = "*",
) -> dict[str, Any]:
    """Push a chat notification to the web panel (agent → user).

    display_id: target panel, "*" = all panels.
    """
    if not message:
        return _err("message is required")

    result = _api("POST", "/api/notify", {
        "message": message,
        "display_id": display_id,
    })

    if "error" in result:
        return _err(result["error"])

    return _ok({"message": message, "display_id": display_id})


# ══ web_interact ═══════════════════════════════════════════════════════════

def tool_web_interact(
    action: str = "",
    display_id: str = "default",
    payload: Optional[dict] = None,
) -> dict[str, Any]:
    """Perform browser automation: screenshot, click, fill, evaluate, etc.

    action: navigate | screenshot | click | fill | evaluate |
            getText | getHtml | exists | waitFor
    payload: action-specific parameters (e.g. {selector, value, url, code}).
    """
    if not action:
        return _err("action is required")

    result = _api("POST", "/api/browser-action", {
        "action": action,
        "payload": payload or {},
        "display_id": display_id,
    }, timeout=30.0)

    if "error" in result:
        return _err(result["error"])

    return _ok(result)


# ══ Terminal tools ═════════════════════════════════════════════════════════

def tool_terminal_open(title: str = "Agent") -> dict[str, Any]:
    """Create a new shared terminal session. The browser shows a tab for it.

    Returns {id, title, port}.
    """
    result = _api("POST", "/api/term", {"title": title})
    if "error" in result:
        return _err(result["error"])

    sid = result.get("id")
    # Verify registration
    check = _api("GET", "/api/term")
    registered = any(s.get("id") == sid for s in (check.get("sessions") or []))

    return _ok({
        "id": sid,
        "title": result.get("title"),
        "port": _web_port(),
        "registered": registered,
    })


def tool_terminal_list() -> dict[str, Any]:
    """List all terminal sessions (id / title / owner / alive)."""
    result = _api("GET", "/api/term")
    if "error" in result:
        return _err(result["error"])
    return _ok(result.get("sessions", []))


def tool_terminal_send(
    id: str = "",
    text: str = "",
    submit: bool = True,
) -> dict[str, Any]:
    """Send text to a terminal session (submit=True appends Enter)."""
    if not id:
        return _err("id is required")
    data = text + ("\r" if submit else "")
    result = _api("POST", f"/api/term/{id}/input", {"data": data})
    if "error" in result:
        return _err(result["error"])
    return _ok({"sent": True, "seq": result.get("seq")})


def tool_terminal_read(id: str = "", since: int = 0) -> dict[str, Any]:
    """Read terminal output since offset. Returns {output, seq, owner, alive}."""
    if not id:
        return _err("id is required")
    result = _api("GET", f"/api/term/{id}/output?since={int(since)}")
    if "error" in result:
        return _err(result["error"])
    return _ok({
        "output": result.get("data", ""),
        "seq": result.get("seq"),
        "owner": result.get("owner"),
        "alive": result.get("alive"),
    })


def tool_terminal_run(
    id: str = "",
    command: str = "",
    timeout: int = 60,
) -> dict[str, Any]:
    """Execute a command in a terminal session and wait for completion.

    Returns {output, exit_code}. The command runs in the shared terminal so
    the user can see it in real-time. Session state (cd, env) persists.
    """
    if not id:
        return _err("id is required")
    if not command:
        return _err("command is required")

    timeout = int(timeout) if timeout and int(timeout) > 0 else 60
    token = uuid.uuid4().hex[:12]

    # Get current output position
    base = _api("GET", f"/api/term/{id}/output?since=0")
    if "error" in base:
        return _err(base["error"])
    since = base.get("seq", 0)

    # Claim ownership
    _api("POST", f"/api/term/{id}/owner", {"who": "agent"})

    try:
        # Wrap command with a sentinel marker to detect completion
        if os.name == "nt":
            wrapped = f'{command}; Write-Output "<<DONE:{token}:${{LASTEXITCODE}}>>"\r'
        else:
            wrapped = f'{command}; echo "<<DONE:{token}:$?>>"\r'

        snd = _api("POST", f"/api/term/{id}/input", {"data": wrapped})
        if "error" in snd:
            return _err(snd["error"])

        marker = re.compile(r"<<DONE:" + token + r":(-?\d*)>>")
        deadline = time.time() + timeout
        acc = ""
        code = None
        done = False

        while time.time() < deadline:
            r = _api("GET", f"/api/term/{id}/output?since={since}")
            if "error" in r:
                return _err(r["error"])
            if r.get("data"):
                acc += r["data"]
                since = r.get("seq", since)
            m = marker.search(acc)
            if m:
                code = int(m.group(1)) if m.group(1) not in (None, "") else None
                acc = acc[:m.start()]
                done = True
                break
            if not r.get("alive", True):
                break
            time.sleep(0.4)

        clean = _strip_ansi(acc)
        # Drop the echoed wrapped-command line
        clean = "\n".join(ln for ln in clean.split("\n") if token not in ln).strip("\n")

        if not done:
            return _err(
                "command timed out (terminal still running, use terminal_read to continue)",
                output={"output": clean, "exit_code": None, "timed_out": True},
            )
        return _ok({"output": clean, "exit_code": code})
    finally:
        _api("POST", f"/api/term/{id}/owner", {"who": "user"})


# ══ file_tab ═══════════════════════════════════════════════════════════════

def tool_file_tab(
    action: str = "",
    path: str = "",
    label: str = "",
) -> dict[str, Any]:
    """Manage file browser tabs in the web UI.

    action: open (open/activate a dir tab) | close | list
    """
    if action not in ("open", "close", "list"):
        return _err(f"unknown action: {action}. Use: open, close, list")

    if action in ("open", "close") and not path:
        return _err(f"action={action} requires a path")

    if action == "list":
        result = _api("GET", "/api/fs/roots")
        if "error" in result:
            return _err(result["error"])
        return _ok({"roots": result.get("roots", []), "home": result.get("home")})

    # For open/close, we just verify the path exists and return info
    p = Path(path)
    if action == "open" and not p.is_dir():
        return _err(f"path does not exist or is not a directory: {path}")

    return _ok({"action": action, "path": path, "label": label or p.name})


# ══ Apps ═══════════════════════════════════════════════════════════════════

def tool_register_app(
    name: str = "",
    description: str = "",
    runtime: str = "",
    script: str = "",
    icon: str = "📦",
) -> dict[str, Any]:
    """Register a script as a clickable app in the web UI.

    runtime: python | shell | powershell
    script: the script body (plain code, no ``` fences).
    """
    if not name:
        return _err("name is required")
    if not runtime:
        return _err("runtime is required")
    if not script:
        return _err("script is required")

    result = _api("POST", f"/api/app/{name}", {
        "name": name,
        "description": description,
        "runtime": runtime,
        "script": script,
        "icon": icon,
    })
    if "error" in result:
        return _err(result["error"])
    return _ok(result)


def tool_list_apps() -> dict[str, Any]:
    """List all registered apps."""
    result = _api("GET", "/api/apps")
    if "error" in result:
        return _err(result["error"])
    return _ok(result.get("apps", []))


def tool_run_app(name: str = "") -> dict[str, Any]:
    """Run a registered app by name or id."""
    if not name:
        return _err("name is required")
    target = name[:-3] if name.endswith(".md") else name
    result = _api("POST", f"/api/app/{target}/run", {})
    if "error" in result:
        return _err(result["error"])
    return _ok(result)


# ══ Tool registration ══════════════════════════════════════════════════════

def _str_schema(props: dict, required: list | None = None) -> dict:
    """Build a JSON Schema dict from {name: description} pairs."""
    properties = {name: {"type": "string", "description": desc} for name, desc in props.items()}
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _mixed_schema(props: dict, required: list | None = None) -> dict:
    """Build a JSON Schema dict from {name: {type, description}} pairs."""
    properties = {}
    for name, spec in props.items():
        if isinstance(spec, str):
            properties[name] = {"type": "string", "description": spec}
        elif isinstance(spec, dict):
            entry = dict(spec)
            entry.setdefault("type", "string")
            properties[name] = entry
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def get_web_ui_tools() -> list:
    """Return the list of Senza Tool objects for web UI tools.

    Imports ``senza`` lazily so this module can be imported even when the
    SDK is not available (e.g. in test environments).
    """
    import senza

    tools = []

    # ── Render ────────────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="web_show",
        description="Display content in a web panel (HTML, markdown, chart, table, image, text). The browser shows it in real-time.",
        parameters=_mixed_schema({
            "content": {"type": "string", "description": "Content to display (or a file path to read from)"},
            "content_type": {"type": "string", "description": "(optional) html | markdown | table | chart | text | image, default html"},
            "display_id": {"type": "string", "description": "(optional) panel id, default 'default'"},
            "title": {"type": "string", "description": "(optional) panel title"},
            "mode": {"type": "string", "description": "(optional) replace | append, default replace"},
        }, ["content"]),
        callback=_adapt(tool_web_show),
    ))

    tools.append(senza.create_tool(
        name="web_notify",
        description="Push a chat notification to the web panel (agent → user).",
        parameters=_str_schema({
            "message": "The message to send",
            "display_id": "(optional) target panel id, '*' = all (default)",
        }, ["message"]),
        callback=_adapt(tool_web_notify),
    ))

    tools.append(senza.create_tool(
        name="web_interact",
        description="Perform browser automation: screenshot, click, fill, evaluate, navigate, etc.",
        parameters=_mixed_schema({
            "action": {"type": "string", "description": "navigate | screenshot | click | fill | evaluate | getText | getHtml | exists | waitFor"},
            "display_id": {"type": "string", "description": "(optional) target view, default 'default'"},
            "payload": {"type": "object", "description": "(optional) action-specific parameters: {url, selector, value, code, timeout}"},
        }, ["action"]),
        callback=_adapt(tool_web_interact),
    ))

    # ── Terminal ──────────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="terminal_open",
        description="Create a new shared terminal session. The browser shows a tab for it.",
        parameters=_str_schema({
            "title": "(optional) terminal title, default 'Agent'",
        }),
        callback=_adapt(tool_terminal_open),
    ))

    tools.append(senza.create_tool(
        name="terminal_run",
        description="Execute a command in a terminal session and wait for completion. Returns {output, exit_code}.",
        parameters=_mixed_schema({
            "id": {"type": "string", "description": "Terminal session id (from terminal_open)"},
            "command": {"type": "string", "description": "Command to execute"},
            "timeout": {"type": "integer", "description": "(optional) max wait seconds, default 60"},
        }, ["id", "command"]),
        callback=_adapt(tool_terminal_run),
    ))

    tools.append(senza.create_tool(
        name="terminal_send",
        description="Send text to a terminal session without waiting for output.",
        parameters=_mixed_schema({
            "id": {"type": "string", "description": "Terminal session id"},
            "text": {"type": "string", "description": "Text to send"},
            "submit": {"type": "boolean", "description": "(optional) append Enter, default true"},
        }, ["id", "text"]),
        callback=_adapt(tool_terminal_send),
    ))

    tools.append(senza.create_tool(
        name="terminal_read",
        description="Read terminal output since a given offset. Returns {output, seq, owner, alive}.",
        parameters=_mixed_schema({
            "id": {"type": "string", "description": "Terminal session id"},
            "since": {"type": "integer", "description": "(optional) char offset, default 0"},
        }, ["id"]),
        callback=_adapt(tool_terminal_read),
    ))

    tools.append(senza.create_tool(
        name="terminal_list",
        description="List all terminal sessions (id / title / owner / alive).",
        parameters=_str_schema({}),
        callback=_adapt(tool_terminal_list),
    ))

    # ── File browser ──────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="file_tab",
        description="Manage file browser tabs: open a directory, close a tab, or list roots.",
        parameters=_str_schema({
            "action": "open | close | list",
            "path": "Directory path (required for open/close)",
            "label": "(optional) tab label",
        }, ["action"]),
        callback=_adapt(tool_file_tab),
    ))

    # ── Apps ──────────────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="register_app",
        description="Register a script as a clickable app in the web UI.",
        parameters=_mixed_schema({
            "name": {"type": "string", "description": "App name"},
            "description": {"type": "string", "description": "One-line description"},
            "runtime": {"type": "string", "description": "python | shell | powershell"},
            "script": {"type": "string", "description": "Script body (plain code, no fences)"},
            "icon": {"type": "string", "description": "(optional) emoji icon, default 📦"},
        }, ["name", "description", "runtime", "script"]),
        callback=_adapt(tool_register_app),
    ))

    tools.append(senza.create_tool(
        name="list_apps",
        description="List all registered apps.",
        parameters=_str_schema({}),
        callback=_adapt(tool_list_apps),
    ))

    tools.append(senza.create_tool(
        name="run_app",
        description="Run a registered app by name or id.",
        parameters=_str_schema({"name": "App id or name (without .md)"}, ["name"]),
        callback=_adapt(tool_run_app),
    ))

    return tools
