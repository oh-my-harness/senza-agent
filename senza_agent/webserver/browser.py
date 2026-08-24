"""Browser automation via Playwright (optional dependency).

Provides screenshot, click, fill, evaluate, and navigation actions for
browser views opened via ``web_show``. Playwright is imported lazily so
the web server starts even without it installed.
"""
from __future__ import annotations

import base64
import time
from typing import Any, Optional


class BrowserController:
    """Controls a headless browser via Playwright.

    If Playwright is not installed, all actions return an error dict
    instead of raising — the web server still starts and other features
    (render, terminal, files, apps) work fine.
    """

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._error: Optional[str] = None

    def _ensure_playwright(self) -> bool:
        """Lazily import and launch Playwright. Returns True if available."""
        if self._playwright is not None:
            return True
        if self._error is not None:
            return False
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
            return True
        except ImportError:
            self._error = "playwright is not installed (pip install playwright && playwright install chromium)"
            return False
        except Exception as e:
            self._error = f"playwright launch failed: {e}"
            return False

    def navigate(self, url: str) -> dict[str, Any]:
        if not self._ensure_playwright():
            return {"error": self._error}
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return {"ok": True, "url": self._page.url, "title": self._page.title()}
        except Exception as e:
            return {"error": str(e)}

    def screenshot(self, full_page: bool = True) -> dict[str, Any]:
        if not self._ensure_playwright():
            return {"error": self._error}
        try:
            data = self._page.screenshot(full_page=full_page)
            b64 = base64.b64encode(data).decode("ascii")
            return {"ok": True, "data": b64, "mime": "image/png"}
        except Exception as e:
            return {"error": str(e)}

    def click(self, selector: str) -> dict[str, Any]:
        if not self._ensure_playwright():
            return {"error": self._error}
        try:
            self._page.click(selector, timeout=10000)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def fill(self, selector: str, value: str) -> dict[str, Any]:
        if not self._ensure_playwright():
            return {"error": self._error}
        try:
            self._page.fill(selector, value, timeout=10000)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def evaluate(self, expression: str) -> dict[str, Any]:
        if not self._ensure_playwright():
            return {"error": self._error}
        try:
            result = self._page.evaluate(expression)
            return {"ok": True, "result": result}
        except Exception as e:
            return {"error": str(e)}

    def get_text(self, selector: str) -> dict[str, Any]:
        if not self._ensure_playwright():
            return {"error": self._error}
        try:
            text = self._page.text_content(selector, timeout=10000)
            return {"ok": True, "text": text or ""}
        except Exception as e:
            return {"error": str(e)}

    def get_html(self, selector: str = "body") -> dict[str, Any]:
        if not self._ensure_playwright():
            return {"error": self._error}
        try:
            if selector == "body":
                html = self._page.content()
            else:
                el = self._page.query_selector(selector)
                html = el.inner_html() if el else ""
            return {"ok": True, "html": html}
        except Exception as e:
            return {"error": str(e)}

    def exists(self, selector: str) -> dict[str, Any]:
        if not self._ensure_playwright():
            return {"error": self._error}
        try:
            el = self._page.query_selector(selector)
            return {"ok": True, "exists": el is not None}
        except Exception as e:
            return {"error": str(e)}

    def wait_for(self, selector: str, timeout_ms: int = 10000) -> dict[str, Any]:
        if not self._ensure_playwright():
            return {"error": self._error}
        try:
            self._page.wait_for_selector(selector, timeout=timeout_ms)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    def perform_action(
        self,
        action: str,
        payload: dict[str, Any],
        display_id: str = "default",
    ) -> dict[str, Any]:
        """Dispatch a browser action by name.

        Args:
            action: navigate | screenshot | click | fill | evaluate |
                    getText | getHtml | exists | waitFor
            payload: Action-specific parameters.
            display_id: Unused for now (reserved for multi-view support).
        """
        if action == "navigate":
            return self.navigate(payload.get("url", ""))
        if action == "screenshot":
            return self.screenshot(full_page=payload.get("full_page", True))
        if action == "click":
            return self.click(payload.get("selector", ""))
        if action == "fill":
            return self.fill(payload.get("selector", ""), payload.get("value", ""))
        if action == "evaluate":
            return self.evaluate(payload.get("code", ""))
        if action == "getText":
            return self.get_text(payload.get("selector", "body"))
        if action == "getHtml":
            return self.get_html(payload.get("selector", "body"))
        if action == "exists":
            return self.exists(payload.get("selector", ""))
        if action == "waitFor":
            return self.wait_for(payload.get("selector", ""), int(payload.get("timeout", 10000)))
        return {"error": f"unknown action: {action}"}

    def shutdown(self) -> None:
        """Clean up browser resources."""
        try:
            if self._page:
                self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
