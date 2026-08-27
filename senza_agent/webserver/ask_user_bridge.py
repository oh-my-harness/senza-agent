"""AskUserBridge: blocking bridge between agent thread and web server thread.

When the agent calls ``tool_ask_user(question)``, the tool blocks on a
``threading.Event``.  The web server, upon receiving the user's answer via
``POST /api/inject``, calls ``provide_answer(text)`` which sets the answer
and signals the event, unblocking the tool.

This module holds a process-global singleton so both the tool (in the agent
thread) and the web server (in the asyncio thread) can reach it.
"""
from __future__ import annotations

import threading
import logging
from typing import Optional


class AskUserBridge:
    """Thread-safe bridge for ask_user tool ↔ web server communication."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._answer: Optional[str] = None
        self._question: Optional[str] = None
        self._active: bool = False
        self._lock = threading.Lock()

    @property
    def is_active(self) -> bool:
        """True when an ask_user call is blocking, waiting for an answer."""
        with self._lock:
            return self._active

    @property
    def question(self) -> Optional[str]:
        """The question the agent is currently blocked on, if any."""
        with self._lock:
            return self._question

    def ask(self, question: str) -> str:
        """Block the calling (agent) thread until the user answers.

        Returns the user's answer text, or an empty string if interrupted
        (e.g. agent aborted).
        """
        with self._lock:
            self._active = True
            self._question = question
            self._answer = None
            self._event.clear()
        logging.getLogger(__name__).info("[bridge.ask] question=%r _active=True, waiting...", question[:80])

        # Block until the web server calls provide_answer() or reset().
        self._event.wait()

        logging.getLogger(__name__).info("[bridge.ask] unblocked, answer=%r", "<empty>" if not (self._answer or "") else (self._answer or "")[:80])

        with self._lock:
            self._active = False
            self._question = None
            answer = self._answer or ""
            self._answer = None
            return answer

    def provide_answer(self, text: str) -> bool:
        """Provide the user's answer. Returns True if the answer was accepted.

        Returns False if no ask_user is currently active (the caller should
        then treat the input as a normal follow-up / new task).
        """
        with self._lock:
            if not self._active:
                logging.getLogger(__name__).info("[bridge.provide_answer] NOT ACTIVE, rejected")
                return False
            self._answer = text
        logging.getLogger(__name__).info("[bridge.provide_answer] accepted answer=%r", text[:80])
        self._event.set()
        return True

    def reset(self) -> None:
        """Interrupt any pending ask_user (e.g. on abort / shutdown)."""
        with self._lock:
            self._active = False
            self._question = None
            self._answer = None
        self._event.set()


# Process-global singleton
_bridge = AskUserBridge()


def get_bridge() -> AskUserBridge:
    """Return the global AskUserBridge instance."""
    return _bridge
