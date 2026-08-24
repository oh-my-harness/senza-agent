"""aiohttp Web server for senza-agent.

Runs on port 8090 alongside the Inspector (port 8080). Provides:
- Render panels (web_show / web_notify) over WebSocket
- PTY terminal sessions (shareable between agent and browser)
- File browser (list / read / write / upload / zip)
- Browser automation (optional, via playwright)
- User-space apps (``.md`` files with YAML frontmatter + script body)
"""
from __future__ import annotations

from .app import WebServer

__all__ = ["WebServer"]
