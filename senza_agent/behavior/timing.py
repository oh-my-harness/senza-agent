"""
Time ledger with an injectable clock.

Ports QevosAgent's timing module to a self-contained class: instead of reading
from / writing to a shared ``state.meta`` dict, the ledger keeps its own book
in instance attributes. The design is otherwise preserved:

1. **Categorized, not a single number.** Only when time is split into
   categories does "the environment is degrading" become a readable signal —
   an ``llm`` share climbing from 40% to 85% tells the agent "it's not me,
   it's the server".
2. **Accumulating bookkeeping, no absolute timestamps.** Lid closed, VM
   suspended, cross-process resume — absolute timestamps all go wrong. The one
   place a wall clock is used is pause absorption, where the process wasn't
   running at all and only the gap between the last persisted wall time and now
   can be recovered.
3. **Injectable clock.** Time-related bugs are the hardest to track down; tests
   must never need a real sleep.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Optional

# Categories: five mutually exclusive buckets; their sum is ≤ total, and the
# difference is unattributed overhead (parsing, bookkeeping, persistence, ...).
CATEGORIES = ("llm", "tool", "wait", "retry", "paused")

_SAMPLE_CAP = 24


def _wall_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_wall(text: object) -> Optional[datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


class TimeLedger:
    """Categorized timing ledger with an injectable monotonic clock.

    Usage::

        ledger = TimeLedger()
        ledger.start()
        with ledger.span("llm"):
            ...  # call the model
        ledger.snapshot()  # {"total": ..., "llm": ..., "tool": 0.0, ...}
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None):
        # Injectable monotonic clock; default to time.monotonic so NTP adjustments
        # and system clock changes never make already-booked time go backwards.
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._book: dict[str, float] = {"total": 0.0}
        for cat in CATEGORIES:
            self._book[cat] = 0.0
        self._mark: Optional[float] = None
        self._wall_seen: Optional[str] = None
        self._samples: list[list[float]] = []
        # Pause bookkeeping: when paused, the running mark is frozen so the
        # pause interval is not silently folded into `total`.
        self._paused: bool = False
        self._pause_started_at: Optional[float] = None

    # ── injectable clock ──────────────────────────────────────────────────────

    def set_clock(self, fn: Callable[[], float]) -> None:
        """Replace the monotonic clock (test-only)."""
        self._clock = fn

    def now(self) -> float:
        return self._clock()

    # ── process-level start / tick ────────────────────────────────────────────

    def start(self) -> None:
        """Begin or resume timing: reset this process's timing anchor.

        ``_mark`` is an in-process monotonic reading and is meaningless across
        processes — on resume it must be re-punched, otherwise the clock
        difference between two processes would be booked as this run's cost.
        """
        self._mark = self.now()
        self._wall_seen = _wall_now()

    def tick(self) -> float:
        """Advance ``total`` by the elapsed time since the last mark.

        ``total`` is *measured*; categories are *attributed*; the gap is
        therefore unattributed overhead — more honest than forcing the
        categories to sum to total.
        """
        if self._mark is None:
            self.start()
            return 0.0
        if self._paused:
            return 0.0
        current = self.now()
        delta = max(0.0, current - float(self._mark))
        self._book["total"] = float(self._book.get("total") or 0.0) + delta
        self._mark = current
        self._wall_seen = _wall_now()
        return delta

    # ── per-category accounting ───────────────────────────────────────────────

    def add(self, category: str, seconds: float) -> None:
        """Book seconds into a category.

        Note: ``paused`` must NOT go through here — it has to be added to
        ``total`` as well to preserve the invariant ``paused ≤ total``
        (otherwise ``active = total - paused`` would be clamped to 0). Pauses
        always go through :meth:`absorb_pause`.
        """
        if category not in CATEGORIES:
            return
        try:
            seconds = float(seconds)
        except Exception:
            return
        if seconds <= 0:
            return
        self._book[category] = float(self._book.get(category) or 0.0) + seconds

    @contextmanager
    def span(self, category: str):
        """Time a block and attribute it to ``category``.

        Exceptions propagate normally, but the time is still booked — a failed
        call took just as much time as a successful one.
        """
        started = self.now()
        try:
            yield
        finally:
            try:
                self.add(category, self.now() - started)
            except Exception:
                pass

    # ── pause / resume ────────────────────────────────────────────────────────

    def pause(self) -> None:
        """Freeze the running mark so the pause interval isn't booked into total."""
        if self._paused:
            return
        self._paused = True
        self._pause_started_at = self._mark

    def resume(self) -> None:
        """Resume after a in-process pause; re-punch the mark to now."""
        if not self._paused:
            return
        self._paused = False
        self._mark = self.now()
        self._pause_started_at = None

    def absorb_pause(self) -> float:
        """Recover "waiting for a human" time on resume.

        While paused the process isn't running at all, so the monotonic clock
        can't measure it — only the gap between the last persisted wall time
        and now can be recovered. This is the one place a wall clock is used,
        and only for this purpose.
        """
        seen = _parse_wall(self._wall_seen)
        if seen is None:
            return 0.0
        try:
            gap = (datetime.now(timezone.utc) - seen).total_seconds()
        except Exception:
            return 0.0
        if gap <= 0:
            return 0.0
        self._book["paused"] = float(self._book.get("paused") or 0.0) + gap
        self._book["total"] = float(self._book.get("total") or 0.0) + gap
        return gap

    # ── start/stop per-category convenience API ───────────────────────────────
    # The categorized start/stop API wraps `span` for callers that prefer
    # explicit begin/end over a context manager. Nested starts on the same
    # category are not supported — each start resets that category's anchor.

    def start_category(self, category: str) -> None:
        """Begin timing ``category``. Equivalent to entering ``span(category)``."""
        if category not in CATEGORIES:
            return
        # Store the start mark on the book under a private key.
        self._book[f"_start_{category}"] = self.now()

    def stop(self, category: str) -> None:
        """Stop timing ``category`` and book the elapsed time into it."""
        if category not in CATEGORIES:
            return
        key = f"_start_{category}"
        started = self._book.pop(key, None)
        if started is None:
            return
        self.add(category, self.now() - float(started))

    # ── derived quantities ────────────────────────────────────────────────────

    def total_seconds(self) -> float:
        return float(self._book.get("total") or 0.0)

    def active_seconds(self) -> float:
        """Work time excluding "waiting for a human".

        Run-level deadlines use ``total`` (a commitment to reality — the time
        spent suspended really was consumed). Graph-level budgets use
        ``active`` (a unit of work budget — 8 hours of someone sleeping
        shouldn't count).
        """
        return max(0.0, float(self._book.get("total") or 0.0) - float(self._book.get("paused") or 0.0))

    def snapshot(self) -> dict:
        """JSON-serializable ledger snapshot, for status.json / dashboards."""
        out: dict[str, float] = {"total": round(float(self._book.get("total") or 0.0), 3)}
        for key in CATEGORIES:
            out[key] = round(float(self._book.get(key) or 0.0), 3)
        out["active"] = round(self.active_seconds(), 3)
        tracked = sum(out[key] for key in CATEGORIES)
        out["untracked"] = round(max(0.0, out["total"] - tracked), 3)
        return out

    def rate(self, iterations: int) -> dict:
        """Whole-run average per-iteration time and LLM share."""
        if iterations <= 0:
            return {}
        total = float(self._book.get("total") or 0.0)
        return {
            "per_iter": round(total / iterations, 2),
            "llm_share": round(
                float(self._book.get("llm") or 0.0) / max(1e-9, total), 3
            ),
        }

    # ── rolling samples ───────────────────────────────────────────────────────
    # Only "recent" rates reveal environmental degradation: whole-run averages
    # dilute early smoothness, so an llm share climbing from 40% to 85% never
    # surfaces in the totals.

    def sample(self, iteration: int) -> None:
        """Record one sample point per iteration. All fields are float."""
        self._samples.append([
            int(iteration),
            round(float(self._book.get("total") or 0.0), 2),
            round(float(self._book.get("llm") or 0.0), 2),
            round(float(self._book.get("tool") or 0.0), 2),
            round(float(self._book.get("retry") or 0.0), 2),
        ])
        if len(self._samples) > _SAMPLE_CAP:
            del self._samples[: len(self._samples) - _SAMPLE_CAP]

    def recent_rate(self, window: int = 10) -> dict:
        """Per-iteration time and breakdown over the last ``window`` rounds.

        Returns ``{}`` when there are too few samples.
        """
        if len(self._samples) < 2:
            return {}
        tail = self._samples[-(window + 1):]
        first, last = tail[0], tail[-1]
        rounds = int(last[0]) - int(first[0])
        if rounds <= 0:
            return {}
        span = float(last[1]) - float(first[1])
        if span <= 0:
            return {}
        return {
            "rounds": rounds,
            "per_iter": round(span / rounds, 1),
            "llm": round((float(last[2]) - float(first[2])) / rounds, 1),
            "tool": round((float(last[3]) - float(first[3])) / rounds, 1),
            "retry": round((float(last[4]) - float(first[4])) / rounds, 1),
            "llm_share": round((float(last[2]) - float(first[2])) / span, 3),
        }


def fmt(seconds: float) -> str:
    """Human-readable duration — used for both model-facing and dashboard text
    so the two never drift apart."""
    try:
        seconds = max(0.0, float(seconds))
    except Exception:
        return "0s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s" if sec else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
