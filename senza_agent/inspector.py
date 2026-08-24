"""Inspector integration — mounts the runtime Inspector Web API via PyO3.

Inspector is on by default (port 8080). Use --no-inspect to disable.
"""
from __future__ import annotations

import sys
from typing import Any, Optional


def setup_inspector(harness: Any, port: int = 8080) -> Optional[Any]:
    """Mount the Inspector Web API on the given harness.

    Call after create_agent() returns the harness, before prompt_and_collect().
    Returns an Inspector handle — keep it alive for the duration of the run.
    Dropping or calling .shutdown() stops the HTTP server.

    Usage:
        inspector = setup_inspector(harness, port=8080)
        # ... run agent ...
        if inspector:
            inspector.shutdown()
    """
    try:
        inspector = harness.mount_inspector(port)
        addr = inspector.bound_addr() if hasattr(inspector, "bound_addr") else None
        url = f"http://{addr}" if addr else f"http://127.0.0.1:{port}"
        print(f"Inspector running at {url}", file=sys.stderr)
        print(f"  Open this URL in your browser to interact.", file=sys.stderr)
        return inspector
    except Exception as e:
        print(f"Warning: Inspector mount failed: {e}", file=sys.stderr)
        return None


def try_open_browser(url: str) -> None:
    """Try to open the browser automatically."""
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass
