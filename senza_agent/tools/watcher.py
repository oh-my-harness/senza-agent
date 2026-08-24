"""Environment watcher manager.

Ported from QevosAgent's ``agent/core/watcher.py``.

A registry of user-defined, periodically-triggered code snippets that collect
and process an environment slice; the framework injects their output into the
LLM context on each iteration.

Key adaptation: the original bound its lifecycle to
``state.meta["_watcher_manager"]``; here we use a module-level singleton so the
manager survives across tool calls without an ``AgentState``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── Constants ───────────────────────────────────────────────────────────────

INJECT_HARD_CAP = 500
SPILL_PREFIX = "watch"


# ── Registry entry ──────────────────────────────────────────────────────────


@dataclass
class WatcherEntry:
    name: str
    path: str
    interval: int = 10
    emit: str = "event"
    enabled: bool = True
    params: dict = field(default_factory=dict)
    desc: str = ""

    store: dict = field(default_factory=dict)
    last_run_time: float = 0.0
    last_run_iter: int = -1
    last_result: Any = None
    error_streak: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name, "path": self.path, "interval": self.interval,
            "emit": self.emit, "enabled": self.enabled, "params": self.params,
            "desc": self.desc, "store": self.store,
            "last_run_time": self.last_run_time, "last_run_iter": self.last_run_iter,
            "last_result": self.last_result if _json_safe(self.last_result) else None,
            "error_streak": self.error_streak,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WatcherEntry":
        return cls(
            name=str(d.get("name", "")),
            path=str(d.get("path", "")),
            interval=int(d.get("interval", 10) or 10),
            emit=str(d.get("emit", "event") or "event"),
            enabled=bool(d.get("enabled", True)),
            params=dict(d.get("params", {}) or {}),
            desc=str(d.get("desc", "") or ""),
            store=dict(d.get("store", {}) or {}),
            last_run_time=float(d.get("last_run_time", 0) or 0),
            last_run_iter=int(d.get("last_run_iter", -1) if d.get("last_run_iter") is not None else -1),
            last_result=d.get("last_result"),
            error_streak=int(d.get("error_streak", 0) or 0),
        )


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False)
        return True
    except Exception:
        return False


# ── Main manager ────────────────────────────────────────────────────────────


class WatcherManager:
    """Registry + scheduler + executor."""

    def __init__(
        self,
        registry_path: Optional[Path] = None,
        artifacts_dir: Optional[Path] = None,
    ) -> None:
        if registry_path is None:
            registry_path = Path(
                os.environ.get("SENZA_AGENT_WATCHERS_REGISTRY")
                or ".senza-agent/watchers.json"
            )
        self.registry_path = Path(registry_path).resolve()
        self.artifacts_dir = Path(artifacts_dir).resolve() if artifacts_dir else None
        self._lock = threading.Lock()
        self._entries: dict = {}
        self._module_cache: dict = {}
        self.load()

    # ── registry persistence ──────────────────────────────────────────────────

    def load(self) -> None:
        if not self.registry_path.exists():
            return
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
            items = raw.get("watchers", []) if isinstance(raw, dict) else []
            with self._lock:
                self._entries = {
                    str(item.get("name", "")): WatcherEntry.from_dict(item)
                    for item in items
                    if item.get("name")
                }
        except Exception:
            pass

    def save(self) -> None:
        try:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = {"watchers": [e.to_dict() for e in self._entries.values()]}
            tmp = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.registry_path)
        except Exception:
            pass

    # ── registration management ───────────────────────────────────────────────

    def register(
        self,
        name: str,
        path: str,
        interval: int = 10,
        emit: str = "event",
        params: Optional[dict] = None,
        enabled: bool = True,
        desc: str = "",
    ) -> dict:
        if not name or not isinstance(name, str):
            return {"ok": False, "error": "name is required"}
        abs_path = Path(path).expanduser().resolve()
        if not abs_path.exists():
            return {"ok": False, "error": f"code file does not exist: {abs_path}"}
        if abs_path.suffix not in (".py", ".sh"):
            return {"ok": False, "error": f"only .py or .sh files are supported, got: {abs_path.suffix}"}
        if emit not in ("event", "live"):
            return {"ok": False, "error": f"emit must be 'event' or 'live', got: {emit}"}

        entry = WatcherEntry(
            name=name, path=str(abs_path), interval=max(1, int(interval or 10)),
            emit=emit, enabled=bool(enabled), params=dict(params or {}), desc=str(desc or ""),
        )
        with self._lock:
            self._entries[name] = entry
        self.save()
        return {"ok": True, "name": name, "path": str(abs_path)}

    def unregister(self, name: str) -> dict:
        with self._lock:
            if name not in self._entries:
                return {"ok": False, "error": f"watcher '{name}' does not exist"}
            del self._entries[name]
        self.save()
        return {"ok": True, "name": name}

    def set_enabled(self, name: str, enabled: bool) -> dict:
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return {"ok": False, "error": f"watcher '{name}' does not exist"}
            entry.enabled = bool(enabled)
        self.save()
        return {"ok": True, "name": name, "enabled": bool(enabled)}

    def update(self, name: str, **fields) -> dict:
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return {"ok": False, "error": f"watcher '{name}' does not exist"}
            for k, v in fields.items():
                if v is None:
                    continue
                if k == "interval":
                    entry.interval = max(1, int(v))
                elif k == "emit":
                    if v not in ("event", "live"):
                        return {"ok": False, "error": "emit must be 'event' or 'live'"}
                    entry.emit = v
                elif k == "params" and isinstance(v, dict):
                    entry.params = dict(v)
                elif k == "enabled":
                    entry.enabled = bool(v)
                elif k == "desc":
                    entry.desc = str(v)
                elif k == "path":
                    abs_path = Path(v).expanduser().resolve()
                    if not abs_path.exists():
                        return {"ok": False, "error": f"code file does not exist: {abs_path}"}
                    entry.path = str(abs_path)
        self.save()
        return {"ok": True, "name": name}

    def list_entries(self) -> list:
        with self._lock:
            return [
                {
                    "name": e.name, "path": e.path, "interval": e.interval,
                    "emit": e.emit, "enabled": e.enabled, "desc": e.desc,
                    "params": e.params, "last_run_iter": e.last_run_iter,
                    "error_streak": e.error_streak,
                }
                for e in self._entries.values()
            ]

    # ── scheduling + execution ────────────────────────────────────────────────

    def poll(self, iter_n: int) -> list:
        events: list = []
        now = time.time()
        with self._lock:
            entries = list(self._entries.values())

        for entry in entries:
            if not entry.enabled:
                continue
            if (now - entry.last_run_time) < entry.interval:
                continue

            try:
                result = self._execute(entry, iter_n)
            except Exception as e:
                tb = traceback.format_exc(limit=2)
                events.append({
                    "name": entry.name, "emit": entry.emit, "kind": "error",
                    "content": f"[env] watcher `{entry.name}` error: {type(e).__name__}: {e}",
                    "image_block": None, "_traceback": tb,
                })
                entry.error_streak += 1
                entry.last_run_time = now
                entry.last_run_iter = iter_n
                continue

            entry.last_run_time = now
            entry.last_run_iter = iter_n
            entry.error_streak = 0

            if result is None:
                continue

            entry.last_result = result if _json_safe(result) else None

            normalized = self._normalize_and_cap(entry, result, iter_n)
            if normalized is not None:
                events.append(normalized)

        if events or any(e.last_run_time == now for e in entries):
            self.save()

        return events

    # ── execution dispatch ────────────────────────────────────────────────────

    def _execute(self, entry: WatcherEntry, iter_n: int) -> Any:
        ext = Path(entry.path).suffix.lower()
        store_view = dict(entry.store)
        store_view["params"] = dict(entry.params)
        prev = entry.last_result

        if ext == ".py":
            module = self._load_py_module(entry.path)
            run_fn = getattr(module, "run", None)
            if not callable(run_fn):
                raise RuntimeError(f"{entry.path} does not define run(prev, store, iter_n)")
            result = run_fn(prev, store_view, iter_n)
        elif ext == ".sh":
            result = self._execute_sh(entry, store_view, iter_n)
        else:
            raise RuntimeError(f"unsupported file type: {ext}")

        store_view.pop("params", None)
        entry.store = store_view
        return result

    def _load_py_module(self, path: str) -> Any:
        try:
            mtime = os.path.getmtime(path)
        except OSError as e:
            raise RuntimeError(f"cannot read {path}: {e}")
        cached = self._module_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        mod_name = f"_senza_watcher_{uuid.uuid4().hex[:8]}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load module: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        self._module_cache[path] = (mtime, module)
        return module

    def _execute_sh(self, entry: WatcherEntry, store_view: dict, iter_n: int) -> Any:
        store_file = None
        if self.artifacts_dir:
            try:
                self.artifacts_dir.mkdir(parents=True, exist_ok=True)
                store_file = self.artifacts_dir / f"_watcher_store_{entry.name}.json"
                writable = {k: v for k, v in store_view.items() if k != "params"}
                store_file.write_text(json.dumps(writable, ensure_ascii=False), encoding="utf-8")
            except Exception:
                store_file = None

        env = dict(os.environ)
        env["WATCHER_PARAMS_JSON"] = json.dumps(entry.params, ensure_ascii=False)
        env["WATCHER_ITER"] = str(iter_n)
        if store_file:
            env["WATCHER_STORE_FILE"] = str(store_file)

        try:
            proc = subprocess.run(
                entry.path if os.name != "nt" else ["bash", entry.path],
                shell=(os.name != "nt"),
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=env,
                timeout=max(5, entry.interval),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("shell watcher timed out")

        if store_file and store_file.exists():
            try:
                updated = json.loads(store_file.read_text(encoding="utf-8"))
                if isinstance(updated, dict):
                    for k, v in updated.items():
                        if k == "params":
                            continue
                        store_view[k] = v
            except Exception:
                pass

        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:200]
            raise RuntimeError(f"shell watcher exit code {proc.returncode}: {err}")

        out = (proc.stdout or "").strip()
        if not out:
            return None
        return {"type": "text", "content": out}

    # ── output normalisation + 500-char cap + overflow spill ─────────────────

    def _normalize_and_cap(self, entry: WatcherEntry, result: Any, iter_n: int) -> Optional[dict]:
        if not isinstance(result, dict):
            result = {"type": "text", "content": str(result)}
        kind = str(result.get("type", "text")).lower()

        if kind == "text":
            text = str(result.get("content", "") or "")
            if not text.strip():
                return None
            return self._wrap_text_event(entry, text, iter_n)

        if kind == "path":
            path = str(result.get("path", "") or "")
            if not path:
                return None
            content = self._format_path_injection(entry, path, hint="code spilled")
            return {
                "name": entry.name, "emit": entry.emit, "kind": "path",
                "content": content, "image_block": None,
                "spill_path": path, "spill_chars": 0,
            }

        if kind == "image":
            image_block = result.get("image_block")
            if not isinstance(image_block, dict):
                return None
            return {
                "name": entry.name, "emit": entry.emit, "kind": "image",
                "content": f"[env] watcher `{entry.name}` output image (iter={iter_n})",
                "image_block": image_block,
            }

        return None

    def _wrap_text_event(self, entry: WatcherEntry, text: str, iter_n: int) -> dict:
        header = f"[env] {entry.name}: "
        candidate = header + text
        if len(candidate) <= INJECT_HARD_CAP:
            return {
                "name": entry.name, "emit": entry.emit, "kind": "text",
                "content": candidate, "image_block": None,
            }
        spill_path = self._spill(entry.name, iter_n, text)
        content = self._format_path_injection(entry, spill_path or "(spill failed)", hint=f"{len(text)} chars overflowed")
        return {
            "name": entry.name, "emit": entry.emit, "kind": "path",
            "content": content, "image_block": None,
            "spill_path": spill_path, "spill_chars": len(text),
        }

    def _format_path_injection(self, entry: WatcherEntry, path: str, hint: str = "") -> str:
        body = f"[env] {entry.name} [overflow] {hint} -> {path}"
        if len(body) > INJECT_HARD_CAP:
            head = f"[env] {entry.name} [overflow] {hint} -> ...{path[-(INJECT_HARD_CAP - 80):]}"
            return head[:INJECT_HARD_CAP]
        return body

    def _spill(self, name: str, iter_n: int, text: str) -> Optional[str]:
        if not self.artifacts_dir:
            return None
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            safe = name.replace("/", "_").replace("\\", "_")
            target = self.artifacts_dir / f"{SPILL_PREFIX}_{safe}_iter{iter_n}.log"
            target.write_text(text, encoding="utf-8")
            return str(target)
        except Exception:
            return None


# ── Module-level singleton ──────────────────────────────────────────────────

_default_manager: Optional[WatcherManager] = None
_manager_lock = threading.Lock()


def get_manager() -> WatcherManager:
    """Lazily initialise the module-level WatcherManager singleton."""
    global _default_manager
    if _default_manager is None:
        with _manager_lock:
            if _default_manager is None:
                artifacts_dir = None
                rd = os.environ.get("RUN_DIR") or os.environ.get("SENZA_AGENT_RUN_DIR")
                if rd:
                    artifacts_dir = Path(rd) / "artifacts"
                _default_manager = WatcherManager(artifacts_dir=artifacts_dir)
    return _default_manager
