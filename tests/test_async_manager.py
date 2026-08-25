"""Tests for AsyncJobManager.drain_completed."""
import time

from senza_agent.tools.async_manager import AsyncJobManager


def test_drain_completed_returns_empty_when_no_jobs():
    mgr = AsyncJobManager()
    assert mgr.drain_completed() == []


def test_drain_completed_returns_finished_job():
    mgr = AsyncJobManager()
    job_id = mgr.start_shell("echo hello")
    # Wait for completion (echo is near-instant)
    for _ in range(50):
        info = mgr.peek(job_id, wait_secs=0.1)
        if info["status"] != "running":
            break
    result = mgr.drain_completed()
    assert len(result) == 1
    assert result[0][0] == job_id
    assert "hello" in result[0][1]


def test_drain_completed_returns_each_job_only_once():
    mgr = AsyncJobManager()
    job_id = mgr.start_shell("echo world")
    for _ in range(50):
        info = mgr.peek(job_id, wait_secs=0.1)
        if info["status"] != "running":
            break
    first = mgr.drain_completed()
    second = mgr.drain_completed()
    assert len(first) == 1
    assert second == []


def test_drain_completed_truncates_long_output():
    mgr = AsyncJobManager()
    job_id = mgr.start_shell("python3 -c \"print('x' * 2000)\"")
    for _ in range(50):
        info = mgr.peek(job_id, wait_secs=0.1)
        if info["status"] != "running":
            break
    result = mgr.drain_completed()
    assert len(result) == 1
    assert len(result[0][1]) <= 500
