"""Tests for TimeLedger."""
from __future__ import annotations

from senza_agent.behavior.timing import CATEGORIES, TimeLedger, fmt


class FakeClock:
    """A manually-advanced monotonic clock for deterministic timing tests."""

    def __init__(self, start: float = 0.0):
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


def test_snapshot_has_all_categories():
    ledger = TimeLedger()
    snap = ledger.snapshot()
    assert set(CATEGORIES).issubset(snap.keys())
    assert "total" in snap and "active" in snap and "untracked" in snap
    for cat in CATEGORIES:
        assert snap[cat] == 0.0


def test_start_tick_accumulates_total():
    clk = FakeClock()
    ledger = TimeLedger(clock=clk)
    ledger.start()
    clk.advance(2.0)
    delta = ledger.tick()
    assert delta == 2.0
    clk.advance(3.0)
    ledger.tick()
    assert ledger.total_seconds() == 5.0


def test_span_attributes_to_category():
    clk = FakeClock()
    ledger = TimeLedger(clock=clk)
    ledger.start()
    with ledger.span("llm"):
        clk.advance(1.5)
    clk.advance(0.5)
    ledger.tick()
    snap = ledger.snapshot()
    assert snap["llm"] == 1.5
    # total = span (1.5) + tick gap (0.5) = 2.0
    assert snap["total"] == 2.0


def test_span_still_books_on_exception():
    clk = FakeClock()
    ledger = TimeLedger(clock=clk)
    ledger.start()
    try:
        with ledger.span("tool"):
            clk.advance(2.0)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert ledger.snapshot()["tool"] == 2.0


def test_start_stop_category():
    clk = FakeClock()
    ledger = TimeLedger(clock=clk)
    ledger.start_category("llm")
    clk.advance(4.0)
    ledger.stop("llm")
    assert ledger.snapshot()["llm"] == 4.0


def test_stop_without_start_is_noop():
    ledger = TimeLedger()
    ledger.stop("llm")  # should not raise
    assert ledger.snapshot()["llm"] == 0.0


def test_add_rejects_unknown_category_and_nonpositive():
    ledger = TimeLedger()
    ledger.add("bogus", 10.0)
    ledger.add("llm", -1.0)
    ledger.add("llm", 0.0)
    assert ledger.snapshot()["llm"] == 0.0


def test_pause_resume_freezes_total():
    clk = FakeClock()
    ledger = TimeLedger(clock=clk)
    ledger.start()
    clk.advance(1.0)
    ledger.tick()
    ledger.pause()
    clk.advance(100.0)  # paused time should NOT count
    ledger.tick()  # no-op while paused
    assert ledger.total_seconds() == 1.0
    ledger.resume()
    clk.advance(2.0)
    ledger.tick()
    assert ledger.total_seconds() == 3.0


def test_active_excludes_paused_absorb():
    clk = FakeClock()
    ledger = TimeLedger(clock=clk)
    ledger.start()
    clk.advance(5.0)
    ledger.tick()
    # absorb_pause uses the wall clock, not the injected one; we can't easily
    # control it, so just assert the invariant active == total - paused.
    snap_before = ledger.snapshot()
    # Force a tiny pause contribution by absorbing (likely 0 in fast tests).
    ledger.absorb_pause()
    snap_after = ledger.snapshot()
    assert snap_after["active"] == round(snap_after["total"] - snap_after["paused"], 3)
    assert snap_after["active"] >= snap_before["active"]


def test_rate_and_recent_rate():
    clk = FakeClock()
    ledger = TimeLedger(clock=clk)
    ledger.start()
    for i in range(1, 6):
        with ledger.span("llm"):
            clk.advance(1.0)
        clk.advance(1.0)  # unattributed
        ledger.tick()
        ledger.sample(i)
    r = ledger.rate(5)
    assert r["per_iter"] == round(10.0 / 5, 2)  # 2s per iter * 5
    rr = ledger.recent_rate(window=3)
    assert rr["rounds"] >= 1
    assert rr["per_iter"] > 0


def test_fmt():
    assert fmt(0) == "0s"
    assert fmt(45) == "45s"
    assert fmt(60) == "1m"
    assert fmt(90) == "1m30s"
    assert fmt(3600) == "1h00m"
    assert fmt(3661) == "1h01m"
    assert fmt(-5) == "0s"
