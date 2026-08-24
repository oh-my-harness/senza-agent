"""Agent assembly: builds a Senza AgentHarness with all tools and behavior.

This module wires together all ported components:
- Config from config.py
- System prompt from system_prompt.py
- Behavior bundle (advisor, acceptance gate, wrapup) from behavior/
- Standard tools (~41) from tools/registry.py
- Graph tools (plan_create/revise/abandon) from tools/graph_tools.py
- Persistence from persistence.py
- Inspector from inspector.py

Usage:
    from senza_agent.config import load_config
    from senza_agent.agent import create_agent

    config = load_config()
    harness = create_agent(config)
    harness.prompt_and_collect("your task")
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from senza_agent.config import Config, load_config
from senza_agent.system_prompt import build_system_prompt
from senza_agent.behavior.state import AgentState
from senza_agent.behavior.bundle import BehaviorBundle
from senza_agent.persistence import RunPersistence


def _ensure_run_dir(config: Config) -> str:
    """Create the per-run directory and return its path."""
    runs_base = Path(config.sessions_dir) / "runs" if config.sessions_dir else Path.home() / ".senza-agent" / "runs"
    runs_base.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = runs_base / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return str(run_dir)


def _web_config_dict(config: Config) -> dict:
    """Convert WebConfig to dict for create_web_tools_plugin()."""
    from senza_agent.config import WebConfig
    wc: WebConfig = config.web
    return {
        "provider": wc.provider,
        "base_url": wc.base_url,
        "api_key": wc.api_key,
        "max_results": wc.max_results,
        "fetch_timeout_secs": wc.fetch_timeout_secs,
        "max_fetch_chars": wc.max_fetch_chars,
    }


def create_agent(config: Config) -> Any:
    """Build a Senza AgentHarness with full plugin stack and behavior.

    Args:
        config: Loaded configuration from load_config().

    Returns:
        A senza.AgentHarness ready for prompt_and_collect().
    """
    import senza

    # ── Per-run setup ───────────────────────────────────────────────────
    run_dir = _ensure_run_dir(config)
    persistence = RunPersistence(run_dir)

    # ── Agent state ─────────────────────────────────────────────────────
    state = AgentState(
        goal="",
        run_dir=run_dir,
        tools=[],  # will be filled after tool registration
    )

    # ── Behavior bundle (advisor + acceptance gate + wrapup) ────────────
    behavior = BehaviorBundle(state, config.behavior)

    # ── Provider ────────────────────────────────────────────────────────
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY", "")
    api_base = config.api_base or os.environ.get("OPENAI_API_BASE", "")
    provider = senza.providers.openai(
        api_key=api_key,
        base_url=api_base if api_base else None,
    )

    # ── Execution environment ───────────────────────────────────────────
    working_dir = config.working_dir or os.getcwd()
    env = senza.create_os_env(working_dir)

    # ── System prompt ───────────────────────────────────────────────────
    system_prompt = build_system_prompt(config)

    # ── Build harness ───────────────────────────────────────────────────
    builder = (
        senza.HarnessBuilder(config.model)
        .provider("*", provider)
        .system_prompt(system_prompt)
        .env(env)
        # File tools
        .plugin(senza.create_fs_tools_plugin())
        # Strategy plugins
        .plugin(senza.strategy.safety_defaults())
        .plugin(senza.strategy.loop_safety())
        .plugin(senza.strategy.status_panel())
        .plugin(senza.strategy.tool_output_guard(env))
        .plugin(senza.strategy.injection_filter())
        .plugin(senza.strategy.project_instruction(env))
        .plugin(senza.strategy.audit(config.audit_path))
        .plugin(senza.strategy.notify())
        # Web/code tools
        .plugin(senza.create_web_tools_plugin(_web_config_dict(config)))
        .tool(senza.create_code_exec_tool(timeout_secs=30))
        # Behavior tools (submit_completion_report, etc.)
        .tools(behavior.tools)
        # Behavior hooks (advisor after_turn, wrapup prepare_next_turn)
        .hooks(behavior.hooks)
        # Acceptance gate: validator checks completion report on final answer.
        # Simple conversations (no tools) pass automatically.
        .final_answer_validator(behavior.validator)
        .should_stop_hook(behavior.should_stop)
        .auto_compact(True)
        .retry(3, 1000)
    )

    # ── Session persistence ─────────────────────────────────────────────
    if config.sessions_dir:
        try:
            builder = builder.session_repo(
                senza.knowledge.jsonl_session_repo(config.sessions_dir)
            )
        except Exception:
            pass  # session_repo is optional

    # ── Budget ──────────────────────────────────────────────────────────
    budget_limit = config.behavior.budget_limit
    if budget_limit and budget_limit > 0:
        try:
            budget_hook = senza.create_budget_exceeded_hook(
                lambda ctx, spent: True  # allow continuation, wrapup will handle
            )
            builder = builder.budget(budget_limit, budget_hook)
        except Exception:
            pass

    # ── Usage ledger ────────────────────────────────────────────────────
    try:
        builder = builder.usage_ledger(senza.UsageLedger())
    except Exception:
        pass

    # ── Standard tools (remember, scratchpad, analyze_content, etc.) ────
    try:
        from senza_agent.tools.registry import get_standard_tools
        standard_tools = get_standard_tools()
        # Collect names already registered by behavior bundle to avoid duplicates.
        existing_names = set()
        if hasattr(behavior, "tools") and behavior.tools:
            for bt in behavior.tools:
                n = bt.name if isinstance(bt.name, str) else getattr(bt, "name", lambda: "?")()
                existing_names.add(n)
        standard_tools = [t for t in standard_tools if (t.name if isinstance(t.name, str) else t.name()) not in existing_names]
        if standard_tools:
            builder = builder.tools(standard_tools)
            state.tools = [t.name if isinstance(t.name, str) else t.name() for t in standard_tools]
    except Exception as e:
        print(f"Warning: failed to load standard tools: {e}", file=sys.stderr)
    # ── Graph tools (plan_create/revise/abandon) ────────────────────────
    try:
        from senza_agent.tools.graph_tools import get_graph_tools
        graph_tools = get_graph_tools()
        if graph_tools:
            builder = builder.tools(graph_tools)
    except Exception as e:
        print(f"Warning: failed to load graph tools: {e}", file=sys.stderr)

    # ── Web UI tools (web_show, terminal, file_tab, apps) ───────────────
    try:
        from senza_agent.tools.web_ui_tools import get_web_ui_tools
        web_ui_tools = get_web_ui_tools()
        if web_ui_tools:
            builder = builder.tools(web_ui_tools)
    except ImportError:
        pass  # web_ui_tools not yet implemented (B13 in progress)
    except Exception as e:
        print(f"Warning: failed to load web UI tools: {e}", file=sys.stderr)

    # ── Knowledge / memory ──────────────────────────────────────────────
    if config.knowledge_dir:
        try:
            source = senza.knowledge.local_source(config.knowledge_dir, "local")
            builder = builder.plugin(senza.knowledge.plugin([source]))
        except Exception:
            pass

    # ── Skills ──────────────────────────────────────────────────────────
    if config.skills_dir:
        try:
            skills = senza.load_skills(config.skills_dir)
            if skills:
                builder = builder.skills(skills)
        except Exception:
            pass

    # ── MCP ─────────────────────────────────────────────────────────────
    if config.mcp_config:
        try:
            builder = builder.mcp_config_file(config.mcp_config)
        except Exception:
            pass

    # ── Sub-agent spawn ─────────────────────────────────────────────────
    if config.spawn_enabled:
        try:
            builder = builder.enable_spawn(config.model, provider, config.sessions_dir)
        except Exception:
            pass

    # ── Wire state into tools module ────────────────────────────────────
    try:
        from senza_agent.tools.standard import set_state
        set_state(state)
    except Exception:
        pass

    # ── Build ───────────────────────────────────────────────────────────
    harness = builder.build()

    # ── Save system prompt + run metadata ───────────────────────────────
    persistence.save_meta({
        "model": config.model,
        "run_dir": run_dir,
        "working_dir": working_dir,
        "start_time": datetime.now().isoformat(),
    })
    persistence.save_scratchpad("")

    return harness
