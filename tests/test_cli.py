"""Tests for the senza-agent CLI."""
from __future__ import annotations

import subprocess
import sys


def test_cli_help():
    """``--help`` exits 0 and lists all expected flags."""
    result = subprocess.run(
        [sys.executable, "-m", "senza_agent.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--inspect" in result.stdout
    assert "--resume" in result.stdout
    assert "--nostop" in result.stdout
    assert "--skills" in result.stdout


def test_cli_help_no_inspect():
    """``--no-inspect`` flag is present in help output."""
    result = subprocess.run(
        [sys.executable, "-m", "senza_agent.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--no-inspect" in result.stdout


def test_cli_help_model():
    """``-m / --model`` flag is present in help output."""
    result = subprocess.run(
        [sys.executable, "-m", "senza_agent.cli", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--model" in result.stdout
    assert "--agents-profile" in result.stdout
    assert "--advisor-profile" in result.stdout
