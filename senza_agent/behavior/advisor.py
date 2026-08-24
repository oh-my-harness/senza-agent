"""Advisor module — high-level strategic guidance via an independent LLM context.

Ported from QevosAgent's ``agent/core/advisor.py``, adapted to the Senza SDK:
instead of an ``LLMBackend``, the advisor builds its own one-shot ``senza``
harness (``senza.providers.openai()`` + ``HarnessBuilder``) so the main agent's
conversation history is never carried over — the advisor sees only the curated
context built here.

Triggers:
  1. Periodic — every ``config.advisor_interval`` turns.
  2. Request  — ``state.advisor_requested`` flag (set by a tool / user).
  3. Stall    — simplified: last advice unchanged since last call.

Design principles (preserved from QevosAgent):
  - Independent context: no main-agent conversation history leak.
  - Noise filtered: only the curated context sections reach the advisor.
  - Non-fatal: any failure is silently swallowed; the main loop is unaffected.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from senza_agent.i18n import t

if TYPE_CHECKING:
    from senza_agent.behavior.state import AgentState
    from senza_agent.config import BehaviorConfig


# Meta key under which the last advisor user-context is mirrored for dashboards.
ADVISOR_LAST_CONTEXT_META_KEY = "_advisor_last_context"

# Marker prefixes for user-injected messages scanned from session entries.
_INJECTION_PREFIXES = ("[用户干预注入]", "[User Injection]", "[Web看板]")

# Default advisor system prompt; overridable via config / meta.
_DEFAULT_ADVISOR_SYSTEM = (
    "You are a senior strategic advisor observing an autonomous agent.\n"
    "Review the agent's current state and give concise, actionable guidance:\n"
    "- Flag stalled or off-track progress.\n"
    "- Prioritise unmet user follow-up instructions.\n"
    "- Suggest concrete next steps using only the listed tools.\n"
    "Be direct and brief. Do not flatter. If the agent is doing well, say so plainly."
)


# ── Context construction ────────────────────────────────────────────────────


def _extract_user_injections(state: "AgentState") -> list[dict]:
    """Collect user-injected instructions issued mid-run.

    Primary source: ``state.meta["_user_injections"]`` (explicit list written by
    inject/interrupt handlers). Falls back to scanning session entries from
    ``harness.read_active_path()`` for messages prefixed with the injection
    markers. Each item: ``{iter, ts, content, source}``.
    """
    items: list[dict] = []

    explicit = state.meta.get("_user_injections")
    if isinstance(explicit, list) and explicit:
        for it in explicit:
            if isinstance(it, dict) and (it.get("content") or "").strip():
                items.append({
                    "iter":    int(it.get("iter") or 0),
                    "ts":      it.get("ts") or "",
                    "content": str(it.get("content") or "").strip(),
                    "source":  it.get("source") or "explicit",
                })
        return items

    # Fallback: scan session entries the harness recorded.
    session_entries = state.meta.get("_session_entries")
    if not isinstance(session_entries, list):
        return items

    for idx, m in enumerate(session_entries):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            continue
        if not any(content.startswith(p) for p in _INJECTION_PREFIXES):
            continue
        body = content.split("\n", 1)[1].strip() if "\n" in content else content
        items.append({
            "iter":    idx,
            "ts":      "",
            "content": body,
            "source":  "scanned",
        })
    return items


def _build_tools_catalog(state: "AgentState") -> str:
    """Build a one-line-per-tool catalog (name + first-line description).

    In Senza, ``state.tools`` is a list of tool names (strings) rather than the
    QevosAgent ``Dict[str, ToolSpec]``. Both shapes are accepted: a name yields
    a bare bullet; a ``ToolSpec``-like object with a ``description`` yields the
    first line of that description.
    """
    lines: list[str] = []
    tools = getattr(state, "tools", []) or []
    if isinstance(tools, dict):
        # Legacy QevosAgent shape: dict of name -> ToolSpec.
        for name in sorted(tools.keys()):
            spec = tools.get(name)
            desc = (getattr(spec, "description", "") or "").strip()
            first_line = desc.split("\n", 1)[0].strip()
            if len(first_line) > 140:
                first_line = first_line[:140] + "…"
            lines.append(f"- {name} — {first_line}" if first_line else f"- {name}")
    else:
        for name in sorted(str(t) for t in tools):
            lines.append(f"- {name}")

    # Active skills catalog (optional, written by the skills loader).
    catalog = state.meta.get("_skills_catalog")
    if isinstance(catalog, str) and catalog.strip():
        lines.append("")
        lines.append(t("advisor.ctx.skills_header"))
        lines.append(catalog.strip())
    else:
        skills = state.meta.get("_active_skills")
        if isinstance(skills, list) and skills:
            lines.append("")
            lines.append(f"(已激活 SKILL：{', '.join(skills)})")
    return "\n".join(lines)


def _build_advisor_context(state: "AgentState", config: "BehaviorConfig") -> str:
    """Build the independent advisor input context (sectioned).

    Sections:
      ## Current Iteration
      ## Original Task Goal
      ## User Follow-up Instructions   (always its own section, never truncated)
      ## Scratchpad
      ## Work Progress Log             (filled by ensure_progress_log if present)
      ## Available Tools & Capabilities
      ## Recent Raw Execution Fragments (last 8, for cross-checking)
    """
    raw_goal = (state.meta.get("_task_desc") or getattr(state, "goal", "") or "").strip()
    scratchpad = (state.meta.get("scratchpad") or "").strip()
    iteration = getattr(state, "turn_count", 0)

    parts: list[str] = []
    parts.append(t("advisor.ctx.iter", iter=iteration))
    parts.append(t("advisor.ctx.goal", goal=raw_goal[:800]))

    # ── User follow-up instructions ──────────────────────────────────────
    injections = _extract_user_injections(state)
    if injections:
        kept = injections[-20:]
        lines: list[str] = []
        for it in kept:
            body = it["content"]
            if len(body) > 2000:
                body = body[:2000] + t("advisor.ctx.truncated")
            tag = f"[iter={it['iter']}]" if it.get("iter") else "[iter=?]"
            lines.append(f"- {tag} {body}")
        parts.append(t("advisor.ctx.user_inj", items="\n".join(lines)))
    else:
        parts.append(t("advisor.ctx.user_inj_empty"))

    # ── Scratchpad ───────────────────────────────────────────────────────
    if scratchpad:
        parts.append(t("advisor.ctx.sp", sp=scratchpad[:1500]))
    else:
        parts.append(t("advisor.ctx.sp_empty"))

    # ── Work progress log (if present) ───────────────────────────────────
    progress = state.meta.get("_progress_log")
    if isinstance(progress, str) and progress.strip():
        method = state.meta.get("_progress_log_method") or "unknown"
        log_iter = state.meta.get("_progress_log_iter") or 0
        parts.append(t("advisor.ctx.progress",
                       method=method, iter=log_iter, log=progress.strip()[:4000]))

    # ── Tools & capabilities ─────────────────────────────────────────────
    tools_text = _build_tools_catalog(state)
    if tools_text:
        parts.append(t("advisor.ctx.tools", items=tools_text))
    else:
        parts.append(t("advisor.ctx.tools_empty"))

    # ── Recent raw execution fragments (reconciliation only) ─────────────
    session_entries = state.meta.get("_session_entries") or []
    recent_msgs: list[str] = []
    for m in session_entries:
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if len(content) > 500:
            content = content[:500] + t("advisor.ctx.truncated")
        recent_msgs.append(f"[{role}] {content}")
    recent_msgs = recent_msgs[-8:]
    history_text = "\n---\n".join(recent_msgs) if recent_msgs else t("advisor.ctx.no_history")
    parts.append(t("advisor.ctx.history", n=len(recent_msgs), hist=history_text))

    return "\n\n".join(parts)


# ── Independent log ──────────────────────────────────────────────────────────


def _advisor_log_path(state: "AgentState") -> Optional[Path]:
    """Resolve the advisor log path from state.run_dir (or meta override)."""
    override = state.meta.get("_advisor_log_path")
    if override:
        return Path(override)
    run_dir = getattr(state, "run_dir", "") or ""
    if not run_dir:
        return None
    return Path(run_dir) / "advisor_log.jsonl"


def _log_advisor_call(
    state: "AgentState",
    trigger_reason: str,
    context: str,
    advice: Optional[str],
    status: str,  # "ok" | "empty" | "failed"
    system: str = "",
) -> None:
    """Append a full advisor call record to advisor_log.jsonl.

    Also writes a last-call snapshot to advisor_last.json for dashboards.
    Failures are silently ignored.
    """
    log_path = _advisor_log_path(state)
    if log_path is None:
        return
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    iteration = getattr(state, "turn_count", 0)
    entry = {
        "ts":        ts,
        "iteration": iteration,
        "trigger":   trigger_reason,
        "status":    status,
        "system":    system,
        "context":   context,
        "advice":    advice,
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    try:
        snap_path = log_path.parent / "advisor_last.json"
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ── Main call ────────────────────────────────────────────────────────────────


def _resolve_advisor_system(state: "AgentState", config: "BehaviorConfig") -> str:
    """Pick the advisor system prompt: meta override > config > default."""
    sys_prompt = state.meta.get("_advisor_system") or ""
    if not sys_prompt and getattr(config, "advisor_model", None):
        # Config has no dedicated system-prompt field; keep the default.
        sys_prompt = ""
    return (sys_prompt or _DEFAULT_ADVISOR_SYSTEM).strip()


def run_advisor(state: "AgentState", config: "BehaviorConfig") -> None:
    """Call the LLM in an independent context and store the advice.

    Builds a throw-away Senza harness (provider + builder) so the main agent's
    session is never touched. On any failure, silently returns without raising.
    Result is written to ``state.last_advice`` and appended to
    ``<run_dir>/advisor_log.jsonl``.
    """
    advisor_system = _resolve_advisor_system(state, config)

    context: Optional[str] = None
    try:
        import senza  # lazy: keeps import-time cost zero until first trigger

        context = _build_advisor_context(state, config)
        if not context or not context.strip():
            return

        user_msg = t("advisor.trigger_msg", reason="periodic", context=context)

        api_key = state.meta.get("_advisor_api_key") or os.environ.get("SENZA_AGENT_API_KEY", "")
        api_base = state.meta.get("_advisor_api_base") or os.environ.get("SENZA_AGENT_API_BASE", "")
        model = state.meta.get("_advisor_model") or getattr(config, "advisor_model", None) or "gpt-4o"

        if not api_key:
            # No credentials — cannot make an independent call. Log and bail.
            _log_advisor_call(state, "periodic", context, None, "failed", system=advisor_system)
            return

        provider = senza.providers.openai(api_key=api_key, base_url=api_base or None)
        harness = (
            senza.HarnessBuilder(model)
            .provider("*", provider)
            .system_prompt(advisor_system)
            .build()
        )

        max_tokens = int(os.environ.get("ADVISOR_MAX_TOKENS", "800"))
        harness.set_max_tokens(max_tokens)

        events = harness.prompt_and_collect(user_msg)
        advice = "".join(e.get("text", "") for e in events if e.get("type") == "text_delta").strip()

        advice = advice if advice else None
        if advice:
            state.last_advice = advice
            state.meta["_advisor_last_iter"] = getattr(state, "turn_count", 0)
        state.meta[ADVISOR_LAST_CONTEXT_META_KEY] = context

        _log_advisor_call(
            state, "periodic", context, advice,
            "ok" if advice else "empty", system=advisor_system,
        )

    except Exception:
        _log_advisor_call(
            state, "periodic", context or "(context build failed)", None, "failed",
            system=advisor_system,
        )


# ── Progress log (simplified self-summary) ───────────────────────────────────


def ensure_progress_log(state: "AgentState") -> str:
    """Return a self-summary of progress so far (no extra LLM call).

    Simplified port: QevosAgent fired a second LLM call here to reuse the main
    agent's KV cache. Senza has no such shared cache, so we synthesise a short
    summary from state fields instead — enough for the advisor to see the macro
    picture without a round-trip.
    """
    cached = state.meta.get("_progress_log")
    if isinstance(cached, str) and cached.strip():
        return cached

    iteration = getattr(state, "turn_count", 0)
    goal = (getattr(state, "goal", "") or "").strip()
    last_advice = (getattr(state, "last_advice", "") or "").strip()
    scratchpad = (state.meta.get("scratchpad") or "").strip()

    parts: list[str] = []
    parts.append(f"Turn: {iteration}")
    if goal:
        parts.append(f"Goal: {goal[:300]}")
    if scratchpad:
        parts.append(f"Scratchpad: {scratchpad[:500]}")
    if last_advice:
        parts.append(f"Last advice: {last_advice[:300]}")
    summary = "\n".join(parts)

    state.meta["_progress_log"] = summary
    state.meta["_progress_log_iter"] = iteration
    state.meta["_progress_log_method"] = "state_summary"
    return summary


# ── Trigger decision ─────────────────────────────────────────────────────────


def should_trigger_advisor(state: "AgentState", config: "BehaviorConfig") -> bool:
    """Decide whether the advisor should fire this turn.

    Triggers:
      1. Request — ``state.advisor_requested`` is set (highest priority).
      2. Periodic — ``turn_count % advisor_interval == 0`` (skip turn 0).
      3. Stall — ``last_advice`` hasn't changed since the last call AND we have
         a non-zero turn count (simplified heuristic).
    """
    # 1. Agent / user requested.
    if getattr(state, "advisor_requested", False):
        return True

    interval = getattr(config, "advisor_interval", 15) or 15
    turn = getattr(state, "turn_count", 0)

    # 2. Periodic (turn 0 skipped — nothing has happened yet).
    if turn > 0 and interval > 0 and turn % interval == 0:
        return True

    # 3. Stall: last advice unchanged since last advisor iteration. We treat
    #    "no advice yet but turns are accumulating past the interval" as a stall
    #    signal only when last_advice is empty and we've crossed one interval.
    last_advised = int(state.meta.get("_advisor_last_iter", 0))
    if not getattr(state, "last_advice", "").strip() and turn - last_advised >= interval:
        return True

    return False


# ── Injection ────────────────────────────────────────────────────────────────


def inject_advisor_advice(state: "AgentState", harness: object) -> None:
    """Inject the latest advice into the main agent via ``harness.steer()``.

    ``harness`` is a Senza ``AgentHarness``. Failures are silently swallowed.
    """
    advice = (getattr(state, "last_advice", "") or "").strip()
    if not advice:
        return
    try:
        harness.steer(advice)  # type: ignore[attr-defined]
    except Exception:
        pass


# ── after_turn hook factory ──────────────────────────────────────────────────


def advisor_after_turn(state: "AgentState", config: "BehaviorConfig") -> Callable[[dict], None]:
    """Return a ``senza.hooks.after_turn`` callback wired to this advisor.

    The callback:
      1. Increments ``state.turn_count``.
      2. Refreshes the progress log (cheap, in-memory).
      3. Checks ``should_trigger_advisor``; if true, runs the advisor and
         injects its advice into the main harness (when available via
         ``ctx["harness"]``).
    """
    def _callback(ctx: dict) -> None:
        try:
            state.turn_count = getattr(state, "turn_count", 0) + 1
            ensure_progress_log(state)
            if should_trigger_advisor(state, config):
                run_advisor(state, config)
                # Consume a one-shot request after firing.
                state.advisor_requested = False
                harness = ctx.get("harness") if isinstance(ctx, dict) else None
                if harness is not None:
                    inject_advisor_advice(state, harness)
        except Exception:
            # The after_turn hook must never break the main loop.
            pass

    return _callback


# Alias for the name expected by BehaviorBundle (which imports advisor_runner).
advisor_runner = advisor_after_turn
