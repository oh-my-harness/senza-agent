"""Tests for RunPersistence."""
from __future__ import annotations

import json
from pathlib import Path

from senza_agent.persistence import RunPersistence


def test_run_dir_created(tmp_path):
    run_dir = tmp_path / "myrun"
    p = RunPersistence(str(run_dir))
    assert run_dir.is_dir()
    assert p.run_dir == run_dir
    # Standard file paths are anchored under run_dir.
    assert p.meta_path.parent == run_dir
    assert p.status_path.parent == run_dir


def test_save_meta_atomic(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    meta = {"goal": "do thing", "iteration": 3, "nested": {"a": [1, 2, 3]}}
    p.save_meta(meta)
    on_disk = json.loads((tmp_path / "run" / "meta.json").read_text(encoding="utf-8"))
    assert on_disk == meta


def test_save_status_payload(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    p.save_status("done", outcome="complete", summary="finished the thing", error=None)
    payload = json.loads((tmp_path / "run" / "status.json").read_text(encoding="utf-8"))
    assert payload["status"] == "done"
    assert payload["outcome"] == "complete"
    assert payload["summary"] == "finished the thing"
    assert payload["run_id"] == "run"
    assert payload["final_answer_written"] is False
    assert "started_at" in payload and "updated_at" in payload


def test_save_scratchpad(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    p.save_scratchpad("working notes\n- item 1\n")
    text = (tmp_path / "run" / "scratchpad.md").read_text(encoding="utf-8")
    assert text == "working notes\n- item 1\n"


def test_append_short_term(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    p.append_short_term({"role": "user", "content": "hello"})
    p.append_short_term({"role": "assistant", "content": "hi"})
    lines = (tmp_path / "run" / "short_term.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"role": "user", "content": "hello"}
    assert json.loads(lines[1]) == {"role": "assistant", "content": "hi"}


def test_save_final_answer(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    p.save_final_answer("the answer is 42")
    assert (tmp_path / "run" / "final_answer.md").read_text(encoding="utf-8") == "the answer is 42"
    # final_answer_written flag in status should now be True.
    p.save_status("done")
    payload = json.loads((tmp_path / "run" / "status.json").read_text(encoding="utf-8"))
    assert payload["final_answer_written"] is True


def test_save_execution_summary(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    p.save_execution_summary({
        "status": "done",
        "outcome": "complete",
        "goal": "build it",
        "final_answer": "shipped",
        "used_tools": ["read_file", "write_file"],
        "json_parse_errors": 2,
        "timeout": False,
        "failures": ["boom"],
    })
    text = (tmp_path / "run" / "execution_summary.md").read_text(encoding="utf-8")
    assert "# Execution Summary" in text
    assert "build it" in text
    assert "shipped" in text
    assert "read_file" in text
    assert "JSON parse errors: 2" in text


def test_save_advisor_log(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    p.save_advisor_log([{"iter": 1, "advice": "go"}, {"iter": 2, "advice": "stop"}])
    lines = (tmp_path / "run" / "advisor_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["advice"] == "go"
    assert json.loads(lines[1])["advice"] == "stop"


def test_save_graph(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    graph = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [["a", "b"]]}
    p.save_graph(graph)
    on_disk = json.loads((tmp_path / "run" / "graph.json").read_text(encoding="utf-8"))
    assert on_disk == graph


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    p.save_meta({"a": 1})
    p.save_status("running")
    p.save_scratchpad("x")
    # After atomic writes, only the target files should exist — no leftover .tmp.
    files = sorted(child.name for child in (tmp_path / "run").iterdir())
    assert "meta.json" in files
    assert not any(name.endswith(".tmp") for name in files)


def test_default_run_dir_under_home(tmp_path, monkeypatch):
    # Redirect HOME so _default_run_dir lands inside tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    p = RunPersistence()
    assert p.run_dir.is_relative_to(tmp_path / ".senza-agent" / "runs")
    assert p.run_dir.is_dir()


# ── finish / diagnostics tests ──────────────────────────────────────────────


class _FakeState:
    """Minimal stand-in for AgentState used by finish/checkpoint/diagnostics."""

    def __init__(self, **kw):
        self.goal = kw.get("goal", "test goal")
        self.turn_count = kw.get("turn_count", 3)
        self.meta = kw.get("meta", {})


def _short_term_with_tools():
    return [
        {"role": "user", "content": "please run the task"},
        {
            "role": "assistant",
            'content': '{"tool": "shell", "input": "echo hi"}',
        },
        {
            "role": "assistant",
            "content": "执行失败: something broke [TOOL ERROR]",
        },
        {
            "role": "assistant",
            "content": "输出格式错误: plain text with no JSON structure",
        },
    ]


def test_collect_diagnostics(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    state = _FakeState(
        meta={
            "short_term": _short_term_with_tools(),
            "long_term": ["[自我修复] recovered from x"],
            "timeout": True,
        }
    )
    diag = p._collect_diagnostics(state)
    assert diag["used_tools"] == ["shell"]
    assert diag["json_parse_errors"] == 1
    assert len(diag["failures"]) == 1
    assert any(i["kind"] == "tool_failure" for i in diag["issues"])
    assert any(i["kind"] == "json_parse_error" for i in diag["issues"])
    assert diag["timeout"] is True
    assert diag["self_heal_notes"] == ["[自我修复] recovered from x"]


def test_collect_diagnostics_empty(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    state = _FakeState(meta={})
    diag = p._collect_diagnostics(state)
    assert diag["used_tools"] == []
    assert diag["failures"] == []
    assert diag["json_parse_errors"] == 0
    assert diag["timeout"] is False
    assert diag["self_heal_notes"] == []


def test_finish_writes_artifacts(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    state = _FakeState(
        goal="build it",
        turn_count=5,
        meta={
            "short_term": _short_term_with_tools(),
            "final_answer": "已成功完成了 the build.",
        },
    )
    p.finish(state, outcome="done")

    assert p.final_answer_path.exists()
    assert p.execution_summary_path.exists()
    assert p.issues_path.exists()
    assert p.reflection_path.exists()
    assert p.status_path.exists()
    assert p.meta_path.exists()

    summary = p.execution_summary_path.read_text(encoding="utf-8")
    assert "Execution Summary" in summary
    assert "shell" in summary
    assert "build it" in summary

    issues = json.loads(p.issues_path.read_text(encoding="utf-8"))
    assert issues["goal"] == "build it"
    assert issues["json_parse_errors"] == 1
    assert issues["used_tools"] == ["shell"]

    reflection = p.reflection_path.read_text(encoding="utf-8")
    assert "Reflection" in reflection
    assert "JSON 解析失败次数：1" in reflection

    status = json.loads(p.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "done"


def test_finish_with_error(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    state = _FakeState(meta={"short_term": _short_term_with_tools()})
    p.finish(state, outcome="failed", error="boom")
    issues = json.loads(p.issues_path.read_text(encoding="utf-8"))
    assert any(i.get("kind") == "run_failure" and i.get("message") == "boom" for i in issues["issues"])
    status = json.loads(p.status_path.read_text(encoding="utf-8"))
    assert status["status"] == "failed"


def test_save_handoff(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    p.save_handoff(2, "segment 2 summary")
    handoff = tmp_path / "run" / "handoff_2.md"
    assert handoff.exists()
    assert handoff.read_text(encoding="utf-8") == "segment 2 summary"


def test_save_system_prompt(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    p.save_system_prompt("you are an agent")
    assert p.system_prompt_path.exists()
    assert p.system_prompt_path.read_text(encoding="utf-8") == "you are an agent"


def test_start_checkpoints_and_scratchpad(tmp_path):
    p = RunPersistence(str(tmp_path / "run"))
    state = _FakeState(meta={"scratchpad": "notes"})
    p.start(state)
    assert p.status_path.exists()
    assert p.scratchpad_path.exists()
    assert "notes" in p.scratchpad_path.read_text(encoding="utf-8")
