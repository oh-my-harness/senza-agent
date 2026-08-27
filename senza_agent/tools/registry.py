"""Registry: wraps each standard tool function in ``senza.create_tool``.

``get_standard_tools()`` returns a list of Senza ``Tool`` objects ready to be
passed to ``senza.AgentHarness`` (or any Senza builder that accepts tools).
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable

import senza
from . import standard


def _adapt(callback: Callable) -> Callable:
    """Wrap a tool callback so it receives unpacked kwargs from the args dict.

    The Senza SDK's Rust layer always calls ``cb(args_dict, ctx)`` with two
    positional arguments.  For callbacks declared as ``def tool(foo: str,
    bar: int)`` this passes the *entire* args dict as ``foo`` and the ctx
    object as ``bar`` — the dict is never unpacked into keyword arguments.

    The SDK's own ``_wrap_tool_callback`` only helps 1-param callbacks (it
    drops ``ctx``), but still passes the raw dict, so ``tool(question: str)``
    receives ``question = {"question": "..."}`` instead of the string.

    This adapter wraps *every* callback to accept ``(args: dict, ctx)`` and
    unpack ``args`` as ``**kwargs`` before calling the real function.  It
    also filters out keys that the real function doesn't accept, so extra
    fields in the LLM's tool-call payload don't cause ``TypeError``.
    """
    try:
        sig = inspect.signature(callback)
    except (TypeError, ValueError):
        # Builtins / C functions — assume it already takes (args, ctx).
        return callback

    params = sig.parameters
    non_var = [p for p in params.values()
               if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]

    # Functions that already declare (args: dict, ctx) — no wrapping needed.
    if len(non_var) >= 2:
        p0_ann = non_var[0].annotation
        if p0_ann is dict or p0_ann == dict or (
            isinstance(p0_ann, str) and p0_ann == "dict"
        ):
            return callback

    has_var_keyword = any(p.kind == p.VAR_KEYWORD for p in params.values())

    accepted_names: set[str] | None = None
    if not has_var_keyword:
        accepted_names = {
            name for name, p in params.items()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        }

    def _wrapped(args: dict, ctx: Any = None):
        if not isinstance(args, dict):
            # LLM passed a non-dict (string, etc.) — forward positionally.
            return callback(args)
        if has_var_keyword:
            return callback(**args)
        filtered = {k: v for k, v in args.items() if k in accepted_names}
        return callback(**filtered)

    return _wrapped


def _str_schema(props: dict, required: list = None) -> dict:
    """Build a JSON Schema dict from {name: description} pairs."""
    properties = {}
    for name, desc in props.items():
        properties[name] = {"type": "string", "description": desc}
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _mixed_schema(props: dict, required: list = None) -> dict:
    """Build a JSON Schema dict from {name: {type, description}} pairs."""
    properties = {}
    for name, spec in props.items():
        if isinstance(spec, str):
            properties[name] = {"type": "string", "description": spec}
        elif isinstance(spec, dict):
            entry = dict(spec)
            entry.setdefault("type", "string")
            properties[name] = entry
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def get_standard_tools() -> list:
    """Return the list of Senza Tool objects for all Tier 2 tools."""
    tools = []

    # ── Memory ──────────────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="remember",
        description="Write an important conclusion to long-term memory for future reference.",
        parameters=_str_schema({"content": "The content to remember"}, ["content"]),
        callback=_adapt(standard.tool_remember),
    ))
    tools.append(senza.create_tool(
        name="raw_append",
        description="Append raw memory (full-fidelity notes / transcript fragments) to an NDJSON file.",
        parameters=_str_schema({
            "content": "The raw content to append",
            "path": "(optional) file path; defaults to env RAW_MEMORY_PATH or ./raw_memory.ndjson",
        }, ["content"]),
        callback=_adapt(standard.tool_raw_append),
    ))
    tools.append(senza.create_tool(
        name="recall_history",
        description="Recall archived raw execution records (fallback when compression lost detail).",
        parameters=_mixed_schema({
            "last_n": {"type": "integer", "description": "(optional, default 12) return the last N records"},
            "query": {"type": "string", "description": "(optional) keyword to filter"},
            "seg": {"type": "integer", "description": "(optional, default -1=current segment) segment number"},
        }),
        callback=_adapt(standard.tool_recall_history),
    ))

    # ── Scratchpad ──────────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="scratchpad_get",
        description="Read the current scratchpad (editable short-term working memory).",
        parameters=_str_schema({}),
        callback=_adapt(standard.tool_scratchpad_get),
    ))
    tools.append(senza.create_tool(
        name="scratchpad_set",
        description="Overwrite the scratchpad (replaces existing content).",
        parameters=_str_schema({"content": "Scratchpad content"}, ["content"]),
        callback=_adapt(standard.tool_scratchpad_set),
    ))
    tools.append(senza.create_tool(
        name="scratchpad_append",
        description="Append content to the scratchpad.",
        parameters=_str_schema({"content": "Content to append"}, ["content"]),
        callback=_adapt(standard.tool_scratchpad_append),
    ))

    # ── Think / Goal ────────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="think",
        description="Record a thought for deep analysis and reasoning. Performs no external action.",
        parameters=_str_schema({"thought": "Your analysis content"}, ["thought"]),
        callback=_adapt(standard.tool_think),
    ))
    tools.append(senza.create_tool(
        name="set_goal",
        description="Modify the current goal (for sub-goal decomposition or goal adjustment).",
        parameters=_str_schema({
            "new_goal": "New goal description",
            "reason": "Reason for changing the goal",
        }, ["new_goal", "reason"]),
        callback=_adapt(standard.tool_set_goal),
    ))

    # ── File analysis ───────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="file_outline",
        description="Extract structural outline (classes, functions, methods with line numbers) without reading the full file.",
        parameters=_str_schema({"path": "File path"}, ["path"]),
        callback=_adapt(standard.tool_file_outline),
    ))
    tools.append(senza.create_tool(
        name="analyze_content",
        description="Merge multiple files/texts into a large context and make an independent LLM call for deep analysis. Original content does not enter the main conversation.",
        parameters=_mixed_schema({
            "sources": {"type": "array", "description": "List of sources: path string / {path} / {text, label}"},
            "question": {"type": "string", "description": "Question or analysis task"},
            "model": {"type": "string", "description": "(optional) override model name"},
            "max_tokens": {"type": "integer", "description": "(optional) max output tokens, default 4000"},
        }, ["sources", "question"]),
        callback=_adapt(standard.tool_analyze_content),
    ))

    # ── Completion / Advisor ────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="request_advisor",
        description="Actively request the senior advisor to intervene immediately after this turn for an independent strategic review.",
        parameters=_str_schema({"reason": "(optional) reason for requesting guidance"}),
        callback=_adapt(standard.tool_request_advisor),
    ))
    tools.append(senza.create_tool(
        name="consult_advisor",
        description="Consult a stronger advisor model for an independent professional opinion on a complex question.",
        parameters=_mixed_schema({
            "question": {"type": "string", "description": "The question to consult"},
            "advisor": {"type": "integer", "description": "Advisor number 1 or 2 (default 1)"},
            "model": {"type": "string", "description": "(optional) model name override"},
            "max_tokens": {"type": "integer", "description": "(optional) max output tokens, default 4096"},
        }, ["question"]),
        callback=_adapt(standard.tool_consult_advisor),
    ))

    # ── Evolved tools ───────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="save_tools",
        description="Save evolved tools and repair metadata to a standalone JSON file.",
        parameters=_str_schema({"path": "Tool file path (e.g. ./agent_tools.json)"}, ["path"]),
        callback=_adapt(standard.tool_save_tools),
    ))
    tools.append(senza.create_tool(
        name="load_tools",
        description="Load evolved tools from a standalone JSON tool file and register them.",
        parameters=_mixed_schema({
            "path": {"type": "string", "description": "Tool file path"},
            "overwrite": {"type": "boolean", "description": "(optional) overwrite existing tools, default false"},
        }, ["path"]),
        callback=_adapt(standard.tool_load_tools),
    ))
    tools.append(senza.create_tool(
        name="append_episodic",
        description="Append a task execution record to the fine-grained memory file (JSONL).",
        parameters=_mixed_schema({
            "path": {"type": "string", "description": "Episodic memory file path"},
            "summary": {"type": "string", "description": "Summary paragraph (100-300 chars)"},
            "tags": {"type": "string", "description": "Comma-separated keywords for retrieval"},
        }, ["path", "summary"]),
        callback=_adapt(standard.tool_append_episodic),
    ))
    tools.append(senza.create_tool(
        name="search_episodic",
        description="Read fine-grained memory file, filter by keyword, return the most recent N records.",
        parameters=_mixed_schema({
            "path": {"type": "string", "description": "Episodic memory file path"},
            "keyword": {"type": "string", "description": "(optional) search keyword"},
            "limit": {"type": "integer", "description": "(optional) max results, default 20"},
        }, ["path"]),
        callback=_adapt(standard.tool_search_episodic),
    ))
    tools.append(senza.create_tool(
        name="save_concept",
        description="Write macro working memory to a Markdown file. Default: section mode (pass section= to replace one section).",
        parameters=_mixed_schema({
            "path": {"type": "string", "description": "Macro memory file path"},
            "content": {"type": "string", "description": "Section content (section mode) or full Markdown (full mode)"},
            "section": {"type": "string", "description": "(recommended) section title to write/replace"},
        }, ["path", "content"]),
        callback=_adapt(standard.tool_save_concept),
    ))
    tools.append(senza.create_tool(
        name="read_concept",
        description="Read macro working memory file and load into state for system prompt injection.",
        parameters=_str_schema({"path": "Macro memory file path"}, ["path"]),
        callback=_adapt(standard.tool_read_concept),
    ))
    tools.append(senza.create_tool(
        name="persist_runtime_patches",
        description="Write accumulated runtime format patches to AGENTS.md for future runs.",
        parameters=_str_schema({"path": "(optional) AGENTS.md path, default ./AGENTS.md"}),
        callback=_adapt(standard.tool_persist_runtime_patches),
    ))
    tools.append(senza.create_tool(
        name="validate_tool_recipe",
        description="Validate a candidate tool recipe without registering it.",
        parameters=_mixed_schema({
            "name": {"type": "string", "description": "Target tool name"},
            "description": {"type": "string", "description": "Tool description"},
            "args_schema": {"type": "object", "description": "Parameter description dict"},
            "python_code": {"type": "string", "description": "Python code defining run(**kwargs)->dict"},
        }, ["name", "description", "args_schema", "python_code"]),
        callback=_adapt(standard.tool_validate_tool_recipe),
    ))
    tools.append(senza.create_tool(
        name="repair_tool_candidate",
        description="Store a validated candidate repair for an existing tool (does not overwrite immediately).",
        parameters=_mixed_schema({
            "name": {"type": "string", "description": "Name of the tool to repair"},
            "description": {"type": "string", "description": "Repaired tool description"},
            "args_schema": {"type": "object", "description": "Repaired parameter description dict"},
            "python_code": {"type": "string", "description": "Repaired candidate Python code"},
        }, ["name", "description", "args_schema", "python_code"]),
        callback=_adapt(standard.tool_repair_tool_candidate),
    ))
    tools.append(senza.create_tool(
        name="promote_tool_candidate",
        description="Promote a validated repair candidate into the formal tool registry.",
        parameters=_str_schema({"name": "Tool name to promote"}, ["name"]),
        callback=_adapt(standard.tool_promote_tool_candidate),
    ))
    tools.append(senza.create_tool(
        name="register_tool",
        description="(Evolution) Define and register a brand-new tool at runtime.",
        parameters=_mixed_schema({
            "name": {"type": "string", "description": "New tool name (English, no spaces)"},
            "description": {"type": "string", "description": "Tool description"},
            "args_schema": {"type": "object", "description": "Parameter description dict"},
            "python_code": {"type": "string", "description": "Python code defining run(**kwargs)->dict"},
        }, ["name", "description", "args_schema", "python_code"]),
        callback=_adapt(standard.tool_register_tool),
    ))
    tools.append(senza.create_tool(
        name="delete_tool",
        description="(Evolution management) Delete a deprecated evolved tool. Must preview with confirm=false first.",
        parameters=_mixed_schema({
            "name": {"type": "string", "description": "Tool name to delete"},
            "confirm": {"type": "boolean", "description": "False=preview (default), True=execute deletion"},
        }, ["name"]),
        callback=_adapt(standard.tool_delete_tool),
    ))

    # ── Async background tasks ──────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="shell_bg",
        description="(Async) Start a shell command in the background; returns job_id immediately without blocking.",
        parameters=_mixed_schema({
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "integer", "description": "(optional) max seconds, 0 = unlimited"},
        }, ["command"]),
        callback=_adapt(standard.tool_shell_bg),
    ))
    tools.append(senza.create_tool(
        name="job_wait",
        description="Query background job status, waiting up to `wait` seconds for completion.",
        parameters=_mixed_schema({
            "job_id": {"type": "string", "description": "Job ID from shell_bg"},
            "wait": {"type": "integer", "description": "(optional) max wait seconds, default 10"},
        }, ["job_id"]),
        callback=_adapt(standard.tool_job_wait),
    ))
    tools.append(senza.create_tool(
        name="job_cancel",
        description="Force-terminate a running background job (kills the entire process tree).",
        parameters=_str_schema({"job_id": "Job ID from shell_bg"}, ["job_id"]),
        callback=_adapt(standard.tool_job_cancel),
    ))
    tools.append(senza.create_tool(
        name="jobs_list",
        description="List all background jobs and their current status.",
        parameters=_str_schema({}),
        callback=_adapt(standard.tool_jobs_list),
    ))

    # ── Environment watchers ────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="watch_register",
        description="(Environment) Register a watcher that runs periodically and injects output into context.",
        parameters=_mixed_schema({
            "name": {"type": "string", "description": "Unique identifier"},
            "path": {"type": "string", "description": "Absolute path to code file (.py or .sh)"},
            "interval": {"type": "integer", "description": "(optional) trigger interval seconds, default 10"},
            "emit": {"type": "string", "description": "(optional) event | live, default event"},
            "params": {"type": "object", "description": "(optional) params dict injected into code"},
            "enabled": {"type": "boolean", "description": "(optional) enabled, default true"},
            "desc": {"type": "string", "description": "(optional) description"},
        }, ["name", "path"]),
        callback=_adapt(standard.tool_watch_register),
    ))
    tools.append(senza.create_tool(
        name="watch_unregister",
        description="Unregister a watcher (code file is not deleted).",
        parameters=_str_schema({"name": "Watcher name"}, ["name"]),
        callback=_adapt(standard.tool_watch_unregister),
    ))
    tools.append(senza.create_tool(
        name="watch_enable",
        description="Enable a registered watcher.",
        parameters=_str_schema({"name": "Watcher name"}, ["name"]),
        callback=_adapt(standard.tool_watch_enable),
    ))
    tools.append(senza.create_tool(
        name="watch_disable",
        description="Disable a watcher (entry is kept, just not scheduled).",
        parameters=_str_schema({"name": "Watcher name"}, ["name"]),
        callback=_adapt(standard.tool_watch_disable),
    ))
    tools.append(senza.create_tool(
        name="watch_update",
        description="Update a watcher's fields (only pass what needs changing).",
        parameters=_mixed_schema({
            "name": {"type": "string", "description": "Watcher name"},
            "interval": {"type": "integer", "description": "(optional) new interval"},
            "emit": {"type": "string", "description": "(optional) event | live"},
            "params": {"type": "object", "description": "(optional) new params dict"},
            "enabled": {"type": "boolean", "description": "(optional) enabled"},
            "desc": {"type": "string", "description": "(optional) new description"},
            "path": {"type": "string", "description": "(optional) new code file path"},
        }, ["name"]),
        callback=_adapt(standard.tool_watch_update),
    ))
    tools.append(senza.create_tool(
        name="watch_list",
        description="List all registered watchers and their status.",
        parameters=_str_schema({}),
        callback=_adapt(standard.tool_watch_list),
    ))

    # ── Environment info ────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="get_env_info",
        description="Get basic environment info: current datetime and working directory.",
        parameters=_str_schema({}),
        callback=_adapt(standard.tool_get_env_info),
    ))

    # ── Image / video ───────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="load_image",
        description="Load a local image or remote URL into the conversation context (multimodal).",
        parameters=_str_schema({
            "path": "Image path (local file or http/https URL)",
            "caption": "(optional) caption text injected before the image",
        }, ["path"]),
        callback=_adapt(standard.tool_load_image),
    ))
    tools.append(senza.create_tool(
        name="load_video",
        description="Extract keyframes from a local video file for multimodal analysis.",
        parameters=_mixed_schema({
            "path": {"type": "string", "description": "Video file path"},
            "interval": {"type": "number", "description": "(optional) frame interval seconds, default 2.0"},
            "max_frames": {"type": "integer", "description": "(optional) max frames, default 16"},
            "start_time": {"type": "number", "description": "(optional) start time seconds, default 0"},
            "end_time": {"type": "number", "description": "(optional) end time seconds, default -1 = end"},
            "caption": {"type": "string", "description": "(optional) caption text"},
        }, ["path"]),
        callback=_adapt(standard.tool_load_video),
    ))

    # ── Ask user ─────────────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="ask_user",
        description=(
            "Ask the user a question and wait for their answer. "
            "Use this when you need clarification, a decision, or information "
            "that only the user can provide. The agent pauses until the user responds."
        ),
        parameters=_str_schema({"question": "The question to ask the user"}, ["question"]),
        callback=_adapt(standard.tool_ask_user),
    ))

    # ── SSH ─────────────────────────────────────────────────────────────────
    tools.append(senza.create_tool(
        name="ssh_execute",
        description="Execute a command on a remote server via SSH. Supports password/key auth and sudo.",
        parameters=_mixed_schema({
            "host": {"type": "string", "description": "Remote host or IP"},
            "port": {"type": "integer", "description": "SSH port, default 22"},
            "username": {"type": "string", "description": "Login username"},
            "password": {"type": "string", "description": "Login password (for password auth)"},
            "command": {"type": "string", "description": "Command to execute"},
            "timeout": {"type": "number", "description": "(optional) timeout seconds, default 30"},
            "key_file": {"type": "string", "description": "(optional) SSH private key file path"},
            "sudo_password": {"type": "string", "description": "(optional) sudo password"},
        }, ["host", "username", "command"]),
        callback=_adapt(standard.tool_ssh_execute),
    ))

    return tools
