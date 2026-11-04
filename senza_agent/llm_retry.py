"""Bounded retry for LLM streams cut short by the provider or gateway.

The Senza runtime surfaces a stream that ends without ``finish_reason`` as
``llm provider error: stream incomplete: N chunks received, finish_reason=...``.
It is fatal there: ``llm-harness-loop`` only retries idle timeouts
(``is_retryable`` covers RateLimit/Overloaded/Timeout), so one dropped SSE
connection ends the whole run.

Retrying is safe for the *turn* only when the failed attempt left no trace:
the runtime rolls the turn back (user message included) when the first LLM
call of the turn fails, and ``message_count()`` is unchanged.  If a tool had
already run, the history holds its results and re-prompting would duplicate
the user message and re-run side effects — so such failures are not retried.

Shared by the desktop task runner and the CLI so both surfaces recover the
same way.
"""
from __future__ import annotations

import os
from typing import Any

RETRIES_ENV = "SENZA_AGENT_STREAM_RETRIES"
DEFAULT_RETRIES = 2
MAX_RETRIES = 5

# Suffix of LlmError::StreamIncomplete's Display impl (llm-api-adapter
# src/error.rs), prefixed by the runtime with "llm provider error: ".
_MARKER = "stream incomplete:"


def retry_budget() -> int:
    """Per-turn retry count: ``SENZA_AGENT_STREAM_RETRIES`` clamped to 0..5."""
    raw = os.environ.get(RETRIES_ENV, "").strip()
    if not raw:
        return DEFAULT_RETRIES
    try:
        return max(0, min(int(raw), MAX_RETRIES))
    except ValueError:
        return DEFAULT_RETRIES


def is_interrupted_stream(message: str) -> bool:
    """True for ``LlmError::StreamIncomplete`` (not for other provider errors)."""
    return _MARKER in (message or "").lower()


def retry_delay_ms(attempt: int) -> int:
    """Backoff before retry number ``attempt`` (0-based): 1s, 2s, 4s, max 8s."""
    return min(1000 * (2 ** max(0, attempt)), 8000)


def conversation_size(harness: Any) -> int | None:
    """``harness.message_count()``, or None when the harness cannot report it.

    None means "cannot prove the turn was rolled back" — callers must not
    retry in that case.
    """
    counter = getattr(harness, "message_count", None)
    if not callable(counter):
        return None
    try:
        value = counter()
    except Exception:  # noqa: BLE001 - a probe, never fatal
        return None
    # Only a real count proves the turn was rolled back; anything else (an
    # older SDK, a test double) must not enable a retry.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
