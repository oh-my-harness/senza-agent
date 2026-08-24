"""Tests for senza-agent tools.

These tests exercise the tool functions in ``standard.py`` directly (calling
them as plain Python functions) and the ``AsyncJobManager`` in
``async_manager.py``.  We avoid importing ``senza`` itself because the SDK
package may not be importable in the test environment (it depends on a Rust
extension); the tool functions are pure Python and testable in isolation.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Ensure the package root is on sys.path so `from senza_agent.tools ...` works
# regardless of how pytest is invoked.
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from senza_agent.tools import standard
from senza_agent.tools import async_manager as _am


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Reset module-level state and redirect paths to tmp_path for each test."""
    # Reset the _StateRef
    standard._state = standard._StateRef()
    # Redirect HOME so memory files don't pollute the real home
    monkeypatch.setenv("HOME", str(tmp_path))
    # Redirect RUN_DIR so scratchpad goes to tmp
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)
    # Reset async manager singleton
    standard._async_mod._default_manager = None
    # Reset watcher manager singleton
    standard._watcher_mod._default_manager = None
    yield
    # Cleanup any background threads from async manager
    mgr = standard._async_mod._default_manager
    if mgr is not None:
        try:
            mgr.cancel_all_running()
        except Exception:
            pass


# ── remember ────────────────────────────────────────────────────────────────


def test_remember_writes_to_memory_file(tmp_path):
    result = standard.tool_remember(content="important finding")
    assert result["status"] == "ok"
    mem_file = tmp_path / ".senza-agent" / "memory_long_term.md"
    assert mem_file.exists()
    content = mem_file.read_text(encoding="utf-8")
    assert "important finding" in content
    assert len(standard._state.long_term) == 1


def test_remember_rejects_empty_content():
    result = standard.tool_remember(content="")
    assert result["status"] == "error"
    assert "empty" in result["error"].lower()


# ── scratchpad ──────────────────────────────────────────────────────────────


def test_scratchpad_set_and_get():
    standard.tool_scratchpad_set(content="hello scratchpad")
    result = standard.tool_scratchpad_get()
    assert result["status"] == "ok"
    assert "hello scratchpad" in result["output"]


def test_scratchpad_append():
    standard.tool_scratchpad_set(content="line1")
    standard.tool_scratchpad_append(content="line2")
    result = standard.tool_scratchpad_get()
    assert result["status"] == "ok"
    assert "line1" in result["output"]
    assert "line2" in result["output"]


def test_scratchpad_append_rejects_empty():
    result = standard.tool_scratchpad_append(content="")
    assert result["status"] == "error"


# ── think ───────────────────────────────────────────────────────────────────


def test_think_returns_success():
    result = standard.tool_think(thought="I should analyse the data flow")
    assert result["status"] == "ok"
    assert "recorded" in result["output"].lower()


# ── get_env_info ────────────────────────────────────────────────────────────


def test_get_env_info_returns_datetime_and_cwd():
    result = standard.tool_get_env_info()
    assert result["status"] == "ok"
    info = result["output"]
    assert "datetime" in info
    assert "cwd" in info
    # datetime should look like YYYY-MM-DD HH:MM:SS
    assert len(info["datetime"]) >= 19
    assert info["cwd"] == os.getcwd()


# ── validate_tool_recipe ────────────────────────────────────────────────────


def test_validate_tool_recipe_valid_code():
    valid_code = '''
def run(**kwargs):
    return {"status": "ok", "output": "hello"}
'''
    result = standard.tool_validate_tool_recipe(
        name="my_tool",
        description="A test tool",
        args_schema={"x": "param x"},
        python_code=valid_code,
    )
    assert result["status"] == "ok"
    out = result["output"]
    assert out["ok"] is True
    assert out["errors"] == []


def test_validate_tool_recipe_invalid_code():
    # Missing run() function
    invalid_code = '''
def not_run(**kwargs):
    return {"status": "ok"}
'''
    result = standard.tool_validate_tool_recipe(
        name="bad_tool",
        description="A broken tool",
        args_schema={},
        python_code=invalid_code,
    )
    assert result["status"] == "ok"
    out = result["output"]
    assert out["ok"] is False
    assert len(out["errors"]) > 0
    assert "run" in out["errors"][0].lower()


def test_validate_tool_recipe_syntax_error():
    result = standard.tool_validate_tool_recipe(
        name="syntax_err",
        description="",
        args_schema={},
        python_code="def run(  # broken",
    )
    assert result["status"] == "ok"
    out = result["output"]
    assert out["ok"] is False
    assert any("syntax" in e.lower() for e in out["errors"])


# ── file_outline ────────────────────────────────────────────────────────────


def test_file_outline_python(tmp_path):
    py_file = tmp_path / "sample.py"
    py_file.write_text('''
class Foo:
    def bar(self):
        pass

def baz():
    pass
''', encoding="utf-8")
    result = standard.tool_file_outline(path=str(py_file))
    assert result["status"] == "ok"
    outline = result["output"]
    assert "Foo" in outline
    assert "bar" in outline
    assert "baz" in outline
    assert "lines" in outline.lower() or "行" in outline


def test_file_outline_nonexistent():
    result = standard.tool_file_outline(path="/nonexistent/file.py")
    assert result["status"] == "error"


# ── set_goal ────────────────────────────────────────────────────────────────


def test_set_goal():
    result = standard.tool_set_goal(new_goal="build a web app", reason="user request")
    assert result["status"] == "ok"
    assert standard._state.goal == "build a web app"


def test_set_goal_rejects_empty():
    result = standard.tool_set_goal(new_goal="", reason="")
    assert result["status"] == "error"


# ── submit_completion_report ────────────────────────────────────────────────


def test_submit_completion_report():
    result = standard.tool_submit_completion_report(
        goal_understanding="build feature X",
        completed_work=["wrote code", "ran tests"],
        remaining_gaps=["docs"],
        evidence_type="artifact",
        evidence=["src/feature.py"],
    )
    assert result["status"] == "ok"
    report = result["output"]
    assert report["goal_understanding"] == "build feature X"
    assert report["completed_work"] == ["wrote code", "ran tests"]
    assert report["remaining_gaps"] == ["docs"]
    assert report["evidence_type"] == "artifact"
    assert standard._state.completion_report == report


# ── episodic memory ─────────────────────────────────────────────────────────


def test_append_and_search_episodic(tmp_path):
    epi_file = tmp_path / "episodic.jsonl"
    standard.tool_append_episodic(
        path=str(epi_file), summary="task A completed", tags="python,test",
    )
    standard.tool_append_episodic(
        path=str(epi_file), summary="task B failed", tags="ssh,linux",
    )

    # Search all
    result = standard.tool_search_episodic(path=str(epi_file))
    assert result["status"] == "ok"
    out = result["output"]
    assert out["total"] == 2

    # Search by keyword
    result = standard.tool_search_episodic(path=str(epi_file), keyword="python")
    assert result["status"] == "ok"
    out = result["output"]
    assert out["total"] == 1
    assert "task A" in out["entries"][0]["summary"]


# ── save/load tools ─────────────────────────────────────────────────────────


def test_save_and_load_tools(tmp_path):
    # Register a tool first
    valid_code = 'def run(**kwargs):\n    return {"status": "ok", "output": "hi"}\n'
    standard.tool_register_tool(
        name="my_tool", description="test", args_schema={"x": "param"}, python_code=valid_code,
    )
    tools_file = tmp_path / "tools.json"
    result = standard.tool_save_tools(path=str(tools_file))
    assert result["status"] == "ok"
    assert result["output"]["saved"] == 1
    assert tools_file.exists()

    # Clear and reload
    standard._state.evolved_tools = {}
    result = standard.tool_load_tools(path=str(tools_file))
    assert result["status"] == "ok"
    assert "my_tool" in standard._state.evolved_tools


# ── evolved tool lifecycle ──────────────────────────────────────────────────


def test_register_and_delete_tool():
    code = 'def run(**kwargs):\n    return {"status": "ok", "output": "done"}\n'
    # Register
    result = standard.tool_register_tool(
        name="temp_tool", description="temp", args_schema={}, python_code=code,
    )
    assert result["status"] == "ok"
    assert "temp_tool" in standard._state.evolved_tools

    # Preview delete
    result = standard.tool_delete_tool(name="temp_tool", confirm=False)
    assert result["status"] == "ok"
    assert result["output"]["preview"] is True

    # Execute delete
    result = standard.tool_delete_tool(name="temp_tool", confirm=True)
    assert result["status"] == "ok"
    assert "temp_tool" not in standard._state.evolved_tools


def test_repair_and_promote_candidate():
    # Register original
    code = 'def run(**kwargs):\n    return {"status": "ok", "output": "v1"}\n'
    standard.tool_register_tool(
        name="repairable", description="v1", args_schema={}, python_code=code,
    )

    # Store repair candidate
    new_code = 'def run(**kwargs):\n    return {"status": "ok", "output": "v2"}\n'
    result = standard.tool_repair_tool_candidate(
        name="repairable", description="v2", args_schema={}, python_code=new_code,
    )
    assert result["status"] == "ok"
    assert "repairable" in standard._state.repair_candidates

    # Promote
    result = standard.tool_promote_tool_candidate(name="repairable")
    assert result["status"] == "ok"
    assert result["output"]["promoted"] is True
    assert "repairable" not in standard._state.repair_candidates
    assert standard._state.evolved_tools["repairable"]["description"] == "v2"


# ── raw_append ──────────────────────────────────────────────────────────────


def test_raw_append(tmp_path):
    raw_file = tmp_path / "raw.ndjson"
    result = standard.tool_raw_append(content="raw note", path=str(raw_file))
    assert result["status"] == "ok"
    assert raw_file.exists()
    lines = raw_file.read_text(encoding="utf-8").strip().split("\n")
    rec = json.loads(lines[0])
    assert rec["content"] == "raw note"
    assert "ts" in rec


# ── save_concept / read_concept ────────────────────────────────────────────


def test_save_and_read_concept(tmp_path):
    concept_file = tmp_path / "concept.md"
    # Full mode
    result = standard.tool_save_concept(
        path=str(concept_file), content="# Topic A\nSome notes about A.",
    )
    assert result["status"] == "ok"
    assert concept_file.exists()

    # Section mode — replace
    result = standard.tool_save_concept(
        path=str(concept_file), content="Updated A content.", section="Topic A",
    )
    assert result["status"] == "ok"
    text = concept_file.read_text(encoding="utf-8")
    assert "Updated A content" in text

    # Read
    result = standard.tool_read_concept(path=str(concept_file))
    assert result["status"] == "ok"
    assert "Updated A content" in result["output"]["content"]


# ── async_manager ───────────────────────────────────────────────────────────


def test_async_job_start_and_wait(tmp_path):
    mgr = _am.AsyncJobManager(jobs_dir=tmp_path / "jobs")
    job_id = mgr.start_shell("echo hello_world")
    assert job_id.startswith("job_")

    # Wait for completion
    info = mgr.peek(job_id, wait_secs=10)
    assert info["status"] in ("done", "failed")
    if info["status"] == "done":
        assert "hello_world" in info["output"]
    assert info["returncode"] == 0


def test_async_job_list(tmp_path):
    mgr = _am.AsyncJobManager(jobs_dir=tmp_path / "jobs")
    job_id = mgr.start_shell("echo test_list")
    # Wait a moment for the job to register
    time.sleep(0.5)
    jobs = mgr.list_jobs()
    assert len(jobs) >= 1
    assert any(j["job_id"] == job_id for j in jobs)


def test_async_job_cancel(tmp_path):
    mgr = _am.AsyncJobManager(jobs_dir=tmp_path / "jobs")
    # Start a long-running command
    job_id = mgr.start_shell("sleep 30")
    time.sleep(0.5)
    result = mgr.cancel(job_id)
    assert result.get("cancelled") is True
    info = mgr.peek(job_id)
    assert info["status"] == "cancelled"


def test_async_job_failed_command(tmp_path):
    mgr = _am.AsyncJobManager(jobs_dir=tmp_path / "jobs")
    job_id = mgr.start_shell("exit 1")
    info = mgr.peek(job_id, wait_secs=10)
    assert info["status"] == "failed"
    assert info["returncode"] == 1


# ── watcher manager ─────────────────────────────────────────────────────────


def test_watcher_register_and_list(tmp_path):
    from senza_agent.tools import watcher as _w

    reg_path = tmp_path / "watchers.json"
    mgr = _w.WatcherManager(registry_path=reg_path, artifacts_dir=tmp_path / "artifacts")

    # Create a simple .py watcher
    watcher_code = tmp_path / "my_watcher.py"
    watcher_code.write_text(
        "def run(prev, store, iter_n):\n"
        "    return {'type': 'text', 'content': 'hello from watcher'}\n",
        encoding="utf-8",
    )

    result = mgr.register(name="test_w", path=str(watcher_code), interval=1)
    assert result["ok"] is True

    entries = mgr.list_entries()
    assert len(entries) == 1
    assert entries[0]["name"] == "test_w"

    # Unregister
    result = mgr.unregister("test_w")
    assert result["ok"] is True
    assert len(mgr.list_entries()) == 0


def test_watcher_poll_executes(tmp_path):
    from senza_agent.tools import watcher as _w

    reg_path = tmp_path / "watchers.json"
    mgr = _w.WatcherManager(registry_path=reg_path, artifacts_dir=tmp_path / "artifacts")

    watcher_code = tmp_path / "poll_watcher.py"
    watcher_code.write_text(
        "def run(prev, store, iter_n):\n"
        "    return {'type': 'text', 'content': f'iter={iter_n}'}\n",
        encoding="utf-8",
    )
    mgr.register(name="poll_w", path=str(watcher_code), interval=0)

    events = mgr.poll(1)
    assert len(events) == 1
    assert "iter=1" in events[0]["content"]


# ── request_advisor ─────────────────────────────────────────────────────────


def test_request_advisor_sets_flag():
    result = standard.tool_request_advisor(reason="need direction")
    assert result["status"] == "ok"
    assert standard._state.advisor_requested is True


# ── ask_user ────────────────────────────────────────────────────────────────


def test_ask_user():
    result = standard.tool_ask_user(question="Which option do you prefer?")
    assert result["status"] == "ok"
    assert result["output"]["question"] == "Which option do you prefer?"


# ── jobs_list tool ──────────────────────────────────────────────────────────


def test_tool_jobs_list_empty():
    result = standard.tool_jobs_list()
    assert result["status"] == "ok"
