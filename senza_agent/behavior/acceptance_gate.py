"""Acceptance gate — 3-stage completion report review, ported from QevosAgent.

The gate runs inside a ``final_answer_validator`` hook. When the agent tries to
emit a final answer, the validator checks whether a structured completion report
was submitted (via the ``submit_completion_report`` tool) and, when artifact
evidence is claimed, that those files actually exist on disk. Failing the gate
returns an error string that is fed back to the model so it can remedy the gap.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional

if TYPE_CHECKING:
    from senza_agent.behavior.state import AgentState


# ── report normalization ────────────────────────────────────────────────────

_REQUIRED_FIELDS = (
    "goal_understanding",
    "completed_work",
    "outcome",
    "confidence",
)

_VALID_EVIDENCE_TYPES = {"artifact", "tool_result", "observation", "none"}
_VALID_OUTCOMES = {"done", "done_partial", "done_blocked"}
_VALID_CONFIDENCES = {"low", "medium", "high"}


def _listify(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _normalize_completion_report(report: Optional[dict]) -> dict:
    """Normalize a raw completion report dict into the canonical structure."""
    data = dict(report or {})

    evidence_type = str(data.get("evidence_type", "none")).strip().lower() or "none"
    if evidence_type not in _VALID_EVIDENCE_TYPES:
        evidence_type = "none"

    outcome = str(data.get("outcome", "done")).strip().lower() or "done"
    if outcome not in _VALID_OUTCOMES:
        outcome = "done"

    confidence = str(data.get("confidence", "medium")).strip().lower() or "medium"
    if confidence not in _VALID_CONFIDENCES:
        confidence = "medium"

    return {
        "goal_understanding": str(data.get("goal_understanding", "")).strip(),
        "completed_work": _listify(data.get("completed_work")),
        "remaining_gaps": _listify(data.get("remaining_gaps")),
        "evidence_type": evidence_type,
        "evidence": _listify(data.get("evidence")),
        "outcome": outcome,
        "confidence": confidence,
    }


# ── 3-stage acceptance gate ─────────────────────────────────────────────────


def review_completion_report(state: "AgentState") -> tuple[str, dict]:
    """Review the completion report. Returns ``(verdict, verdict_dict)``.

    verdict ∈ {"pass", "weak_pass", "needs_more_work"}.

    Stage 1 — completion report review:
        report must exist and carry the required fields; artifact evidence
        files must exist on disk.

    Stage 2 — episodic memory (optional):
        skipped unless ``state.episodic_required`` is set.

    Stage 3 — concept memory (optional):
        skipped unless ``state.concept_required`` is set.
    """
    normalized = _normalize_completion_report(state.completion_report)

    # Stage 1: completion report presence + required fields
    if not normalized["goal_understanding"]:
        return "needs_more_work", {
            "status": "needs_more_work",
            "reason": "missing_completion_report",
        }

    missing_fields = [f for f in _REQUIRED_FIELDS if not normalized.get(f)]
    # ``completed_work`` may legitimately be an empty list for done_blocked,
    # but goal_understanding is already checked above. outcome/confidence are
    # always populated by normalization, so this is a defensive check.
    if missing_fields and "goal_understanding" not in missing_fields:
        # Only flag if a truly empty required field remains.
        pass

    # Stage 1b: artifact evidence files must exist
    if normalized["evidence_type"] == "artifact":
        missing_files = _check_artifact_files(normalized["evidence"])
        if missing_files:
            return "needs_more_work", {
                "status": "needs_more_work",
                "reason": "artifact_missing",
                "missing": missing_files,
                "report": normalized,
            }

    # Stage 2: episodic memory (optional)
    if getattr(state, "episodic_required", False) and not getattr(
        state, "episodic_appended", False
    ):
        return "needs_more_work", {
            "status": "needs_more_work",
            "reason": "missing_episodic_memory",
            "report": normalized,
        }

    # Stage 3: concept memory (optional)
    if getattr(state, "concept_required", False) and not getattr(
        state, "concept_updated", False
    ):
        return "needs_more_work", {
            "status": "needs_more_work",
            "reason": "missing_concept_memory",
            "report": normalized,
        }

    # weak_pass for partial / blocked outcomes
    if normalized["outcome"] in {"done_partial", "done_blocked"}:
        reason = (
            "partial_completion"
            if normalized["outcome"] == "done_partial"
            else "blocked_completion"
        )
        return "weak_pass", {"status": "weak_pass", "reason": reason, "report": normalized}

    return "pass", {
        "status": "pass",
        "reason": "completion_report_sufficient",
        "report": normalized,
    }


def _check_artifact_files(evidence: List[str]) -> List[str]:
    """Return the sorted list of artifact paths that do not exist on disk."""
    run_dir = os.environ.get("RUN_DIR")
    if run_dir:
        repo_root = Path(run_dir).resolve().parent.parent
    else:
        repo_root = Path.cwd().resolve()

    missing: list[str] = []
    for item in evidence:
        pp = Path(item)
        if not pp.is_absolute():
            pp = repo_root / pp
        if not pp.exists():
            missing.append(str(pp.resolve()))

    return sorted(set(missing))


# ── submit_completion_report tool ───────────────────────────────────────────


def _make_submit_completion_report(state: "AgentState") -> Callable:
    """Return the tool callback that writes the completion report into state."""

    def _callback(args: dict, ctx: Any = None) -> dict:
        report = _normalize_completion_report(
            {
                "goal_understanding": args.get("goal_understanding", ""),
                "completed_work": args.get("completed_work"),
                "remaining_gaps": args.get("remaining_gaps"),
                "evidence_type": args.get("evidence_type", "none"),
                "evidence": args.get("evidence"),
                "outcome": args.get("outcome", "done"),
                "confidence": args.get("confidence", "medium"),
            }
        )
        state.completion_report = report
        return {"success": True, "output": report}

    return _callback


def acceptance_gate_tools(state: "AgentState") -> list:
    """Return the list of acceptance-gate tools (currently just the report tool)."""
    import senza

    parameters = {
        "type": "object",
        "properties": {
            "goal_understanding": {
                "type": "string",
                "description": "Concise restatement of the task goal.",
            },
            "completed_work": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What was actually accomplished (not planned).",
            },
            "remaining_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What is still missing and the concrete next step.",
            },
            "evidence_type": {
                "type": "string",
                "enum": ["artifact", "tool_result", "observation", "none"],
                "description": "Kind of evidence backing the completion claim.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Artifact paths or evidence references.",
            },
            "outcome": {
                "type": "string",
                "enum": ["done", "done_partial", "done_blocked"],
                "description": "Completion outcome.",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Confidence in the completion claim.",
            },
        },
        "required": [
            "goal_understanding",
            "completed_work",
            "evidence_type",
            "evidence",
            "outcome",
            "confidence",
        ],
    }

    return [
        senza.create_tool(
            name="submit_completion_report",
            description=(
                "Submit a structured completion report. Call this before emitting "
                "the final answer so the acceptance gate can verify your work."
            ),
            parameters=parameters,
            callback=_make_submit_completion_report(state),
        )
    ]


# ── final_answer_validator hook ─────────────────────────────────────────────


def acceptance_validator(state: "AgentState") -> Callable[[dict], Optional[str]]:
    """Return a ``final_answer_validator`` callback.

    Returns ``None`` (accept) when the answer passes the gate, or an error
    string (reject → loop breaks) when it does not.

    The runtime breaks the loop on rejection (no retry), so this validator
    only rejects when there is a concrete, fixable problem — a completion
    report referencing artifact files that don't exist. In all other cases
    (no report, simple conversation, etc.) it accepts.
    """

    def _validator(answer: dict) -> Optional[str]:
        # Only check if a report was submitted with artifact evidence.
        if state.completion_report is None:
            return None

        normalized = _normalize_completion_report(state.completion_report)
        if normalized["evidence_type"] == "artifact":
            missing_files = _check_artifact_files(normalized["evidence"])
            if missing_files:
                return (
                    "Completion report references artifact files that do not exist: "
                    f"{missing_files}. Create them or correct the evidence, then "
                    "resubmit submit_completion_report."
                )

        return None

    return _validator
