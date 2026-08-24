"""
Per-run persistence for senza-agent.

Ports QevosAgent's RunPersistence to a state-coupling-free design: callers pass
data explicitly rather than handing in a state object. Per-run directory layout,
atomic writes, and the standard file set (meta/status/scratchpad/final_answer/
execution_summary/advisor_log/graph) are preserved.

Run directory layout (under ~/.senza-agent/runs/<YYYYMMDD-HHMMSS>/):
    short_term.jsonl      append-only per-turn records
    meta.json             run metadata (atomic)
    status.json           run status snapshot (atomic)
    scratchpad.md         working scratchpad (atomic)
    final_answer.md       final answer (atomic)
    execution_summary.md  post-run summary (atomic)
    advisor_log.jsonl     append-only advisor entries
    graph.json            execution graph snapshot (atomic)
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _make_summary(text: str, max_len: int = 40) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    if len(text) <= max_len:
        return text
    return text[:max_len] + ("…" if len(text) > max_len else "")



_COMPLETION_PREFIXES = (
    # 中文
    "已成功完成了", "已成功地完成了", "已成功完成", "已成功地完成",
    "成功完成了", "成功完成",
    "已经完成了", "已经完成",
    "已完成了", "已完成",
    "完成了",
    "已成功", "成功地",
    # English
    "Successfully completed", "I have successfully", "I've successfully",
    "The task has been", "I have completed", "I've completed",
)

_FAILURE_MARKERS = ("执行失败", "execution failed", "[TOOL ERROR]")
_JSON_ERR_MARKERS = (
    "JSON 解析失败", "JSON parse error",
    "输出格式错误", "plain text with no JSON structure",
)


def _make_completion_summary(text: str, max_len: int = 40) -> str:
    """取 final_answer 第一句并剥离冗余完成前缀，避免列表里每条都以"已完成"开头。"""
    if not text:
        return ""
    # 先取第一句（放宽到 200 字，再做二次截断）
    first = _make_summary(text.strip(), max_len=200)
    # 剥离冗余前缀（按长度降序匹配，避免短前缀遮蔽长前缀）
    for prefix in _COMPLETION_PREFIXES:
        if first.startswith(prefix):
            first = first[len(prefix):].lstrip(" \t，,：:")
            break
    first = first.strip()
    if not first:
        return _make_summary(text, max_len)
    return first[:max_len] + ("…" if len(first) > max_len else "")

def _write_text_atomic(path: Path, content: str) -> None:
    # Resolve to absolute path to avoid abs->relative rename failure on Windows
    # (tempfile always returns an absolute tmp.name via os.path.abspath internally)
    abs_path = path.resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(abs_path.parent), prefix=path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                os.replace(tmp_name, abs_path)
                return
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError as exc:  # Windows sometimes denies rename while scanners hold the file
            last_err = exc
            time.sleep(0.05 * (attempt + 1))
    if last_err is not None:
        raise last_err


class _SafeEncoder(json.JSONEncoder):
    """JSON encoder with stable set handling and a best-effort fallback."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, set):
            return sorted(obj)
        try:
            return super().default(obj)
        except TypeError:
            return repr(obj)


def _write_json_atomic(path: Path, payload: dict) -> None:
    _write_text_atomic(
        path, json.dumps(payload, ensure_ascii=False, indent=2, cls=_SafeEncoder) + "\n"
    )


def _default_run_dir() -> Path:
    base = Path(os.path.expanduser("~")) / ".senza-agent" / "runs"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = base / stamp
    # Avoid collisions when two runs start within the same second.
    counter = 0
    while run_dir.exists():
        counter += 1
        run_dir = base / f"{stamp}-{counter}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


class RunPersistence:
    """Per-run disk persistence. All writes are atomic where it matters.

    The constructor accepts an explicit ``run_dir`` (matching the public API);
    pass ``None`` to get a freshly minted timestamped directory under
    ``~/.senza-agent/runs/``.
    """

    def __init__(self, run_dir: Optional[str | os.PathLike[str]] = None):
        if run_dir is None:
            self.run_dir = _default_run_dir()
        else:
            self.run_dir = Path(run_dir)
            self.run_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = _utc_now()

        self.short_term_path = self.run_dir / "short_term.jsonl"
        self.meta_path = self.run_dir / "meta.json"
        self.status_path = self.run_dir / "status.json"
        self.scratchpad_path = self.run_dir / "scratchpad.md"
        self.system_prompt_path = self.run_dir / "system_prompt.md"
        self.final_answer_path = self.run_dir / "final_answer.md"
        self.execution_summary_path = self.run_dir / "execution_summary.md"
        self.issues_path = self.run_dir / "issues.json"
        self.advisor_log_path = self.run_dir / "advisor_log.jsonl"
        self.graph_path = self.run_dir / "graph.json"
        self.reflection_path = self.run_dir / "reflection.md"
        self.handoff_path = self.run_dir / "handoff_{n}.md"

    # ── status payload ────────────────────────────────────────────────────────

    def _status_payload(
        self,
        status: str = "running",
        outcome: str = "",
        summary: str = "",
        error: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "status": status,
            "outcome": outcome or None,
            "run_id": self.run_dir.name,
            "summary": summary or "",
            "started_at": self.started_at,
            "updated_at": _utc_now(),
            "final_answer_written": self.final_answer_path.exists(),
            "error": error,
        }
        if extra:
            payload.update(extra)
        return payload

    # ── append-only records ───────────────────────────────────────────────────

    def append_short_term(self, record: dict) -> None:
        """Append one record to short_term.jsonl (one JSON object per line)."""
        self.short_term_path.parent.mkdir(parents=True, exist_ok=True)
        with self.short_term_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, cls=_SafeEncoder) + "\n")

    def save_advisor_log(self, entries: list) -> None:
        """Write the full advisor log as JSONL (overwrites previous content)."""
        lines = []
        for entry in entries:
            lines.append(json.dumps(entry, ensure_ascii=False, cls=_SafeEncoder))
        _write_text_atomic(self.advisor_log_path, "\n".join(lines) + ("\n" if lines else ""))

    # ── atomic full-file writes ───────────────────────────────────────────────

    def save_meta(self, meta: dict) -> None:
        """Write meta.json atomically."""
        _write_json_atomic(self.meta_path, meta or {})

    def save_status(
        self,
        status: str,
        outcome: str = "",
        summary: str = "",
        error: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        """Write status.json atomically."""
        _write_json_atomic(
            self.status_path,
            self._status_payload(
                status=status,
                outcome=outcome,
                summary=summary,
                error=error,
                extra=extra,
            ),
        )

    def save_scratchpad(self, content: str) -> None:
        """Write scratchpad.md atomically."""
        _write_text_atomic(self.scratchpad_path, content or "")

    def save_final_answer(self, text: str) -> None:
        """Write final_answer.md atomically."""
        _write_text_atomic(self.final_answer_path, text or "")

    def save_execution_summary(self, diagnostics: dict) -> None:
        """Write execution_summary.md atomically.

        ``diagnostics`` is a free-form dict; recognised keys are rendered into
        labeled sections. Unrecognised keys are ignored rather than dropped, so
        callers can pass richer dicts without breaking the summary.
        """
        lines = [
            "# Execution Summary",
            "",
            "## Outcome",
            f"- status: {diagnostics.get('status', '')}",
            f"- outcome: {diagnostics.get('outcome', '') or '(unset)'}",
            f"- error: {diagnostics.get('error') or '(none)'}",
            "",
        ]
        goal = diagnostics.get("goal")
        if goal:
            lines += ["## Goal", str(goal), ""]
        final_answer = diagnostics.get("final_answer")
        if final_answer:
            lines += ["## Final Answer", str(final_answer), ""]
        used_tools = diagnostics.get("used_tools") or []
        lines += ["## Tools Used"]
        if used_tools:
            lines.extend(f"- {name}" for name in used_tools)
        else:
            lines.append("- (none inferred)")
        lines.append("")
        lines += [
            "## Issues Observed",
            f"- JSON parse errors: {diagnostics.get('json_parse_errors', 0)}",
            f"- Timeout hit: {bool(diagnostics.get('timeout'))}",
        ]
        failures = diagnostics.get("failures") or []
        if failures:
            lines.extend(["", "### Failure Snippets"])
            for idx, snippet in enumerate(failures[:10], 1):
                lines.append(f"{idx}. {str(snippet).replace('```', '')}")
        lines.append("")
        _write_text_atomic(self.execution_summary_path, "\n".join(lines).strip() + "\n")

    def save_graph(self, graph: dict) -> None:
        """Write graph.json atomically (execution graph snapshot)."""
        _write_json_atomic(self.graph_path, graph or {})

    # ── lifecycle: start / checkpoint / finish ────────────────────────────────

    def _collect_diagnostics(self, state) -> dict:
        """Scan short_term records for tool usage, failures, and parse errors.

        Adapted from QevosAgent: short_term/long_term live on ``state.meta``
        in senza-agent rather than as top-level attributes, so we pull them
        via getattr-with-defaults for safety.
        """
        meta = dict(getattr(state, "meta", {}) or {})
        short_term = list(meta.get("short_term", []) or [])
        long_term = list(meta.get("long_term", []) or [])

        used_tools: list[str] = []
        failures: list[str] = []
        issues: list[dict] = []
        json_parse_errors = 0
        self_heal_notes: list[str] = []

        for idx, message in enumerate(short_term):
            content = message.get("content", "") if isinstance(message, dict) else ""
            if not isinstance(content, str):
                continue

            # 只扫 assistant：工具调用只可能出自模型。扫全部消息会把系统注入的
            # 格式纠错模板里的占位符也当成真调用记进来。
            if message.get("role") == "assistant" and '"tool"' in content:
                match = re.search(r'"tool"\s*:\s*"([^"]+)"', content)
                if match:
                    used_tools.append(match.group(1))

            if any(m in content for m in _FAILURE_MARKERS):
                failures.append(content[:800])
                issues.append(
                    {
                        "kind": "tool_failure",
                        "short_term_index": idx,
                        "snippet": content[:2000],
                    }
                )

            if any(m in content for m in _JSON_ERR_MARKERS):
                json_parse_errors += 1
                issues.append(
                    {
                        "kind": "json_parse_error",
                        "short_term_index": idx,
                        "snippet": content[:2000],
                    }
                )

        _SELF_HEAL_MARKERS = ("[自我修复]", "[Self-heal]", "[RUN_OK]")
        for item in long_term:
            if isinstance(item, str) and any(m in item for m in _SELF_HEAL_MARKERS):
                self_heal_notes.append(item)

        used_tools = list(dict.fromkeys(used_tools))
        return {
            "used_tools": used_tools,
            "failures": failures,
            "issues": issues,
            "json_parse_errors": json_parse_errors,
            "self_heal_notes": self_heal_notes,
            "timeout": bool(meta.get("timeout")),
            "prompt_est": meta.get("prompt_tokens_est"),
            "context_window": meta.get("context_window"),
        }

    def start(self, state) -> None:
        """Begin a run: checkpoint + save scratchpad if present."""
        if state is not None:
            self.checkpoint(state, status="running")
            scratchpad = (getattr(state, "meta", {}) or {}).get("scratchpad", "")
            if isinstance(scratchpad, str) and scratchpad:
                self.save_scratchpad(scratchpad)
        else:
            _write_json_atomic(self.status_path, self._status_payload(status="running"))

    def checkpoint(
        self,
        state,
        status: str = "running",
        error: Optional[str] = None,
        summary_override: Optional[str] = None,
    ) -> None:
        """Write meta.json (minus non-serializable internals) + status.json."""
        if state is not None:
            meta = dict(getattr(state, "meta", {}) or {})
            # 移除不可 JSON 序列化的内部对象（如 AsyncJobManager、LLM 实例）
            _NON_SERIALIZABLE_KEYS = ("_async_manager", "_llm", "_team_api", "_watcher_manager")
            for _k in _NON_SERIALIZABLE_KEYS:
                meta.pop(_k, None)
            meta["_persistence"] = {
                "updated_at": _utc_now(),
                "iteration": int(getattr(state, "turn_count", 0) or 0),
            }
            _write_json_atomic(self.meta_path, meta)
        goal = getattr(state, "goal", "") if state is not None else ""
        summary = summary_override if summary_override is not None else _make_summary(goal)
        self.save_status(status=status, summary=summary, error=error)

    def save_handoff(self, seg_index: int, text: str) -> None:
        """Write handoff_{N}.md — structured handoff doc for segment N.

        Each compression segment writes one; raw per-turn records remain in
        short_term.jsonl, handoff is the readable overlay.
        """
        _write_text_atomic(self.run_dir / f"handoff_{int(seg_index)}.md", text or "")

    def save_system_prompt(self, text: str) -> None:
        """Write system_prompt.md atomically."""
        _write_text_atomic(self.system_prompt_path, text or "")

    def _write_execution_summary(
        self, state, outcome: str, diagnostics: dict, error: Optional[str]
    ) -> None:
        final_answer = ""
        goal = ""
        if state is not None:
            final_answer = (getattr(state, "meta", {}) or {}).get("final_answer") or ""
            goal = getattr(state, "goal", "") or ""

        run_outcome = (getattr(state, "meta", {}) or {}).get("run_outcome") if state is not None else None
        run_outcome = run_outcome if isinstance(run_outcome, dict) else {}

        lines = [
            "# Execution Summary",
            "",
            f"## Outcome",
            f"- status: {outcome}",
            f"- run_outcome: {run_outcome.get('outcome') or '(unset)'}"
            + (f" ({run_outcome['reason']})" if run_outcome.get("reason") else ""),
            f"- resumable: {bool(run_outcome.get('resumable'))}",
            f"- error: {error or '(none)'}",
            "",
        ]
        if run_outcome.get("gaps"):
            lines.append("## Remaining Gaps")
            lines.extend(f"- {gap}" for gap in run_outcome["gaps"])
            lines.append("")
        lines += [
            "## Goal",
            goal or "(unknown)",
            "",
            "## Final Answer",
            final_answer or "(no final_answer)",
            "",
            "## Run Artifacts",
            "- short_term.jsonl",
            "- meta.json",
            "- status.json",
            "- scratchpad.md",
            "- final_answer.md",
            "- execution_summary.md",
            "- issues.json",
            "- reflection.md",
            "",
            "## Tools Used",
        ]
        if diagnostics["used_tools"]:
            lines.extend(f"- {name}" for name in diagnostics["used_tools"])
        else:
            lines.append("- (none inferred)")

        lines.extend(
            [
                "",
                "## Issues Observed",
                f"- JSON parse errors: {diagnostics['json_parse_errors']}",
                f"- Timeout hit: {diagnostics['timeout']}",
            ]
        )
        if diagnostics["failures"]:
            lines.extend(["", "### Failure Snippets"])
            for idx, snippet in enumerate(diagnostics["failures"][:10], 1):
                lines.append(f"{idx}. {snippet.replace('```', '')}")

        lines.extend(["", "## Self-Healing Notes"])
        if diagnostics["self_heal_notes"]:
            lines.extend(f"- {note}" for note in diagnostics["self_heal_notes"][-20:])
        else:
            lines.append("- (none)")

        _write_text_atomic(self.execution_summary_path, "\n".join(lines).strip() + "\n")

    def _write_issues(self, state, diagnostics: dict, error: Optional[str]) -> None:
        goal = getattr(state, "goal", "") if state is not None else ""
        payload = {
            "goal": goal,
            "timeout": diagnostics["timeout"],
            "json_parse_errors": diagnostics["json_parse_errors"],
            "used_tools": diagnostics["used_tools"],
            "issues": list(diagnostics["issues"]),
        }
        if error:
            payload["issues"].append({"kind": "run_failure", "message": error})
        _write_json_atomic(self.issues_path, payload)

    def _write_reflection(self, diagnostics: dict, error: Optional[str]) -> None:
        lines = [
            "# Reflection",
            "",
            "## 实际执行链路（概览）",
        ]
        if diagnostics["used_tools"]:
            lines.extend(f"- {name}" for name in diagnostics["used_tools"])
        else:
            lines.append("- (unknown)")

        lines.extend(
            [
                "",
                "## 发生的问题/异常",
                f"- JSON 解析失败次数：{diagnostics['json_parse_errors']}",
                f"- Timeout: {diagnostics['timeout']}",
            ]
        )
        if error:
            lines.append(f"- 运行异常：{error}")
        if diagnostics["failures"]:
            lines.append("- 观察到工具执行失败片段（见 issues.json）")

        lines.extend(
            [
                "",
                "## 下次行动清单",
                "- 原始 short_term 继续保持逐条追加写，避免任务中途退出时丢失关键轨迹。",
                "- 对外展示类文件依赖事实层文件生成，不再把它们当成唯一信息源。",
            ]
        )
        _write_text_atomic(self.reflection_path, "\n".join(lines).strip() + "\n")

    def finish(self, state, outcome: str, error: Optional[str] = None) -> None:
        """End a run: save final answer, collect diagnostics, checkpoint, and
        write execution_summary.md / issues.json / reflection.md.
        """
        status = outcome if outcome in {"running", "paused", "done", "failed"} else "failed"
        final_answer = ""
        if state is not None:
            final_answer = (getattr(state, "meta", {}) or {}).get("final_answer") or ""
        if final_answer:
            self.save_final_answer(final_answer)

        diagnostics = self._collect_diagnostics(state) if state is not None else {
            "used_tools": [],
            "failures": [],
            "issues": [],
            "json_parse_errors": 0,
            "self_heal_notes": [],
            "timeout": False,
            "prompt_est": None,
            "context_window": None,
        }

        completion_summary = _make_completion_summary(final_answer) if final_answer else None
        self.checkpoint(state, status=status, error=error, summary_override=completion_summary)
        self._write_execution_summary(state, status, diagnostics, error)
        self._write_issues(state, diagnostics, error)
        self._write_reflection(diagnostics, error)
