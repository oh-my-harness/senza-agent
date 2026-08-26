"""QevosAgent-compatible state bridge for senza-agent.

Translates Senza SDK agent events into the QevosAgent dashboard state format,
and implements all API endpoints the dashboard frontend expects.

The dashboard frontend (panel.html) is a direct port of QevosAgent's 7744-line
SPA. It expects:
  - Root WebSocket ``/`` broadcasting ``{type: 'state', ...state}`` messages
  - REST endpoints for task control, runs, profiles, skills, crons, memory, env

This module provides a ``StateBridge`` class that wraps the existing
``TaskManager`` and ``RenderManager`` to produce the compatible state.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web, WSMsgType

# Sentinel: _convert_event modified _current_events in-place; broadcast but don't append.
_MODIFIED = object()


# ── Paths ──────────────────────────────────────────────────────────────────

_AGENT_DIR = Path(os.environ.get("SENZA_AGENT_DIR", Path.cwd()))
_RUNS_DIR = _AGENT_DIR / "runs"
_SKILLS_DIR = _AGENT_DIR / "skills"
_CRONS_DIR = _AGENT_DIR / "crons"
_APPS_DIR = _AGENT_DIR / "apps"
_MEMORY_CONCEPT = _AGENT_DIR / "memory_concept.md"
_MEMORY_EPISODIC = _AGENT_DIR / "memory_episodic.jsonl"


def _read_text(fp: Path) -> str:
    try:
        return fp.read_text(encoding="utf-8")
    except Exception:
        return ""


def _read_json(fp: Path) -> Any:
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None


def _safe_name(name: str) -> str:
    """Sanitize a file name: strip .md, keep alnum/_/-."""
    return name.replace(".md", "").replace("/", "_").replace("\\", "_")


def _list_runs() -> list[dict]:
    """List run directories sorted by mtime (newest first)."""
    if not _RUNS_DIR.is_dir():
        return []
    runs = []
    for entry in _RUNS_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        summary = _read_json(entry / "summary.json")
        if summary is None:
            summary = {}
        runs.append({
            "runId": entry.name,
            "summary": summary.get("summary", ""),
            "status": summary.get("status", "unknown"),
            "createdAt": st.st_ctime,
            "mtime": st.st_mtime,
        })
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return runs


def _load_run(run_id: str) -> Optional[dict]:
    """Load a run's full state from its directory."""
    run_dir = _RUNS_DIR / run_id
    if not run_dir.is_dir():
        return None
    events = []
    events_file = run_dir / "events.jsonl"
    if events_file.is_file():
        for line in _read_text(events_file).splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    summary = _read_json(run_dir / "summary.json") or {}
    scratchpad = _read_text(run_dir / "scratchpad.md")
    system_prompt = _read_text(run_dir / "system_prompt.md")
    meta = _read_json(run_dir / "meta.json") or {}
    return {
        "runId": run_id,
        "events": events,
        "summary": summary,
        "scratchpad": scratchpad,
        "systemPrompt": system_prompt,
        "meta": meta,
    }


def _list_run_files(run_id: str) -> list[dict]:
    """List all files in a run directory (recursive, relative paths)."""
    run_dir = _RUNS_DIR / run_id
    if not run_dir.is_dir():
        return []
    files = []
    for p in sorted(run_dir.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(run_dir))
            st = p.stat()
            files.append({
                "name": rel,
                "path": rel,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "type": "file",
            })
    return files


# ── State Bridge ───────────────────────────────────────────────────────────


class StateBridge:
    """Bridges Senza SDK events to QevosAgent dashboard state format.

    Maintains the ``state`` dict that gets broadcast over the root WebSocket.
    Task events from ``TaskManager`` are translated into ``state.events``.
    """

    def __init__(self, task_manager: Any, render_manager: Any) -> None:
        self.task = task_manager
        self.render = render_manager
        self._ws_clients: set[web.WebSocketResponse] = set()
        self.pending_rebuild: bool = False
        self._rebuild_callback: Any = None
        self._state: dict[str, Any] = {
            "runs": [],
            "runSummaries": {},
            "runTags": {},
            "activeRunId": None,
            "status": None,
            "scratchpad": "",
            "systemPrompt": "",
            "patchEvents": [],
            "events": [],
            "meta": {},
            "launching": False,
            "agentPid": None,
            "agentAlive": False,
            "webDisplays": {},
            "fileTabs": None,
            "networkInfo": None,
            "teamNodeId": None,
            "advisorLast": None,
            "advisorHistory": [],
            "graph": None,
            "instanceName": os.environ.get("INSTANCE_NAME", ""),
            "terminals": [],
            "browserAgent": None,
        }
        self._current_events: list[dict[str, Any]] = []
        self._current_text: list[str] = []
        self._turn_count: int = 0
        self._seen_tool_calls: set[str] = set()
        self._seen_tool_results: set[str] = set()
        self._streaming_idx: int | None = None  # idx of current streaming event

    @property
    def state(self) -> dict[str, Any]:
        return self._state

    def _refresh_runs(self) -> None:
        """Refresh runs list, summaries, and tags from disk.

        Frontend contract (panel.html):
          - state.runs         : string[] of run IDs
          - state.runSummaries : { [runId]: summaryText }
          - state.runTags      : { [runId]: string[] }
        """
        run_dicts = _list_runs()
        # If the runs dir is temporarily unavailable (network drive hiccup,
        # permissions, etc.), preserve the existing list rather than wiping
        # the sidebar.
        if not run_dicts and self._state.get("runs"):
            return
        self._state["runs"] = [r["runId"] for r in run_dicts]
        self._state["runSummaries"] = {
            r["runId"]: r.get("summary", "") for r in run_dicts
        }
        # Tags: read from summary.json if present (summary may contain a "tags" list).
        run_tags: dict[str, list[str]] = {}
        for r in run_dicts:
            run_dir = _RUNS_DIR / r["runId"]
            summary = _read_json(run_dir / "summary.json")
            if summary and isinstance(summary.get("tags"), list):
                run_tags[r["runId"]] = [str(t) for t in summary["tags"]]
            else:
                run_tags[r["runId"]] = []
        self._state["runTags"] = run_tags

    async def broadcast(self) -> None:
        """Broadcast current state to all connected WS clients."""
        msg = json.dumps({"type": "state", **self._state})
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    async def broadcast_console(self, stream: str, text: str) -> None:
        """Push a console line to all clients."""
        msg = json.dumps({
            "type": "console",
            "stream": stream,
            "text": text,
            "ts": int(time.time() * 1000),
        })
        dead = set()
        for ws in self._ws_clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead

    # ── WebSocket ────────────────────────────────────────────────────────

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Root WebSocket handler — broadcasts state to dashboard clients."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)

        # Send initial state
        self._refresh_runs()
        await ws.send_str(json.dumps({"type": "state", **self._state}))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if data.get("type") == "ping":
                        await ws.send_str(json.dumps({"type": "pong"}))
                    elif data.get("type") == "select_run":
                        # Client selected a historical run to view
                        run_id = data.get("runId")
                        if run_id:
                            run_data = _load_run(run_id)
                            if run_data:
                                await ws.send_str(json.dumps({
                                    "type": "state",
                                    "events": run_data["events"],
                                    "scratchpad": run_data["scratchpad"],
                                    "systemPrompt": run_data["systemPrompt"],
                                    "meta": run_data["meta"],
                                    "activeRunId": run_id,
                                    "viewRunId": run_id,
                                }))
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._ws_clients.discard(ws)

        return ws

    # ── Task event integration ───────────────────────────────────────────

    async def on_task_event(self, event: dict[str, Any]) -> None:
        """Called when a task event is received from the agent.

        Converts Senza SDK event types to QevosAgent dashboard format.
        """
        qev = self._convert_event(event)
        if qev is _MODIFIED:
            # Event modified _current_events in-place (streaming delta or turn_end
            # replacement). Broadcast the update without appending.
            self._state["status"] = {"status": "running"}
            self._state["events"] = self._current_events[-500:]
            await self.broadcast()
            return
        if qev is None:
            return  # skip events we don't render

        self._current_events.append(qev)
        ev_type = qev.get("type", "")

        if ev_type in ("streaming", "tool_call", "thought"):
            self._state["status"] = {"status": "running"}
        elif ev_type in ("done", "settled", "agent_end"):
            self._state["status"] = {"status": "settled"}
        elif ev_type == "aborted":
            self._state["status"] = {"status": "aborted"}
        elif ev_type == "error":
            self._state["status"] = {"status": "error"}

        self._state["events"] = self._current_events[-500:]
        await self.broadcast()

        # If the agent just called ask_user, the tool will block in the agent
        # thread waiting for the user to answer. Switch to "paused" state so
        # the dashboard shows the awaiting-input banner.
        if ev_type == "tool_call" and qev.get("tool") == "ask_user":
            question = ""
            args = qev.get("args", {})
            if isinstance(args, dict):
                question = str(args.get("question", ""))
            self._state["meta"]["awaiting_input"] = question
            self._state["status"] = {"status": "paused"}
            await self.broadcast()
        # If the tool_result for ask_user just arrived, the agent is unblocked
        # — clear the awaiting_input flag and return to running.
        elif ev_type == "tool_result" and qev.get("tool") == "ask_user":
            self._state["meta"].pop("awaiting_input", None)
            self._state["status"] = {"status": "running"}
            await self.broadcast()

    def _convert_event(self, ev: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Convert a Senza SDK event dict to QevosAgent dashboard event format.

        Uses SDK delta + high-level events:
        - thinking_delta/text_delta → accumulated into a "streaming" event (in-place)
        - turn_end             → replaces streaming event with final "thought"
        - tool_execution_start → tool_call (has tool_name + args)
        - tool_execution_end   → tool_result (has result.text + ok)
        - agent_end            → done (has new_messages with final answer)
        - error                → error
        """
        ev_type = ev.get("type", "")
        idx = len(self._current_events)

        # Skip low-level / non-rendered events
        if ev_type in (
            "turn_start", "message_start", "message_update", "message_end",
            "phase_change", "model_update", "thinking_level_update",
            "tools_update", "active_tools_update", "resources_update",
            "session_info_update", "queue_update", "savepoint",
            "branch_forked", "branch_switched", "branch_deleted", "branch_summarized",
            "compaction_start", "compaction_end",
            "agent_start", "retry_attempt",
            "tool_call_start", "tool_call_args_delta", "tool_call_end",
            "tool_execution_update",
        ):
            return None
        # thinking_delta / text_delta → accumulate into a single "streaming" event
        # that grows in-place. On turn_end, the streaming event is replaced by
        # a final "thought" event with the complete message_text.
        if ev_type in ("thinking_delta", "text_delta"):
            chunk = ev.get("thinking") or ev.get("text", "")
            if not chunk:
                return None
            if self._streaming_idx is None:
                # Create new streaming event
                sev = {"type": "streaming", "thinking": "", "text": "", "idx": idx}
                self._current_events.append(sev)
                self._streaming_idx = len(self._current_events) - 1
            sev = self._current_events[self._streaming_idx]
            if ev_type == "thinking_delta":
                sev["thinking"] += chunk
            else:
                sev["text"] += chunk
            return _MODIFIED  # modified in-place, broadcast but don't append

        # Deduplicate tool events: harness_tool_call_* and tool_execution_*
        # share the same tool_use_id. Prefer tool_execution_* (agent-level),
        # skip harness_tool_call_* to avoid duplicate render.
        tool_id = ev.get("tool_use_id", "")
        if ev_type == "harness_tool_call_start":
            if tool_id and tool_id in self._seen_tool_calls:
                return None
            self._seen_tool_calls.add(tool_id)
            return {
                "type": "tool_call",
                "tool": ev.get("tool_name", "unknown"),
                "args": ev.get("args", {}),
                "thought": "",
                "idx": idx,
                "iter": self._turn_count,
            }
        if ev_type == "harness_tool_call_end":
            if tool_id and tool_id in self._seen_tool_results:
                return None
            self._seen_tool_results.add(tool_id)
            result = ev.get("result", {})
            output = ""
            if isinstance(result, dict):
                output = result.get("details", result.get("text", ""))
            return {
                "type": "tool_result",
                "tool": ev.get("tool_name", ""),
                "output": str(output),
                "success": not result.get("is_error", False) if isinstance(result, dict) else True,
                "idx": idx,
            }

        # tool_execution_start → Qevos tool_call (skip if harness already emitted)
        if ev_type == "tool_execution_start":
            if tool_id and tool_id in self._seen_tool_calls:
                return None
            self._seen_tool_calls.add(tool_id)
            return {
                "type": "tool_call",
                "tool": ev.get("tool_name", "unknown"),
                "args": ev.get("args", {}),
                "thought": "",
                "idx": idx,
                "iter": self._turn_count,
            }

        # tool_execution_end → Qevos tool_result
        if ev_type == "tool_execution_end":
            if tool_id and tool_id in self._seen_tool_results:
                return None
            self._seen_tool_results.add(tool_id)
            result = ev.get("result", {})
            output = ""
            if isinstance(result, dict):
                output = result.get("text", result.get("details", ""))
            return {
                "type": "tool_result",
                "tool": ev.get("tool_name", ""),
                "output": str(output),
                "success": ev.get("ok", True),
                "idx": idx,
            }

        # turn_end → thought (replace streaming event with final text)
        if ev_type == "turn_end":
            self._turn_count += 1
            text = ev.get("message_text", "")
            if not text:
                # No text — clear streaming event if present
                if self._streaming_idx is not None:
                    self._current_events.pop(self._streaming_idx)
                    self._streaming_idx = None
                    return _MODIFIED
                return None
            # Replace the streaming event with a final thought
            if self._streaming_idx is not None:
                old_idx = self._current_events[self._streaming_idx]["idx"]
                thought_ev = {"type": "thought", "thought": text, "idx": old_idx, "iter": self._turn_count}
                self._current_events[self._streaming_idx] = thought_ev
                self._streaming_idx = None
                return _MODIFIED
            return {"type": "thought", "thought": text, "idx": idx, "iter": self._turn_count}

        # agent_end → done (final answer from last assistant message)
        if ev_type == "agent_end":
            answer = ""
            msgs = ev.get("new_messages", [])
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "assistant" and m.get("text"):
                    answer = m["text"]
                    break
            # Skip if the last thought already contains this text (turn_end emitted it)
            for existing in reversed(self._current_events):
                if existing.get("type") == "thought" and existing.get("thought"):
                    if existing["thought"] == answer or answer in existing["thought"]:
                        return None  # duplicate — turn_end already showed it
                    break
            return {"type": "done", "answer": answer, "idx": idx}

        # settled → done (if no agent_end seen)
        if ev_type == "settled":
            return {"type": "done", "answer": "", "idx": idx}

        # aborted/error
        if ev_type == "aborted":
            return {"type": "error", "text": "Task aborted", "idx": idx}
        if ev_type == "error":
            return {"type": "error", "text": ev.get("message", "Error"), "idx": idx}

        # Unknown — pass through
        return {**ev, "idx": idx}

    async def on_task_start(self, text: str) -> None:
        """Called when a new task starts."""
        self._current_events = []
        self._current_text = []
        self._turn_count = 0
        self._seen_tool_results = set()
        self._streaming_idx = None
        # Add goal event
        goal_ev = {"type": "goal", "text": text, "idx": 0}
        self._current_events.append(goal_ev)
        self._state["events"] = self._current_events
        self._state["status"] = {"status": "running"}
        self._state["agentAlive"] = True
        self._state["launching"] = False
        run_id = f"run_{int(time.time())}"
        self._state["activeRunId"] = run_id
        self._state["meta"] = {"_user_goal": text[:500], "goal": text[:500]}
        await self.broadcast()

    async def on_task_end(self, summary: dict) -> None:
        """Called when a task ends.

        Keeps agentAlive=True so the dashboard routes follow-up messages
        to /api/inject (which reuses the harness's conversation context)
        instead of /api/launch (which starts a fresh run).
        """
        self._state["agentAlive"] = True
        self._state["status"] = {"status": "idle"}
        # Save run to disk
        run_id = self._state.get("activeRunId", f"run_{int(time.time())}")
        if run_id:
            run_dir = _RUNS_DIR / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "events.jsonl").write_text(
                "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in self._current_events),
                encoding="utf-8",
            )
            (run_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        self._refresh_runs()
        # If config was saved while a task was running, rebuild harness now.
        if self.pending_rebuild:
            self.pending_rebuild = False
            if self._rebuild_callback:
                try:
                    self._rebuild_callback()
                except Exception:
                    pass
        await self.broadcast()

    async def on_followup(self, text: str) -> None:
        """Called for a follow-up message after a task has ended.

        Appends a goal event to the existing events list (does NOT clear
        history) and marks the agent as running again.
        """
        self._seen_tool_calls = set()
        self._seen_tool_results = set()
        goal_ev = {"type": "goal", "text": text, "idx": len(self._current_events)}
        self._streaming_idx = None
        self._current_events.append(goal_ev)
        self._state["events"] = self._current_events[-500:]
        self._state["status"] = {"status": "running"}
        self._state["agentAlive"] = True
        run_id = f"run_{int(time.time())}"
        self._state["activeRunId"] = run_id
        self._state["meta"] = {"_user_goal": text[:500], "goal": text[:500]}
        await self.broadcast()

# ── API Handlers ───────────────────────────────────────────────────────────


class QevosAPI:
    """QevosAgent-compatible REST API handlers for the dashboard."""

    def __init__(self, state_bridge: StateBridge, task_manager: Any, webserver: Any = None) -> None:
        self.sb = state_bridge
        self.task = task_manager
        self.ws = webserver

    def add_routes(self, app: web.Application) -> None:
        """Register all QevosAgent-compatible API routes."""
        # Task control
        app.router.add_post("/api/launch", self._api_launch)
        app.router.add_post("/api/kill", self._api_kill)
        app.router.add_post("/api/inject", self._api_inject)
        app.router.add_post("/api/inject-image", self._api_inject_image)
        app.router.add_get("/api/version", self._api_version)
        app.router.add_get("/api/state", self._api_state)
        # Run management
        app.router.add_get("/api/run/{run_id}", self._api_run_get)
        app.router.add_delete("/api/run/{run_id}", self._api_run_delete)
        app.router.add_get("/api/run/{run_id}/skills", self._api_run_skills)
        app.router.add_get("/api/run/{run_id}/advisor", self._api_run_advisor)
        app.router.add_get("/api/run/{run_id}/advisor/{idx}", self._api_run_advisor)
        app.router.add_get("/api/run-files/{run_id}", self._api_run_files)
        app.router.add_get("/api/run-file/{run_id}/{path:.*}", self._api_run_file_get)
        app.router.add_post("/api/run-file/{run_id}/{path:.*}", self._api_run_file_write)
        app.router.add_get("/api/run-file-raw/{run_id}/{path:.*}", self._api_run_file_raw)
        app.router.add_get("/api/download-file/{run_id}/{path:.*}", self._api_download_file)
        app.router.add_get("/api/job-output/{run_id}/{job_id}", self._api_job_output)
        app.router.add_post("/api/runs-index", self._api_runs_index)
        app.router.add_get("/api/run/{run_id}/graph", self._api_run_graph)
        # Profiles
        app.router.add_get("/api/profiles", self._api_profiles)
        app.router.add_get("/api/profile-file/{name}", self._api_profile_file_get)
        app.router.add_post("/api/profile-file/{name}", self._api_profile_file_post)
        app.router.add_delete("/api/profile-file/{name}", self._api_profile_file_delete)
        app.router.add_get("/api/agents-md", self._api_agents_md)
        app.router.add_post("/api/agents-md", self._api_agents_md_post)
        app.router.add_get("/api/advisor-md", self._api_advisor_md)
        app.router.add_post("/api/advisor-md", self._api_advisor_md_post)
        # Skills
        app.router.add_get("/api/skills", self._api_skills_list)
        app.router.add_get("/api/skill/{name}", self._api_skill_get)
        app.router.add_post("/api/skill/{name}", self._api_skill_post)
        app.router.add_delete("/api/skill/{name}", self._api_skill_delete)
        # Crons
        app.router.add_get("/api/crons", self._api_crons_list)
        app.router.add_get("/api/cron/{name}", self._api_cron_get)
        app.router.add_post("/api/cron/{name}", self._api_cron_post)
        app.router.add_delete("/api/cron/{name}", self._api_cron_delete)
        app.router.add_post("/api/cron/{name}/run", self._api_cron_run)
        app.router.add_get("/api/cron-history", self._api_cron_history)
        app.router.add_get("/api/followup-history", self._api_followup_history)
        # Memory
        app.router.add_get("/api/memory-concept", self._api_memory_concept_get)
        app.router.add_post("/api/memory-concept", self._api_memory_concept_post)
        app.router.add_get("/api/memory-episodic", self._api_memory_episodic_get)
        app.router.add_post("/api/memory-episodic", self._api_memory_episodic_post)
        # Env
        app.router.add_get("/api/env", self._api_env_get)
        app.router.add_post("/api/env", self._api_env_post)
        app.router.add_post("/api/env/test", self._api_env_test)
        # File tabs
        app.router.add_post("/api/file-tab", self._api_file_tab)
        # App project
        app.router.add_get("/api/app-project", self._api_app_project)

    # ── Task control ─────────────────────────────────────────────────────

    async def _api_launch(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        goal = (body.get("goal") or "").strip()
        if not goal:
            return web.json_response({"error": "goal is required"}, status=400)
        await self.sb.on_task_start(goal)
        result = await self.task.start_task(goal)
        return web.json_response(result)

    async def _api_kill(self, request: web.Request) -> web.Response:
        result = await self.task.abort_task()
        self.sb._state["agentAlive"] = False
        await self.sb.broadcast()
        from senza_agent.webserver.ask_user_bridge import get_bridge
        get_bridge().reset()
        return web.json_response(result)

    async def _api_inject(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        command = (body.get("command") or "").strip()
        if not command:
            return web.json_response({"error": "command required"}, status=400)
        # If the agent is paused waiting for an ask_user answer, route the
        # user's text to the AskUserBridge instead of starting a new task.
        from senza_agent.webserver.ask_user_bridge import get_bridge
        bridge = get_bridge()
        if bridge.is_active:
            accepted = bridge.provide_answer(command)
            if accepted:
                return web.json_response({"ok": True, "ask_user_answer": True})
            # Bridge became inactive between the check and the call — fall through
        # Strip /inject prefix — dashboard auto-prepends it for non-slash input
        if command.startswith("/inject "):
            command = command[len("/inject "):].strip()
        # Follow-up message: append goal event without clearing history.
        # The harness retains conversation context across prompt_async calls.
        await self.sb.on_followup(command)
        result = await self.task.start_task(command)
        return web.json_response(result)

    async def _api_inject_image(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": False, "error": "image injection not supported"})

    async def _api_version(self, request: web.Request) -> web.Response:
        try:
            from senza_agent import __version__
            ver = __version__
        except Exception:
            ver = "1.0.0"
        return web.json_response({
            "version": ver,
            "busy": self.task.is_running,
            "agentAlive": self.sb._state.get("agentAlive", False),
        })

    async def _api_state(self, request: web.Request) -> web.Response:
        self.sb._refresh_runs()
        return web.json_response({"type": "state", **self.sb.state})

    # ── Run management ───────────────────────────────────────────────────

    async def _api_run_get(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        data = _load_run(run_id)
        if data is None:
            return web.json_response({}, status=404)
        return web.json_response(data)

    async def _api_run_delete(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        run_dir = _RUNS_DIR / run_id
        if run_dir.is_dir():
            import shutil
            shutil.rmtree(run_dir, ignore_errors=True)
            self.sb._refresh_runs()
            await self.sb.broadcast()
            return web.json_response({"ok": True})
        return web.json_response({"error": "not found"}, status=404)

    async def _api_run_skills(self, request: web.Request) -> web.Response:
        return web.json_response({"skills": []})

    async def _api_run_advisor(self, request: web.Request) -> web.Response:
        return web.json_response({"entries": []})

    async def _api_run_files(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        return web.json_response({"files": _list_run_files(run_id)})

    async def _api_run_file_get(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rel_path = request.match_info["path"]
        fp = _RUNS_DIR / run_id / rel_path
        if not fp.is_file():
            return web.json_response({"error": "not found"}, status=404)
        try:
            content = fp.read_text(encoding="utf-8")
            return web.json_response({"content": content, "binary": False})
        except UnicodeDecodeError:
            return web.json_response({"content": "", "binary": True})

    async def _api_run_file_write(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rel_path = request.match_info["path"]
        fp = _RUNS_DIR / run_id / rel_path
        fp.parent.mkdir(parents=True, exist_ok=True)
        try:
            body = await request.json()
            fp.write_text(body.get("content", ""), encoding="utf-8")
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_run_file_raw(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rel_path = request.match_info["path"]
        fp = _RUNS_DIR / run_id / rel_path
        if not fp.is_file():
            return web.Response(text="not found", status=404)
        from .files import guess_mime
        return web.FileResponse(fp, headers={"Content-Type": guess_mime(str(fp))})

    async def _api_download_file(self, request: web.Request) -> web.Response:
        run_id = request.match_info["run_id"]
        rel_path = request.match_info["path"]
        fp = _RUNS_DIR / run_id / rel_path
        if not fp.is_file():
            return web.Response(text="not found", status=404)
        from .files import guess_mime
        return web.FileResponse(
            fp,
            headers={
                "Content-Type": guess_mime(str(fp)),
                "Content-Disposition": f'attachment; filename="{fp.name}"',
            },
        )

    async def _api_job_output(self, request: web.Request) -> web.Response:
        return web.json_response({"output": "", "error": "not found"})

    async def _api_runs_index(self, request: web.Request) -> web.Response:
        runs = _list_runs()
        index = []
        for r in runs:
            data = _load_run(r["runId"]) or {}
            events = data.get("events", [])
            # Extract human-readable text from events
            texts = []
            for ev in events:
                if isinstance(ev, dict):
                    if ev.get("type") == "text_delta":
                        texts.append(ev.get("text", ""))
                    elif ev.get("type") == "tool_call":
                        texts.append(ev.get("tool", ""))
            index.append({
                "runId": r["runId"],
                "summary": r["summary"],
                "text": " ".join(texts)[:500],
            })
        return web.json_response({"index": index})

    async def _api_run_graph(self, request: web.Request) -> web.Response:
        return web.json_response({"graph": None})

    # ── Profiles ─────────────────────────────────────────────────────────

    async def _api_profiles(self, request: web.Request) -> web.Response:
        agents_files = []
        advisor_files = []
        if _AGENT_DIR.is_dir():
            for f in sorted(_AGENT_DIR.iterdir()):
                if not f.is_file() or not f.name.endswith(".md"):
                    continue
                if f.name.startswith("AGENTS"):
                    agents_files.append(f.name)
                elif f.name.startswith("ADVISOR"):
                    advisor_files.append(f.name)
        return web.json_response({
            "agents": agents_files,
            "advisor": advisor_files,
        })

    async def _api_profile_file_get(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        fp = _AGENT_DIR / name
        if not fp.is_file():
            return web.json_response({"content": ""})
        return web.json_response({"content": _read_text(fp)})

    async def _api_profile_file_post(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        fp = _AGENT_DIR / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body.get("content", ""), encoding="utf-8")
        return web.json_response({"ok": True})

    async def _api_profile_file_delete(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        fp = _AGENT_DIR / name
        if fp.is_file():
            fp.unlink()
            return web.json_response({"ok": True})
        return web.json_response({"error": "not found"}, status=404)

    async def _api_agents_md(self, request: web.Request) -> web.Response:
        return web.json_response({"content": _read_text(_AGENT_DIR / "AGENTS.md")})

    async def _api_agents_md_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        (_AGENT_DIR / "AGENTS.md").write_text(body.get("content", ""), encoding="utf-8")
        return web.json_response({"ok": True})

    async def _api_advisor_md(self, request: web.Request) -> web.Response:
        return web.json_response({"content": _read_text(_AGENT_DIR / "ADVISOR.md")})

    async def _api_advisor_md_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        (_AGENT_DIR / "ADVISOR.md").write_text(body.get("content", ""), encoding="utf-8")
        return web.json_response({"ok": True})

    # ── Skills ───────────────────────────────────────────────────────────

    async def _api_skills_list(self, request: web.Request) -> web.Response:
        skills = []
        if _SKILLS_DIR.is_dir():
            for f in sorted(_SKILLS_DIR.iterdir()):
                if f.is_file() and f.suffix == ".md":
                    content = _read_text(f)
                    first_line = ""
                    for line in content.splitlines():
                        line = line.strip()
                        if line and not line.startswith("<!--"):
                            first_line = line.lstrip("# ").strip()
                            break
                    skills.append({"name": f.stem, "description": first_line})
        return web.json_response({"skills": skills})

    async def _api_skill_get(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        fp = _SKILLS_DIR / f"{name}.md"
        if not fp.is_file():
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"content": _read_text(fp)})

    async def _api_skill_post(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        _SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        (_SKILLS_DIR / f"{name}.md").write_text(body.get("content", ""), encoding="utf-8")
        return web.json_response({"ok": True})

    async def _api_skill_delete(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        fp = _SKILLS_DIR / f"{name}.md"
        if fp.is_file():
            fp.unlink()
            return web.json_response({"ok": True})
        return web.json_response({"error": "not found"}, status=404)

    # ── Crons ────────────────────────────────────────────────────────────

    async def _api_crons_list(self, request: web.Request) -> web.Response:
        crons = []
        if _CRONS_DIR.is_dir():
            for f in sorted(_CRONS_DIR.iterdir()):
                if f.is_file() and f.suffix == ".md":
                    content = _read_text(f)
                    crons.append({"id": f.stem, "content": content})
        return web.json_response({"crons": crons, "pending": [], "cronAvailable": False})

    async def _api_cron_get(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        fp = _CRONS_DIR / f"{name}.md"
        if not fp.is_file():
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"content": _read_text(fp)})

    async def _api_cron_post(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        _CRONS_DIR.mkdir(parents=True, exist_ok=True)
        (_CRONS_DIR / f"{name}.md").write_text(body.get("content", ""), encoding="utf-8")
        return web.json_response({"ok": True})

    async def _api_cron_delete(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        fp = _CRONS_DIR / f"{name}.md"
        if fp.is_file():
            fp.unlink()
            return web.json_response({"ok": True})
        return web.json_response({"error": "not found"}, status=404)

    async def _api_cron_run(self, request: web.Request) -> web.Response:
        name = _safe_name(request.match_info["name"])
        fp = _CRONS_DIR / f"{name}.md"
        if not fp.is_file():
            return web.json_response({"error": "not found"}, status=404)
        content = _read_text(fp)
        # Extract goal from cron content (first non-comment line)
        goal = ""
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("<!--"):
                goal = line
                break
        if goal:
            await self.sb.on_task_start(goal)
            await self.task.start_task(goal)
            return web.json_response({"ok": True, "queued": False})
        return web.json_response({"error": "no goal found in cron"})

    async def _api_cron_history(self, request: web.Request) -> web.Response:
        return web.json_response({"history": []})

    async def _api_followup_history(self, request: web.Request) -> web.Response:
        return web.json_response({"enabled": False, "maxGen": 0, "history": []})

    # ── Memory ───────────────────────────────────────────────────────────

    async def _api_memory_concept_get(self, request: web.Request) -> web.Response:
        if not _MEMORY_CONCEPT.is_file():
            return web.json_response({"content": None, "exists": False})
        return web.json_response({"content": _read_text(_MEMORY_CONCEPT), "exists": True})

    async def _api_memory_concept_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        content = body.get("content", "")
        _MEMORY_CONCEPT.parent.mkdir(parents=True, exist_ok=True)
        _MEMORY_CONCEPT.write_text(content, encoding="utf-8")
        return web.json_response({"ok": True})

    async def _api_memory_episodic_get(self, request: web.Request) -> web.Response:
        if not _MEMORY_EPISODIC.is_file():
            return web.json_response({"content": None, "exists": False})
        content = _read_text(_MEMORY_EPISODIC)
        lines = [l for l in content.splitlines() if l.strip()]
        return web.json_response({"content": content, "exists": True, "count": len(lines)})

    async def _api_memory_episodic_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        content = body.get("content", "")
        _MEMORY_EPISODIC.parent.mkdir(parents=True, exist_ok=True)
        _MEMORY_EPISODIC.write_text(content, encoding="utf-8")
        return web.json_response({"ok": True})

    # ── Env ──────────────────────────────────────────────────────────────
    async def _api_env_get(self, request: web.Request) -> web.Response:
        """Return current LLM config (from env, which is synced with settings.json).

        Returns the full env state including all API slots. API keys are
        returned unmasked — this is a local dashboard, not a remote API.
        """
        # Collect all relevant env vars for the frontend.
        keys = [
            "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_TEMPERATURE",
            "BACKUP_OPENAI_BASE_URL", "BACKUP_OPENAI_API_KEY", "BACKUP_OPENAI_MODEL", "BACKUP_OPENAI_TEMPERATURE",
            "BACKUP2_OPENAI_BASE_URL", "BACKUP2_OPENAI_API_KEY", "BACKUP2_OPENAI_MODEL", "BACKUP2_OPENAI_TEMPERATURE",
            "ADVISOR1_OPENAI_BASE_URL", "ADVISOR1_OPENAI_API_KEY", "ADVISOR1_OPENAI_MODEL", "ADVISOR1_OPENAI_TEMPERATURE",
            "ADVISOR2_OPENAI_BASE_URL", "ADVISOR2_OPENAI_API_KEY", "ADVISOR2_OPENAI_MODEL", "ADVISOR2_OPENAI_TEMPERATURE",
            "PREFERRED_API", "MAX_ITERS", "INSTANCE_NAME",
            "HTTPS_PROXY", "HTTP_PROXY",
            "DASHBOARD_HOST", "DASHBOARD_PORT", "DASHBOARD_ALLOW", "DASHBOARD_DENY",
            "MAX_TOOL_FEEDBACK_CHARS", "LLM_MAX_TOKENS",
        ]
        result = {}
        for k in keys:
            v = os.environ.get(k, "")
            # Also check OPENAI_API_BASE as fallback for OPENAI_BASE_URL
            if k == "OPENAI_BASE_URL" and not v:
                v = os.environ.get("OPENAI_API_BASE", "")
            result[k] = v
        result["configured"] = bool(result.get("OPENAI_API_KEY") and result.get("OPENAI_BASE_URL"))
        # Also return behavior settings (nested in settings.json)
        try:
            from senza_agent.config import load_config
            cfg = load_config()
            result["THINKING_LEVEL"] = cfg.behavior.thinking_level or ""
        except Exception:
            result["THINKING_LEVEL"] = ""
        return web.json_response(result)

    async def _api_env_post(self, request: web.Request) -> web.Response:
        """Save settings to ``~/.senza-agent/settings.json``, update env, and
        hot-reload the agent harness if idle.

        If a task is running, the settings are still persisted and env is
        updated, but the harness rebuild is deferred — the next idle moment
        (task end) will pick up the new config via ``_maybe_rebuild_after_task``.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)

        from senza_agent.config import save_settings
        save_settings(body)

        # Hot-reload: rebuild harness if idle; otherwise defer to task end.
        rebuilt = False
        if self.task.is_running:
            self.sb.pending_rebuild = True
        elif self.ws is not None:
            rebuilt = self.ws.rebuild_harness()

        return web.json_response({"ok": True, "rebuilt": rebuilt})

    async def _api_env_test(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        base_url = body.get("baseUrl", "")
        api_key = body.get("apiKey", "")
        if not base_url:
            return web.json_response({"ok": False, "error": "baseUrl required"})
        import aiohttp
        url = base_url.rstrip("/") + "/models"
        headers = {}
        if api_key and api_key != "local":
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return web.json_response({"ok": True, "status": resp.status})
                    return web.json_response({"ok": False, "error": f"HTTP {resp.status}"})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)})

    # ── File tabs ────────────────────────────────────────────────────────

    async def _api_file_tab(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        action = body.get("action")
        tab_path = body.get("path", "")
        label = body.get("label", "")
        run_id = body.get("runId", "")
        # Manage file tabs in state
        ft = self.sb._state.get("fileTabs") or {"tabs": [], "active": ""}
        if action == "open":
            # Add tab if not exists
            existing = [t for t in ft["tabs"] if t.get("path") == tab_path]
            if not existing:
                ft["tabs"].append({"path": tab_path, "label": label or tab_path})
            ft["active"] = tab_path
        elif action == "close":
            ft["tabs"] = [t for t in ft["tabs"] if t.get("path") != tab_path]
            if ft["active"] == tab_path:
                ft["active"] = ft["tabs"][-1]["path"] if ft["tabs"] else ""
        self.sb._state["fileTabs"] = ft
        await self.sb.broadcast()
        return web.json_response({"ok": True, "tabs": ft["tabs"]})

    # ── App project ──────────────────────────────────────────────────────

    async def _api_app_project(self, request: web.Request) -> web.Response:
        root = request.query.get("root", "")
        if not root or not Path(root).is_absolute():
            return web.json_response({"error": "absolute root required"}, status=400)
        abs_path = Path(root).resolve()
        # Look for senza.project.json or qevos.project.json
        for marker in ["senza.project.json", "qevos.project.json"]:
            p = abs_path / marker
            if p.is_file():
                data = _read_json(p)
                if data and data.get("app"):
                    return web.json_response({"app": data["app"], "root": str(abs_path), "marker": marker})
        return web.json_response({"error": "no project marker found", "root": str(abs_path)})
