"""Standard tool set for senza-agent.

Ported from QevosAgent's ``agent/tools/standard.py``, adapted from the
``fn(state, **kwargs) -> ToolResult`` pattern to plain ``def tool_xxx(**kwargs) -> dict``
functions suitable for wrapping with ``senza.create_tool``.

Key adaptations:
- ``state.long_term``      -> append to ``~/.senza-agent/memory_long_term.md``
- ``state.meta['scratchpad']`` -> read/write ``~/.senza-agent/runs/<run_id>/scratchpad.md``
- ``state.meta['evolved_tools']`` etc. -> module-level ``_state``
- ``ToolResult(success=True, output=...)``  -> ``{'status': 'ok', 'output': ...}``
- ``ToolResult(success=False, error=...)``  -> ``{'status': 'error', 'error': ...}``
"""

from __future__ import annotations

import ast
import base64
import io
import json
import os
import re
import subprocess
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from . import async_manager as _async_mod
from . import watcher as _watcher_mod


# ── Module-level state ref ──────────────────────────────────────────────────


class _StateRef:
    """Mutable shared state, set by agent.py via ``set_state``."""

    completion_report: Optional[dict] = None
    goal: str = ""
    advisor_requested: bool = False
    evolved_tools: dict = {}
    repair_candidates: dict = {}
    repair_failures: dict = {}
    repair_history: list = []
    long_term: list = []
    concept_memory: str = ""
    runtime_patches: list = []
    interrupt_handler: Any = None
    vision_supported: Optional[bool] = None
    bad_image_urls: dict = {}
    yield_waiting_job: Optional[dict] = None


_state = _StateRef()


def set_state(state: Any) -> None:
    """Called by agent.py to share AgentState.

    Accepts either a ``_StateRef``-like object or any object with the relevant
    attributes; stores the reference so tools can read/write shared fields.
    """
    global _state
    _state = state


def _ok(output: Any = None, **extra) -> dict:
    d = {"status": "ok"}
    if output is not None or not extra:
        d["output"] = output
    d.update(extra)
    return d


def _err(error: str, **extra) -> dict:
    d = {"status": "error", "error": error}
    d.update(extra)
    return d


# ── Path helpers ────────────────────────────────────────────────────────────


def _home_dir() -> Path:
    return Path(os.environ.get("HOME", "~")).expanduser()


def _memory_file() -> Path:
    return _home_dir() / ".senza-agent" / "memory_long_term.md"


def _run_dir() -> Optional[Path]:
    rd = os.environ.get("RUN_DIR") or os.environ.get("SENZA_AGENT_RUN_DIR")
    if rd:
        return Path(rd)
    return None


def _scratchpad_file() -> Optional[Path]:
    rd = _run_dir()
    if rd is None:
        return None
    return rd / "scratchpad.md"


def _episodic_ts() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


# ── Long-term memory ────────────────────────────────────────────────────────


def tool_remember(content: str = "") -> dict:
    """Write an important conclusion to long-term memory."""
    if not content or not content.strip():
        return _err("content must not be empty")
    text = content.strip()
    _state.long_term.append(text)
    p = _memory_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- {text}\n")
    except Exception as e:
        return _err(f"failed to persist memory: {e}")
    return _ok(f"remembered (long-term memory now has {len(_state.long_term)} entries)")


def tool_raw_append(content: str = "", path: str = "") -> dict:
    """Append raw memory (full-fidelity notes / transcript fragments) to an NDJSON file."""
    try:
        if not content:
            return _err("content must not be empty")
        if not path:
            path = os.environ.get("RAW_MEMORY_PATH", "./raw_memory.ndjson")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": int(time.time()), "content": content}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return _ok(f"raw appended: {p.resolve()}")
    except Exception as e:
        return _err(str(e))


# ── Scratchpad ──────────────────────────────────────────────────────────────


def _scratchpad_trim(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    lines = text.splitlines(keepends=True)
    head = "".join(lines[:3])
    body = text[len(head):]
    overflow = len(head) + len(body) - max_chars
    if overflow >= len(body):
        return text[:max_chars]
    body = body[overflow:]
    return head + body


def _scratchpad_read() -> str:
    p = _scratchpad_file()
    if p is not None and p.exists():
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            pass
    return ""


def _scratchpad_persist(text: str) -> None:
    p = _scratchpad_file()
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    except Exception:
        pass


def tool_scratchpad_get() -> dict:
    """Get current scratchpad content."""
    return _ok(_scratchpad_read())


def tool_scratchpad_set(content: str = "") -> dict:
    """Overwrite scratchpad (editable short-term working memory)."""
    max_chars = int(os.environ.get("SCRATCHPAD_MAX_CHARS", "2000"))
    text = (content or "").strip()
    text = _scratchpad_trim(text, max_chars)
    _scratchpad_persist(text)
    return _ok(f"scratchpad set ({len(text)} chars)")


def tool_scratchpad_append(content: str = "") -> dict:
    """Append to scratchpad."""
    if not content or not content.strip():
        return _err("content must not be empty")
    max_chars = int(os.environ.get("SCRATCHPAD_MAX_CHARS", "2000"))
    cur = _scratchpad_read()
    add = content.strip()
    cur = (cur.rstrip() + "\n" + add) if cur else add
    cur = _scratchpad_trim(cur, max_chars)
    _scratchpad_persist(cur)
    return _ok(f"scratchpad appended ({len(cur)} chars)")


# ── Recall history (stub — needs session history access) ────────────────────


def tool_recall_history(last_n: int = 12, query: str = "", seg: int = -1) -> dict:
    """Recall archived raw execution records (stub: session history not directly accessible)."""
    return _ok(
        "recall_history is not available in this runtime — "
        "session history is managed by the Senza harness and not directly accessible. "
        "Use scratchpad_get or remember for working memory."
    )


# ── Think / Goal ────────────────────────────────────────────────────────────


def tool_think(thought: str = "") -> dict:
    """Record a thought for deep analysis. Performs no external action."""
    return _ok(f"thought recorded ({len(thought)} chars)")


def tool_set_goal(new_goal: str = "", reason: str = "") -> dict:
    """Modify the current goal (for sub-goal decomposition or goal adjustment)."""
    if not new_goal or not new_goal.strip():
        return _err("new_goal must not be empty")
    old = _state.goal
    _state.goal = new_goal.strip()
    return _ok({"old_goal": old, "new_goal": _state.goal, "reason": reason or ""})


# ── File outline ────────────────────────────────────────────────────────────


def tool_file_outline(path: str = "") -> dict:
    """Extract structural outline (classes, functions, methods with line numbers)."""
    try:
        path2 = os.path.expandvars(os.path.expanduser(path))
        p = Path(path2)
        if not p.exists():
            return _err(f"file does not exist: {path}")

        content = p.read_text(encoding="utf-8")
        lines = content.splitlines()
        total = len(lines)

        if p.suffix.lower() == ".py":
            try:
                tree = ast.parse(content)
            except SyntaxError as e:
                return _err(f"Python syntax error: {e}")

            entries: list = []

            def _walk(nodes, indent=0):
                for node in nodes:
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        kind = "class" if isinstance(node, ast.ClassDef) else (
                            "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                        )
                        end = getattr(node, "end_lineno", "?")
                        entries.append(f"{'  ' * indent}{kind} {node.name}  [{node.lineno}-{end}]")
                        if isinstance(node, ast.ClassDef):
                            _walk(node.body, indent + 1)

            _walk(tree.body)
            if not entries:
                return _ok(f"[{p.name}]  {total} lines, no class/function definitions found")
            return _ok(f"[{p.name}]  {total} lines\n" + "\n".join(entries))

        patterns = [
            (r"^(\s*)(export\s+)?(default\s+)?(async\s+)?function\s+(\w+)", "function"),
            (r"^(\s*)(export\s+)?(abstract\s+)?class\s+(\w+)", "class"),
            (r"^(\s*)(\w+)\s*[:=]\s*(async\s+)?\(.*\)\s*=>", "arrow"),
            (r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(", "func"),
            (r"^(\s*)(public|private|protected|static|virtual|override).*\s(\w+)\s*\([^)]*\)\s*\{", "method"),
            (r"^(\w[\w\s\*]+)\s+(\w+)\s*\([^)]*\)\s*\{", "func"),
        ]
        entries2: list = []
        for i, line in enumerate(lines, 1):
            for pat, kind in patterns:
                m = re.match(pat, line)
                if m:
                    entries2.append(f"  {kind:<8} {line.strip()[:80]}  [line {i}]")
                    break

        if not entries2:
            return _ok(f"[{p.name}]  {total} lines (no function/class declarations recognised)")
        return _ok(f"[{p.name}]  {total} lines\n" + "\n".join(entries2))
    except Exception as ex:
        return _err(str(ex))


# ── Analyze content (uses Senza provider for independent LLM call) ──────────


def tool_analyze_content(
    sources: list = None,
    question: str = "",
    model: str = "",
    max_tokens: int = 4000,
) -> dict:
    """Merge multiple files/texts into a large context and make an independent LLM call for deep analysis."""
    if sources is None:
        sources = []
    sections: list = []
    load_errors: list = []
    total_chars = 0
    char_limit = int(os.environ.get("ANALYZE_CONTENT_CHAR_LIMIT", str(400_000)))

    for src in (sources or []):
        if isinstance(src, str):
            src = {"path": src}
        if not isinstance(src, dict):
            load_errors.append(f"invalid source format: {src!r}")
            continue

        if "path" in src:
            raw_path = os.path.expandvars(os.path.expanduser(src["path"]))
            p = Path(raw_path)
            label = src.get("label") or p.name
            if not p.exists():
                load_errors.append(f"file does not exist: {src['path']}")
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                load_errors.append(f"read failed {src['path']}: {e}")
                continue
        elif "text" in src:
            text = str(src["text"])
            label = src.get("label", "text fragment")
        else:
            load_errors.append(f"source missing 'path' or 'text': {src!r}")
            continue

        if total_chars + len(text) > char_limit:
            remaining = char_limit - total_chars
            if remaining <= 0:
                load_errors.append(f"reached char limit {char_limit}, skipped: {label}")
                continue
            text = text[:remaining]
            load_errors.append(f"warning: {label} truncated to {remaining} chars")

        total_chars += len(text)
        sections.append(f"=== {label} ===\n{text}")

    if not sections:
        err = "no content to analyze." + (" errors: " + "; ".join(load_errors) if load_errors else "")
        return _err(err)

    combined = "\n\n".join(sections)
    warn_block = ("\n\n[load warnings]\n" + "\n".join(load_errors)) if load_errors else ""
    user_msg = (
        f"The following is content to analyze ({len(sections)} sources, {total_chars:,} chars):\n\n"
        f"{combined}{warn_block}\n\n---\nPlease answer the following:\n{question}"
    )

    try:
        import senza

        api_key = os.environ.get("SENZA_AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("SENZA_AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        if not api_key:
            return _err("no API key configured (SENZA_AGENT_API_KEY or OPENAI_API_KEY)")
        provider = senza.providers.openai(api_key=api_key, base_url=base_url or None)
        result_text = provider.chat(user_msg)
    except Exception as e:
        return _err(f"LLM call failed: {e}")

    header = f"[analyze_content] analyzed {len(sections)} sources, {total_chars:,} chars (independent call)\n"
    if load_errors:
        header += "[load warnings] " + "; ".join(load_errors) + "\n"
    header += "-" * 40 + "\n"
    return _ok(header + result_text)


# ── Completion report ───────────────────────────────────────────────────────


def _normalize_report_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def tool_submit_completion_report(
    goal_understanding: str = "",
    completed_work: Any = None,
    remaining_gaps: Any = None,
    evidence_type: str = "none",
    evidence: Any = None,
    outcome: str = "done",
    confidence: str = "medium",
) -> dict:
    """Submit a structured completion report for the acceptance gate."""
    report = {
        "goal_understanding": (goal_understanding or "").strip(),
        "completed_work": _normalize_report_list(completed_work),
        "remaining_gaps": _normalize_report_list(remaining_gaps),
        "evidence_type": (evidence_type or "none").strip().lower(),
        "evidence": _normalize_report_list(evidence),
        "outcome": (outcome or "done").strip().lower(),
        "confidence": (confidence or "medium").strip().lower(),
    }
    _state.completion_report = report
    return _ok(report)


# ── Advisor ─────────────────────────────────────────────────────────────────


def tool_request_advisor(reason: str = "") -> dict:
    """Actively request the senior advisor to intervene immediately after this turn."""
    _state.advisor_requested = True
    return _ok({
        "status": "advisor_scheduled",
        "note": "The advisor will intervene before the next turn to provide an independent strategic review.",
    })


def tool_consult_advisor(
    question: str = "",
    advisor: int = 1,
    model: Optional[str] = None,
    max_tokens: int = 4096,
) -> dict:
    """Consult a stronger 'advisor model' for an independent professional opinion."""
    if not question or not question.strip():
        return _err("question must not be empty")

    n = str(advisor).strip() or "1"
    if n not in ("1", "2"):
        n = "1"
    prefix = f"ADVISOR{n}_OPENAI_"

    base_url = (os.environ.get(prefix + "BASE_URL") or "").strip()
    api_key = (os.environ.get(prefix + "API_KEY") or "").strip()
    cfg_model = (os.environ.get(prefix + "MODEL") or "").strip()
    temp_str = (os.environ.get(prefix + "TEMPERATURE") or "").strip()

    if not base_url or not cfg_model:
        # Fallback to main senza-agent config
        base_url = os.environ.get("SENZA_AGENT_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
        api_key = os.environ.get("SENZA_AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        cfg_model = os.environ.get("SENZA_AGENT_MODEL") or os.environ.get("LLM_MODEL", "")

    if not base_url or not cfg_model:
        return _err(f"advisor model {n} is not configured")

    use_model = model or cfg_model
    try:
        import senza

        provider = senza.providers.openai(api_key=api_key or "local", base_url=base_url)
        text = provider.chat(question)
        return _ok(text)
    except Exception as e:
        return _err(str(e))


# ── Evolved tools: dynamic registration ─────────────────────────────────────


def _validate_evolved_tool_python_code(python_code: str) -> list:
    """Best-effort static validation for persisted tool recipes."""
    errors: list = []
    try:
        tree = ast.parse(textwrap.dedent(python_code))
    except SyntaxError as e:
        return [f"code syntax error: {e}"]

    has_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
    if not has_run:
        errors.append("python_code must define a function named `run`")

    return list(dict.fromkeys(errors))


def _build_tool_recipe(name: str, description: str, args_schema: dict, python_code: str) -> dict:
    return {
        "name": name,
        "description": description,
        "args_schema": args_schema if isinstance(args_schema, dict) else {},
        "python_code": textwrap.dedent(python_code).strip(),
    }


def tool_register_tool(
    name: str = "",
    description: str = "",
    args_schema: dict = None,
    python_code: str = "",
) -> dict:
    """Register a new tool at runtime (evolved tool)."""
    if args_schema is None:
        args_schema = {}
    if not name or not name.strip():
        return _err("name must not be empty")
    if name in _state.evolved_tools:
        return _err(f"tool '{name}' already exists; delete it first to overwrite")

    errors = _validate_evolved_tool_python_code(python_code)
    if errors:
        return _err("; ".join(errors))

    recipe = _build_tool_recipe(name, description, args_schema, python_code)
    _state.evolved_tools[name] = recipe
    _state.long_term.append(f"[tool evolution] registered new tool '{name}': {description}")
    return _ok(f"tool '{name}' registered successfully! You can now use it.")


def tool_validate_tool_recipe(
    name: str = "",
    description: str = "",
    args_schema: dict = None,
    python_code: str = "",
) -> dict:
    """Validate a candidate tool recipe without registering it."""
    if args_schema is None:
        args_schema = {}
    errors = _validate_evolved_tool_python_code(python_code)
    recipe = _build_tool_recipe(name, description, args_schema, python_code)
    return _ok({"ok": len(errors) == 0, "errors": errors, "recipe": recipe})


def tool_repair_tool_candidate(
    name: str = "",
    description: str = "",
    args_schema: dict = None,
    python_code: str = "",
) -> dict:
    """Store a validated candidate repair for an existing tool."""
    if args_schema is None:
        args_schema = {}
    if name not in _state.evolved_tools:
        return _err(f"tool '{name}' does not exist, cannot repair")

    validation = tool_validate_tool_recipe(
        name=name, description=description, args_schema=args_schema, python_code=python_code,
    )
    val_output = validation.get("output", {}) if validation.get("status") == "ok" else {}
    if not val_output.get("ok"):
        _state.repair_failures.setdefault(name, []).append({"name": name, "errors": val_output.get("errors", [])})
        return _err("; ".join(val_output.get("errors", [])) or "candidate failed validation", output=val_output)

    candidate = dict(val_output["recipe"])
    candidate["validation"] = {"ok": True, "errors": []}
    _state.repair_candidates[name] = candidate
    _state.long_term.append(f"[tool repair candidate] generated pending repair for '{name}'")
    return _ok({"name": name, "candidate_stored": True, "validation": candidate["validation"]})


def tool_promote_tool_candidate(name: str = "") -> dict:
    """Promote a validated repair candidate into the formal tool registry."""
    candidate = _state.repair_candidates.get(name)
    if not candidate:
        return _err(f"tool '{name}' has no pending candidate to promote")

    validation = candidate.get("validation", {})
    if not validation.get("ok"):
        errs = validation.get("errors") or ["candidate failed validation"]
        return _err("; ".join(errs))

    errors = _validate_evolved_tool_python_code(candidate.get("python_code", ""))
    if errors:
        _state.repair_failures.setdefault(name, []).append({"name": name, "errors": errors})
        return _err("; ".join(errors))

    previous = _state.evolved_tools.get(name)
    _state.evolved_tools[name] = _build_tool_recipe(
        name, candidate.get("description", ""), candidate.get("args_schema", {}), candidate.get("python_code", ""),
    )
    _state.repair_history.append({"name": name, "previous_recipe": previous, "promoted_recipe": _state.evolved_tools[name]})
    _state.repair_candidates.pop(name, None)
    _state.long_term.append(f"[tool repair] tool '{name}' candidate promoted to formal version")
    return _ok({"name": name, "promoted": True})


def tool_delete_tool(name: str = "", confirm: bool = False) -> dict:
    """Delete a deprecated evolved tool (built-in tools cannot be deleted)."""
    if name not in _state.evolved_tools:
        return _err(f"tool '{name}' does not exist or is a built-in tool")

    recipe = _state.evolved_tools[name]
    description = recipe.get("description", "(no description)") if isinstance(recipe, dict) else "(no description)"

    if not confirm:
        return _ok({
            "preview": True, "name": name, "description": description,
            "tip": "Call again with confirm=true to execute deletion after user confirmation.",
        })

    _state.evolved_tools.pop(name, None)
    _state.repair_candidates.pop(name, None)
    _state.long_term.append(f"[tool deletion] evolved tool '{name}' deleted. Original description: {description}")
    return _ok({"deleted": name, "tip": "tool deleted; call save_tools to persist this change."})


# ── Tool file persistence ───────────────────────────────────────────────────


def tool_save_tools(path: str = "") -> dict:
    """Save evolved tools and repair metadata to a standalone JSON file."""
    try:
        valid_tools: dict = {}
        invalid_tools: dict = {}
        for name, rec in _state.evolved_tools.items():
            python_code = rec.get("python_code", "") if isinstance(rec, dict) else ""
            errors = _validate_evolved_tool_python_code(python_code) if python_code else ["missing python_code"]
            if errors:
                invalid_tools[name] = {"errors": errors, "recipe": rec}
            else:
                valid_tools[name] = rec

        payload = {
            "version": 1,
            "tools": valid_tools,
            "repair_candidates": _state.repair_candidates,
            "repair_failures": _state.repair_failures,
            "repair_history": _state.repair_history,
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return _ok({"path": str(p.resolve()), "saved": len(valid_tools), "skipped_invalid": len(invalid_tools)})
    except Exception as e:
        return _err(str(e))


def tool_load_tools(path: str = "", overwrite: bool = False) -> dict:
    """Load evolved tools from a standalone JSON tool file."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return _err("tool file must be a JSON object")

        tools_dict = payload.get("tools", {})
        repair_candidates = payload.get("repair_candidates", {})
        if not isinstance(tools_dict, dict):
            return _err("tools field must be a dict")

        _state.evolved_tools = tools_dict
        _state.repair_candidates = repair_candidates if isinstance(repair_candidates, dict) else {}
        _state.repair_failures = list(payload.get("repair_failures", []))
        _state.repair_history = list(payload.get("repair_history", []))

        restored = skipped = invalid = 0
        for name, rec in tools_dict.items():
            if not isinstance(rec, dict):
                invalid += 1
                continue
            python_code = rec.get("python_code", "")
            errors = _validate_evolved_tool_python_code(python_code)
            if errors:
                invalid += 1
                continue
            restored += 1

        return _ok({"restored": restored, "skipped": skipped, "invalid": invalid})
    except Exception as e:
        return _err(str(e))


# ── Episodic memory (JSONL) ─────────────────────────────────────────────────


def _normalize_tags(tags) -> list:
    raw: list
    if isinstance(tags, str):
        raw = [tags]
    elif tags:
        raw = [str(t) for t in tags]
    else:
        raw = []
    seen: set = set()
    out: list = []
    for item in raw:
        for part in re.split(r"[,，]", item):
            p = part.strip()
            if not p:
                continue
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    return out


def tool_append_episodic(path: str = "", summary: str = "", tags: str = "") -> dict:
    """Append a task execution record to the fine-grained memory file (JSONL)."""
    try:
        if not summary or not summary.strip():
            return _err("summary must not be empty")

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if isinstance(tags, str) else list(tags)
        raw_goal = (_state.goal or "")[:200]
        entry = {"ts": _episodic_ts(), "goal": raw_goal, "summary": summary.strip(), "tags": tag_list}

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return _ok({"appended": True, "path": str(p.resolve())})
    except Exception as e:
        return _err(str(e))


def tool_search_episodic(path: str = "", keyword: str = "", limit: int = 20) -> dict:
    """Read fine-grained memory file, filter by keyword, return the most recent N records."""
    try:
        p = Path(path)
        if not p.exists():
            return _ok({"entries": [], "total": 0})

        entries: list = []
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        if keyword:
            kw = keyword.lower()
            entries = [
                e for e in entries
                if kw in e.get("goal", "").lower()
                or kw in e.get("summary", "").lower()
                or any(kw in t.lower() for t in e.get("tags", []))
            ]

        total = len(entries)
        result = entries[-limit:] if total > limit else entries
        return _ok({"entries": result, "total": total})
    except Exception as e:
        return _err(str(e))


# ── Macro concept memory (Markdown) ────────────────────────────────────────


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")


def _normalize_heading_title(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"^#{1,6}[ \t]*", "", s).strip()
    return s.casefold()


def _find_section_span(lines: list, section: str) -> Optional[Tuple[int, int, int]]:
    target = _normalize_heading_title(section)
    if not target:
        return None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m or _normalize_heading_title(m.group(2)) != target:
            continue
        level = len(m.group(1))
        end = len(lines)
        for j in range(i + 1, len(lines)):
            m2 = _HEADING_RE.match(lines[j])
            if m2 and len(m2.group(1)) <= level:
                end = j
                break
        return i, end, level
    return None


def tool_save_concept(path: str = "", content: str = "", section: str = "") -> dict:
    """Write macro working memory to a Markdown file and sync to state."""
    try:
        if not content or not content.strip():
            return _err("content must not be empty")
        p = Path(os.path.expandvars(os.path.expanduser(path)))
        p.parent.mkdir(parents=True, exist_ok=True)
        body = content.strip()
        section = (section or "").strip()

        if not section:
            new_text = body + "\n"
            mode = "full"
        else:
            title = re.sub(r"^#{1,6}[ \t]*", "", section).strip()
            existing = p.read_text(encoding="utf-8") if p.exists() else ""
            lines = existing.splitlines()
            span = _find_section_span(lines, title)
            level = span[2] if span else 2
            heading = lines[span[0]] if span else f"{'#' * level} {title}"

            body_lines = body.splitlines()
            first = next((ln for ln in body_lines if ln.strip()), "")
            m_first = _HEADING_RE.match(first)
            if m_first and _normalize_heading_title(m_first.group(2)) == _normalize_heading_title(title):
                block_lines = [heading] + body_lines[body_lines.index(first) + 1:]
            else:
                block_lines = [heading] + body_lines

            if span:
                start, end, _ = span
                new_lines = lines[:start] + block_lines + [""] + lines[end:]
                while len(new_lines) > start + len(block_lines) + 1 and \
                        new_lines[start + len(block_lines) + 1].strip() == "":
                    del new_lines[start + len(block_lines) + 1]
                mode = "section_replaced"
            else:
                new_lines = (lines + [""] if lines and lines[-1].strip() else list(lines)) + block_lines
                mode = "section_appended"
            new_text = "\n".join(new_lines).rstrip() + "\n"

        p.write_text(new_text, encoding="utf-8")
        _state.concept_memory = new_text.strip()
        return _ok({"path": str(p.resolve()), "mode": mode, "section": section or None, "chars": len(new_text)})
    except Exception as e:
        return _err(str(e))


def tool_read_concept(path: str = "") -> dict:
    """Read macro working memory file and load into state."""
    try:
        p = Path(path)
        if not p.exists():
            return _ok({"content": "", "exists": False})
        content = p.read_text(encoding="utf-8").strip()
        _state.concept_memory = content
        return _ok({"content": content, "chars": len(content)})
    except Exception as e:
        return _err(str(e))


def tool_persist_runtime_patches(path: str = "./AGENTS.md") -> dict:
    """Write accumulated runtime format patches to AGENTS.md for future runs."""
    try:
        patches: list = _state.runtime_patches or []
        if not patches:
            return _ok("no runtime patches, skipping write")
        p = Path(path)
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        section = "\n\n## Runtime experience (auto-generated)\n" + "\n".join(f"- {rule}" for rule in patches) + "\n"
        existing_stripped = re.sub(r"\n\n## Runtime experience \(auto-generated\)\n.*", "", existing, flags=re.DOTALL)
        new_content = existing_stripped.rstrip() + section
        p.write_text(new_content, encoding="utf-8")
        return _ok({"path": str(p.resolve()), "patches_written": len(patches), "rules": patches})
    except Exception as e:
        return _err(str(e))


# ── Async background tasks ──────────────────────────────────────────────────


def tool_shell_bg(command: str = "", timeout: int = 0) -> dict:
    """Start a shell command in the background; returns job_id immediately."""
    if not command or not command.strip():
        return _err("command must not be empty")
    mgr = _async_mod.get_manager()
    t = int(timeout) if timeout and int(timeout) > 0 else None
    job_id = mgr.start_shell(command, timeout=t)
    return _ok({"job_id": job_id, "tip": "command started in background; use job_wait to check, job_cancel to stop."})


def tool_job_wait(job_id: str = "", wait: int = 10) -> dict:
    """Query background job status, waiting up to `wait` seconds for completion."""
    if not job_id:
        return _err("job_id must not be empty")
    mgr = _async_mod.get_manager()
    info = mgr.peek(job_id, wait_secs=float(max(0, int(wait))))
    if "error" in info:
        return _err(info["error"])
    status = info["status"]
    rc = info.get("returncode")
    if status == "running":
        succeeded = True
    elif status == "done":
        succeeded = (rc == 0)
    else:
        succeeded = False
    if succeeded:
        return _ok(info)
    return {"status": "error", "error": f"job {status}", "output": info}


def tool_job_cancel(job_id: str = "") -> dict:
    """Force-terminate a running background job (kills the entire process tree)."""
    if not job_id:
        return _err("job_id must not be empty")
    mgr = _async_mod.get_manager()
    result = mgr.cancel(job_id)
    if "error" in result:
        return _err(result["error"])
    return _ok(result)


def tool_jobs_list() -> dict:
    """List all background jobs and their current status."""
    mgr = _async_mod.get_manager()
    jobs = mgr.list_jobs()
    mgr.cleanup()
    if not jobs:
        return _ok("(no background jobs)")
    return _ok(jobs)


def tool_wait_for_job(job_id: str = "", check_interval: int = 15) -> dict:
    """Enter lightweight wait mode until the specified background job completes."""
    if not job_id:
        return _err("job_id must not be empty")
    mgr = _async_mod.get_manager()
    jobs = {j["job_id"] for j in mgr.list_jobs()}
    if job_id not in jobs:
        return _err(f"job {job_id} not found; start it with shell_bg first")
    _state.yield_waiting_job = {"job_id": job_id, "interval": max(5, int(check_interval))}
    return _ok({"message": f"entered wait mode, will check {job_id} every {check_interval}s", "job_id": job_id})


# ── Environment watchers ────────────────────────────────────────────────────


def tool_watch_register(
    name: str = "",
    path: str = "",
    interval: int = 10,
    emit: str = "event",
    params: Optional[dict] = None,
    enabled: bool = True,
    desc: str = "",
) -> dict:
    """Register an environment watcher."""
    if not name or not path:
        return _err("name and path are required")
    mgr = _watcher_mod.get_manager()
    result = mgr.register(
        name=name, path=path, interval=int(interval), emit=emit,
        params=params or {}, enabled=bool(enabled), desc=desc,
    )
    if not result.get("ok"):
        return _err(result.get("error", "registration failed"))
    return _ok(result)


def tool_watch_unregister(name: str = "") -> dict:
    """Unregister a watcher (code file is not deleted)."""
    if not name:
        return _err("name is required")
    mgr = _watcher_mod.get_manager()
    result = mgr.unregister(name)
    if not result.get("ok"):
        return _err(result.get("error", "unregister failed"))
    return _ok(result)


def tool_watch_enable(name: str = "") -> dict:
    """Enable a registered watcher."""
    if not name:
        return _err("name is required")
    mgr = _watcher_mod.get_manager()
    result = mgr.set_enabled(name, True)
    if not result.get("ok"):
        return _err(result.get("error", "enable failed"))
    return _ok(result)


def tool_watch_disable(name: str = "") -> dict:
    """Disable a watcher (entry is kept, just not scheduled)."""
    if not name:
        return _err("name is required")
    mgr = _watcher_mod.get_manager()
    result = mgr.set_enabled(name, False)
    if not result.get("ok"):
        return _err(result.get("error", "disable failed"))
    return _ok(result)


def tool_watch_update(
    name: str = "",
    interval: Optional[int] = None,
    emit: Optional[str] = None,
    params: Optional[dict] = None,
    enabled: Optional[bool] = None,
    desc: Optional[str] = None,
    path: Optional[str] = None,
) -> dict:
    """Update a watcher's fields (only pass what needs changing)."""
    if not name:
        return _err("name is required")
    mgr = _watcher_mod.get_manager()
    fields = {
        "interval": interval, "emit": emit, "params": params,
        "enabled": enabled, "desc": desc, "path": path,
    }
    result = mgr.update(name, **fields)
    if not result.get("ok"):
        return _err(result.get("error", "update failed"))
    return _ok(result)


def tool_watch_list() -> dict:
    """List all registered watchers and their status."""
    mgr = _watcher_mod.get_manager()
    entries = mgr.list_entries()
    if not entries:
        return _ok("(no registered watchers)")
    return _ok(entries)


# ── Environment info ────────────────────────────────────────────────────────


def tool_get_env_info() -> dict:
    """Return basic environment info: current datetime and working directory."""
    now = datetime.now()
    cwd = os.getcwd()
    return _ok({
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": now.strftime("%A"),
        "cwd": cwd,
    })


# ── Image / video loading ───────────────────────────────────────────────────


_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
)


def _sniff_image_mime(raw: bytes) -> Optional[str]:
    for magic, mime in _IMAGE_MAGIC:
        if raw.startswith(magic):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalise_image(raw: bytes) -> Tuple[str, str]:
    """Convert raw image bytes to base64 + MIME type."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        fmt = (img.format or "").upper()
        if fmt in ("JPEG", "JPG"):
            if img.mode in ("RGBA", "P", "LA"):
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=95)
            else:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=95)
            return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
        else:
            if img.mode not in ("RGB", "RGBA", "L", "LA"):
                img = img.convert("RGBA")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode(), "image/png"
    except ImportError:
        mime = "image/jpeg"
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            mime = "image/gif"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            mime = "image/webp"
        return base64.b64encode(raw).decode(), mime


def _fetch_remote_image(url: str, timeout: float = 20.0) -> Tuple[Optional[bytes], str, bool]:
    """Download a remote image and verify it is actually image bytes."""
    import urllib.error as _ue
    import urllib.request as _ur

    max_bytes = int(os.environ.get("LOAD_IMAGE_MAX_BYTES", str(24 * 1024 * 1024)))
    req = _ur.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; senza-agent/1.0; +load_image)",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })
    try:
        with _ur.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
    except _ue.HTTPError as e:
        return None, f"download failed: HTTP {e.code} {e.reason}", e.code in (400, 401, 403, 404, 410, 451)
    except Exception as e:
        return None, f"download failed: {type(e).__name__}: {e}", False

    if not raw:
        return None, "downloaded 0 bytes", False
    if len(raw) > max_bytes:
        return None, f"image exceeds {max_bytes // 1024 // 1024}MB limit", True

    if _sniff_image_mime(raw):
        return raw, "", False

    return None, "URL did not return recognised image bytes", True


def tool_load_image(path: str = "", caption: str = "") -> dict:
    """Load a local image or remote URL into the conversation context (multimodal)."""
    if _state.vision_supported is False:
        return _err("current LLM backend does not support multimodal (vision)")

    import urllib.parse

    p = (path or "").strip()
    if not p:
        return _err("path must not be empty")

    if p.startswith("http://") or p.startswith("https://"):
        if isinstance(_state.bad_image_urls, dict) and p in _state.bad_image_urls:
            return _err(f"this URL was previously confirmed unusable as an image: {_state.bad_image_urls[p]}")

        raw, err, permanent = _fetch_remote_image(p)
        if err:
            if permanent:
                _state.bad_image_urls[p] = err.split("\n")[0]
            return _err(err)

        try:
            data, mime = _normalise_image(raw)
        except Exception as e:
            _state.bad_image_urls[p] = f"decode failed: {e}"
            return _err(f"image downloaded but cannot decode ({len(raw)} bytes): {e}")

        return _ok({"loaded": p, "mime": mime, "size_kb": len(data) // 1024, "caption": caption})

    fp = Path(p)
    if not fp.is_absolute():
        fp = Path(os.getcwd()) / fp
    if not fp.exists():
        return _err(f"file does not exist: {fp}")

    try:
        raw = fp.read_bytes()
        data, mime = _normalise_image(raw)
    except Exception as e:
        return _err(f"failed to read image: {e}")

    return _ok({"loaded": fp.name, "mime": mime, "size_kb": len(data) // 1024, "caption": caption})


def tool_load_video(
    path: str = "",
    interval: float = 2.0,
    max_frames: int = 16,
    start_time: float = 0.0,
    end_time: float = -1.0,
    caption: str = "",
) -> dict:
    """Extract keyframes from a local video file for multimodal analysis."""
    if _state.vision_supported is False:
        return _err("current LLM backend does not support multimodal (vision)")

    try:
        import cv2
    except ImportError:
        return _err("missing dependency opencv-python; run: pip install opencv-python")

    fp = Path((path or "").strip())
    if not fp.is_absolute():
        fp = Path(os.getcwd()) / fp
    if not fp.exists():
        return _err(f"file does not exist: {fp}")

    cap = cv2.VideoCapture(str(fp))
    if not cap.isOpened():
        return _err(f"cannot open video file: {fp.name}")

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration = total_frames / fps

        start_frame = max(0, int(start_time * fps))
        end_frame = total_frames if end_time < 0 else min(total_frames, int(end_time * fps))
        if start_frame >= end_frame:
            return _err(f"invalid time range: start_time={start_time}s >= end_time (duration {duration:.1f}s)")

        step_frames = max(1, int(fps * interval))
        candidates = list(range(start_frame, end_frame, step_frames))
        if len(candidates) > max_frames:
            step = len(candidates) / max_frames
            candidates = [candidates[int(i * step)] for i in range(max_frames)]

        extracted = 0
        for frame_idx in candidates:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok:
                continue
            ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok2:
                continue
            extracted += 1
    finally:
        cap.release()

    if extracted == 0:
        return _err("could not extract any frames from the video")

    range_desc = f"{start_time:.1f}s-{duration:.1f}s" if end_time < 0 else f"{start_time:.1f}s-{end_time:.1f}s"
    summary = (
        f"extracted {extracted} frames from {fp.name} "
        f"(duration {duration:.1f}s, range {range_desc}, fps={fps:.1f}, interval {interval}s)"
    )
    return _ok({"summary": summary, "frames": extracted, "caption": caption})


# ── SSH execute ─────────────────────────────────────────────────────────────


def tool_ssh_execute(
    host: str = "",
    port: int = 22,
    username: str = "",
    password: Optional[str] = None,
    command: str = "",
    timeout: float = 30,
    key_file: Optional[str] = None,
    sudo_password: Optional[str] = None,
    **kwargs,
) -> dict:
    """Execute a command on a remote server via SSH."""
    if not host or not username or not command:
        return _err("missing required parameters: host, username, command")
    if not password and not key_file:
        return _err("must provide either password or key_file")

    if sudo_password:
        if "sudo" in command:
            command = command.replace("sudo", "sudo -S", 1)
        else:
            command = "sudo -S " + command
        escaped = sudo_password.replace("'", "'\\''")
        command = f"printf '%s\\n' '{escaped}' | " + command

    try:
        import paramiko
    except ImportError:
        return _err("missing dependency paramiko; run: pip install paramiko")

    try:
        import time as _time

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": host, "port": int(port), "username": username,
            "timeout": min(float(timeout), 15),
        }
        if key_file:
            connect_kwargs["key_filename"] = key_file
        else:
            connect_kwargs["password"] = password

        ssh.connect(**connect_kwargs)

        try:
            stdin, stdout, stderr = ssh.exec_command(command, timeout=float(timeout))
            channel = stdout.channel

            start = _time.time()
            stdout_data = b""
            stderr_data = b""

            while True:
                ih = _state.interrupt_handler
                if ih and getattr(ih, "force_stop", False):
                    ih.force_stop = False
                    channel.close()
                    return _err("SSH command interrupted by user (/stop)")

                if _time.time() - start > float(timeout):
                    channel.close()
                    return _err(f"SSH command timed out ({timeout}s)")

                if channel.exit_status_ready():
                    break

                if channel.recv_ready():
                    stdout_data += channel.recv(4096)
                if channel.recv_stderr_ready():
                    stderr_data += channel.recv_stderr(4096)

                _time.sleep(0.1)

            stdout_data += stdout.read()
            stderr_data += stderr.read()
            returncode = channel.recv_exit_status()

            stdout_str = stdout_data.decode("utf-8", errors="replace")
            stderr_str = stderr_data.decode("utf-8", errors="replace")

            parts = []
            if stdout_str:
                parts.append("[stdout]\n" + stdout_str)
            if stderr_str:
                parts.append("[stderr]\n" + stderr_str)
            output = "\n".join(parts)

            if returncode == 0:
                return _ok(output)
            return {"status": "error", "error": f"exit code: {returncode}", "output": output}
        finally:
            ssh.close()

    except paramiko.AuthenticationException:
        return _err("SSH authentication failed: wrong username or password")
    except paramiko.SSHException as e:
        return _err(f"SSH error: {e}")
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")
