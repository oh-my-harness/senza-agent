"""Tests for the acceptance gate."""
from __future__ import annotations

from pathlib import Path

from senza_agent.behavior.state import AgentState
from senza_agent.behavior.acceptance_gate import acceptance_validator


def test_validator_passes_when_no_report():
    # No report → accept (runtime breaks loop on reject, no retry).
    state = AgentState()
    state.turn_count = 1
    result = acceptance_validator(state)({})
    assert result is None


def test_validator_passes_when_report_submitted():
    state = AgentState()
    state.turn_count = 1  # simulate at least one tool turn
    state.completion_report = {"report": "done", "deliverables": ["file.py"]}
    result = acceptance_validator(state)({})
    assert result is None

def test_validator_checks_artifact_files(tmp_path):
    state = AgentState()
    state.turn_count = 1  # simulate at least one tool turn
    state.completion_report = {
        "outcome": "done",
        "evidence_type": "artifact",
        "evidence": [str(tmp_path / "nonexistent.py")],
    }
    result = acceptance_validator(state)({})
    assert result is not None  # should fail because file doesn't exist


def test_validator_passes_when_artifact_exists(tmp_path):
    artifact = tmp_path / "real.py"
    artifact.write_text("# done")
    state = AgentState()
    state.turn_count = 1  # simulate at least one tool turn
    state.completion_report = {
        "goal_understanding": "do the thing",
        "completed_work": ["wrote real.py"],
        "outcome": "done",
        "confidence": "high",
        "evidence_type": "artifact",
        "evidence": [str(artifact)],
    }
    result = acceptance_validator(state)({})
    assert result is None


def test_validator_weak_pass_for_partial_outcome():
    state = AgentState()
    state.turn_count = 1  # simulate at least one tool turn
    state.completion_report = {
        "goal_understanding": "do the thing",
        "completed_work": ["partial"],
        "outcome": "done_partial",
        "confidence": "medium",
        "evidence_type": "none",
        "evidence": [],
    }
    result = acceptance_validator(state)({})
    assert result is None  # weak_pass is acceptable
