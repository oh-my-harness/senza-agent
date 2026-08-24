"""Startup dependency self-check.

Motivation: a missing optional dependency (e.g. paramiko) should not silently
degrade — at startup we probe each declared dependency and print a loud banner
if any are missing.  The check itself never blocks startup; set
``SENZA_STRICT_DEPS=1`` to abort on missing *core* deps.

Design:
- Uses ``importlib.util.find_spec`` (no heavy imports like cv2).
- Two tiers: *core* (missing → weird failures) and *optional* (missing → that
  feature is unavailable).
- Non-fatal by default: prints a banner and continues.
"""
from __future__ import annotations

import importlib.util
import os
import sys

# (import name, pip package, tier, description)
_DEPS: list[tuple[str, str, str, str]] = [
    # ── core ──────────────────────────────────────────────────────────────
    ("senza",       "senza-sdk",       "core",     "Senza runtime SDK (agent harness)"),
    # ── optional ──────────────────────────────────────────────────────────
    ("paramiko",    "paramiko",        "optional", "SSH / remote shell tool (ssh_execute)"),
    ("cv2",         "opencv-python",   "optional", "Video frame extraction (load_video)"),
    ("playwright",  "playwright",      "optional", "Browser automation (web_interact)"),
]


def _is_available(import_name: str) -> bool:
    """Return True if *import_name* can be resolved without importing it."""
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        # find_spec can raise if a parent package itself is half-broken.
        return False


def check_dependencies(strict: bool | None = None) -> dict:
    """Probe all declared dependencies; print a banner for missing ones.

    Returns ``{'missing_core': [...], 'missing_optional': [...]}`` where each
    element is ``(import_name, pip_name, description)``.

    When *strict* is true and core deps are missing, ``sys.exit(1)`` is called.
    *strict=None* reads the ``SENZA_STRICT_DEPS`` environment variable.
    """
    if strict is None:
        strict = os.environ.get("SENZA_STRICT_DEPS", "0") not in (
            "0", "", "false", "False",
        )

    missing_core: list[tuple[str, str, str]] = []
    missing_optional: list[tuple[str, str, str]] = []

    for import_name, pip_name, tier, desc in _DEPS:
        if _is_available(import_name):
            continue
        (missing_core if tier == "core" else missing_optional).append(
            (import_name, pip_name, desc)
        )

    if missing_core or missing_optional:
        all_pip = [p for _, p, _ in missing_core + missing_optional]
        lines = [
            "",
            "=" * 64,
            "⚠️  Dependency check: missing third-party libraries",
            "=" * 64,
        ]
        if missing_core:
            lines.append("[core] Missing — will cause weird failures, install first:")
            for _imp, pip_name, desc in missing_core:
                lines.append(f"    ✗ {pip_name:16} — {desc}")
        if missing_optional:
            lines.append("[optional] Missing — corresponding feature unavailable:")
            for _imp, pip_name, desc in missing_optional:
                lines.append(f"    · {pip_name:16} — {desc}")
        lines.append("-" * 64)
        lines.append("Install all (use the same Python that runs senza-agent):")
        lines.append(f"    pip install {' '.join(all_pip)}")
        lines.append("=" * 64)
        sys.stderr.write("\n".join(lines) + "\n")
        sys.stderr.flush()

        if strict and missing_core:
            sys.stderr.write(
                "SENZA_STRICT_DEPS is set and core dependencies are missing — aborting.\n"
            )
            sys.exit(1)

    return {"missing_core": missing_core, "missing_optional": missing_optional}
