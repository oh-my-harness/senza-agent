"""
On-disk artifact index — a deterministic file manifest that survives compression.

Ports QevosAgent's ``artifact_index`` module into a self-contained class. Large
content is spilled to disk before entering the context; the path is normally
the only carrier of that fact, and compression can drop the carrying message —
leaving the file on disk but the agent unaware of it. This module records each
spill into an index and renders it back as fixed text on every handoff, so the
manifest does not decay across compression generations.

Adapted for senza-agent: the module-level functions operating on a shared
``state`` are replaced by an ``ArtifactIndex`` class that owns its own list and
is bound to a ``run_dir``. The simplified ``register(path, description)``
public API is preserved; richer metadata (source/tool/chars/iter) is accepted
via optional keyword arguments for callers that have it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from senza_agent.i18n import t

# Max entries kept in the index (older ones are evicted).
_MAX_ENTRIES = int(os.environ.get("ARTIFACT_INDEX_MAX", "30"))
# Hard cap on the rendered manifest text, to keep it from blowing up the context.
_MANIFEST_MAX_CHARS = int(os.environ.get("ARTIFACT_MANIFEST_MAX_CHARS", "1800"))

# Sources evicted first when the index overflows: spills are regenerable
# intermediate state, while write_file outputs are deliverables the model
# produced on purpose — keep those as long as possible.
_EVICTABLE_SOURCES = frozenset({"spill", "watcher"})

_SOURCE_LABEL_KEYS = {
    "spill": "artifact.src.spill",
    "watcher": "artifact.src.watcher",
    "write_file": "artifact.src.write_file",
}


def _display_path(path: str, run_dir: str) -> str:
    """Prefer a path relative to the run dir — the manifest should be short and
    still readable on a different machine."""
    s = str(path or "").strip()
    if not s or not run_dir:
        return s
    try:
        rel = os.path.relpath(Path(s).resolve(), Path(run_dir).resolve())
    except Exception:
        return s
    # Paths escaping the run dir (../..) stay absolute — relative would be harder.
    if rel.startswith(".."):
        return s
    return rel.replace(os.sep, "/")


class ArtifactIndex:
    """Append-style on-disk artifact manifest bound to a run directory.

    Repeated writes to the same path update the entry in place (no duplicates),
    preserving its original position so the manifest still reads as a timeline.
    """

    def __init__(self, run_dir: str):
        self.run_dir = str(run_dir or "")
        self._entries: list[dict] = []

    # ── registration ──────────────────────────────────────────────────────────

    def register(
        self,
        path: str,
        description: str = "",
        *,
        source: str = "",
        tool: str = "",
        chars: int = 0,
        iter_n: Optional[int] = None,
    ) -> None:
        """Register an artifact that has definitely been written to disk.

        Any exception is swallowed silently — this must never break the main
        flow. Re-registering the same path updates the entry in place.
        """
        if not path:
            return
        try:
            disp = _display_path(path, self.run_dir)
            if not disp:
                return

            entry = {
                "path": disp,
                "description": str(description or ""),
                "source": str(source or "?"),
                "tool": str(tool or ""),
                "iter": int(iter_n) if iter_n is not None else 0,
                "chars": int(chars or 0),
            }

            for i, old in enumerate(self._entries):
                if isinstance(old, dict) and old.get("path") == disp:
                    self._entries[i] = entry
                    return

            self._entries.append(entry)

            # Overflow eviction: eat regenerable spills first, only touch
            # deliverables when there's nothing else left.
            while len(self._entries) > _MAX_ENTRIES:
                victim = 0
                for i, e in enumerate(self._entries):
                    if isinstance(e, dict) and e.get("source") in _EVICTABLE_SOURCES:
                        victim = i
                        break
                self._entries.pop(victim)
        except Exception:
            pass

    # ── read-out ──────────────────────────────────────────────────────────────

    def manifest(self) -> list[dict]:
        """Return a copy of the registered artifacts (read-only for callers)."""
        return [e for e in self._entries if isinstance(e, dict) and e.get("path")]

    def render(self, max_chars: Optional[int] = None) -> str:
        """Render the manifest as text for LLM context; empty string when none.

        When over the character cap, the *oldest* entries are dropped first and
        the omission count is noted — newer artifacts are more likely to be
        relevant, and older ones already appeared in earlier handoffs.
        """
        entries = self.manifest()
        if not entries:
            return ""

        cap = _MANIFEST_MAX_CHARS if max_chars is None else int(max_chars)
        header = t("artifact.manifest_header") + "\n" + t("artifact.manifest_hint")

        lines = [self._format_entry(e) for e in entries]
        dropped = 0
        while lines:
            body = "\n".join(lines)
            note = ("\n" + t("artifact.manifest_omitted", n=dropped)) if dropped else ""
            out = f"{header}\n{body}{note}"
            if len(out) <= cap:
                return out
            lines.pop(0)
            dropped += 1

        return ""

    def _format_entry(self, entry: dict) -> str:
        source = entry.get("source", "")
        label = t(_SOURCE_LABEL_KEYS.get(source, "artifact.src.other"))
        tool = entry.get("tool") or ""
        if tool:
            label = f"{label}({tool})"
        bits = [label, f"iter{int(entry.get('iter', 0) or 0)}"]
        chars = int(entry.get("chars", 0) or 0)
        if chars > 0:
            bits.append(t("artifact.chars", n=chars))
        desc = entry.get("description") or ""
        if desc:
            bits.append(str(desc))
        return f"- {entry['path']} — " + " · ".join(bits)
