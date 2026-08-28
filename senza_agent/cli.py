#!/usr/bin/env python3
"""senza-agent CLI — command-line entry point.

Ported from QevosAgent's ``run_goal.py``.  Provides:

- Fault-tolerant stdio (broken-pipe tolerant stdout/stderr wrappers)
- ``.env`` loading (no external dependency)
- API slot system (primary / backup / backup2) with endpoint probing
- ``argparse``-based CLI with ``--nostop``, ``--skills``, ``--inspect``, etc.
- Event-stream printing (text deltas, tool calls, terminal events)
- Interactive REPL when no task is given

The Senza SDK (``senza``), the agent factory (``senza_agent.agent``), and the
inspector (``senza_agent.inspector``) are imported *lazily* — any of them may be
absent during early development.  The CLI degrades gracefully: ``--help`` and
dependency checks always work; a missing SDK prints a clear error instead of a
stack trace.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


# ── ANSI colors (best-effort; fine if terminal strips them) ──────────────────
GREEN = "\033[92m"
BLUE = "\033[94m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


# ═══════════════════════════════════════════════════════════════════════════════
# Fault-tolerant stdio
# ═══════════════════════════════════════════════════════════════════════════════


class _FaultTolerantTextIO:
    """Wrap a text stream (stdout/stderr) so a broken downstream never aborts the run.

    If the reader of our stdout/stderr goes away mid-run (e.g. a dashboard
    closes its socket), the next write/flush raises ``BrokenPipeError`` (EPIPE)
    or ``OSError`` (EBADF).  Console output is best-effort telemetry — the run's
    real state is persisted independently — so an unwritable console must NOT
    crash the agent, nor make the interpreter's shutdown stdout-flush fail
    (which returns exit code 120).  Once the stream breaks we silently drop
    further output for the rest of the run.
    """

    def __init__(self, stream):
        self._stream = stream
        self._broken = False

    def write(self, s):
        if self._broken:
            return len(s) if isinstance(s, str) else 0
        try:
            return self._stream.write(s)
        except (BrokenPipeError, OSError):
            self._broken = True
            return len(s) if isinstance(s, str) else 0

    def flush(self):
        if self._broken:
            return
        try:
            self._stream.flush()
        except (BrokenPipeError, OSError):
            self._broken = True

    def __getattr__(self, name):
        # Delegate everything else (encoding, fileno, isatty, buffer, ...).
        return getattr(self._stream, name)


def install_fault_tolerant_stdio():
    """Replace ``sys.stdout``/``sys.stderr`` with broken-pipe-tolerant wrappers.

    Idempotent and never raises — a failure here must not block startup.
    Call once, before any console output.
    """
    # Enable ANSI escape code processing on Windows 10+ (cmd.exe / PowerShell
    # default to disabled, causing \033[9Xm to show as literal text).
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            for _handle in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
                handle = kernel32.GetStdHandle(_handle)
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass

    try:
        if not isinstance(sys.stdout, _FaultTolerantTextIO):
            sys.stdout = _FaultTolerantTextIO(sys.stdout)
        if not isinstance(sys.stderr, _FaultTolerantTextIO):
            sys.stderr = _FaultTolerantTextIO(sys.stderr)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# .env loading
# ═══════════════════════════════════════════════════════════════════════════════


def load_dotenv_if_present(path: str = ".env"):
    """Load simple ``KEY=VALUE`` pairs from a ``.env`` file into ``os.environ``.

    Existing environment variables win over ``.env`` values.
    This keeps the runtime lightweight and avoids an extra dependency.
    """
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


# ═══════════════════════════════════════════════════════════════════════════════
# API slot system
# ═══════════════════════════════════════════════════════════════════════════════

# Three LLM API slots that can fall back to each other.
# Slot "primary" reuses the unprefixed OPENAI_* vars; whichever slot wins the
# probe, its values are written back to OPENAI_* so the rest of the code (agent,
# tools, subprocesses) only reads OPENAI_*.
# PREFERRED_API decides which slot to try first; the rest fall back in slot
# order.  Unset → primary.
API_SLOTS = ["primary", "backup", "backup2"]
_SLOT_PREFIX = {"primary": "OPENAI", "backup": "BACKUP", "backup2": "BACKUP2"}
_SLOT_LABEL = {"primary": "API 1", "backup": "API 2", "backup2": "API 3"}


def preferred_api_slot() -> str:
    """Return the preferred slot, honoring ``PREFERRED_API`` env var."""
    slot = (os.environ.get("PREFERRED_API") or "").strip().lower()
    return slot if slot in API_SLOTS else "primary"


def _slot_config(slot: str) -> dict:
    """Build the config dict for a slot from its env-var prefix."""
    prefix = _SLOT_PREFIX[slot]
    return {
        "slot": slot,
        "base": (os.environ.get(prefix + "_OPENAI_BASE_URL") or "").strip(),
        "key": os.environ.get(prefix + "_OPENAI_API_KEY"),
        "model": (os.environ.get(prefix + "_OPENAI_MODEL") or "").strip(),
        "temp": (os.environ.get(prefix + "_OPENAI_TEMPERATURE") or "").strip(),
        "json_mode": (os.environ.get(prefix + "_OPENAI_JSON_MODE") or "").strip(),
        "thinking": (os.environ.get(prefix + "_OPENAI_THINKING_BUDGET") or "").strip(),
    }


def api_slot_candidates() -> list[dict]:
    """Yield ``(slot, prefix)`` tuples in probe order, keeping only slots with a base URL.

    The preferred slot comes first; the rest follow in slot order.
    """
    preferred = preferred_api_slot()
    order = [preferred] + [s for s in API_SLOTS if s != preferred]
    return [c for c in (_slot_config(s) for s in order) if c["base"]]


def ensure_env_defaults():
    """Load ``.env`` and set default env vars for the run."""
    load_dotenv_if_present()

    # NOTE: SENZA_AGENT_RUNS_DIR is intentionally NOT defaulted here.
    # The webserver's _resolve_runs_dir() owns resolution:
    # explicit env > ~/.senza-agent/runs under a packaged app > <agent dir>/runs.
    # Defaulting "./runs" here would shadow the packaged-app case.
    os.environ.setdefault("AUTO_SAVE_SNAPSHOT_ON_EXIT", "1")
    os.environ.setdefault("AUTO_REMEMBER_ON_DONE", "1")

    # Map standard env vars to the primary slot if the slot isn't set.
    # This lets users export OPENAI_API_KEY / OPENAI_API_BASE / OPENAI_MODEL
    # without learning the slot prefix convention.
    if not os.environ.get("OPENAI_OPENAI_BASE_URL"):
        std_base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
        if std_base:
            os.environ["OPENAI_OPENAI_BASE_URL"] = std_base
    if not os.environ.get("OPENAI_OPENAI_API_KEY"):
        if os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
    if not os.environ.get("OPENAI_OPENAI_MODEL"):
        if os.environ.get("OPENAI_MODEL"):
            os.environ["OPENAI_OPENAI_MODEL"] = os.environ["OPENAI_MODEL"]

    if not api_slot_candidates():
        # Not fatal — the user may configure via senza_agent.config instead.
        # Print a hint but do not raise.
        print(
            f"{YELLOW}[senza-agent] No OPENAI_BASE_URL configured in any API slot. "
            f"Set it in .env or ~/.senza-agent/config.json.{RESET}"
        )


# ── Proxy helpers ─────────────────────────────────────────────────────────────


def _should_bypass_proxy(base_url: str) -> bool:
    """Return True if *base_url* should bypass the proxy.

    Covers: loopback, RFC1918 private ranges, link-local, common internal
    domain suffixes (.local / .lan / .internal), and hosts listed in NO_PROXY.
    """
    from urllib.parse import urlparse
    import ipaddress

    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return True

    no_proxy = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "")
    for entry in [e.strip().lower() for e in no_proxy.split(",") if e.strip()]:
        bare = entry.lstrip(".")
        if host == bare or host.endswith("." + bare):
            return True

    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return True
    except ValueError:
        if (
            host == "localhost"
            or host.endswith(".local")
            or host.endswith(".lan")
            or host.endswith(".internal")
        ):
            return True
    return False


def _urlopen_no_env_proxy(url: str, headers: dict[str, str], timeout: int = 10):
    """GET *url* using stdlib urllib, with explicit proxy control.

    Replaces httpx — the probe only needs a simple GET /models, so the
    standard library suffices and removes an extra dependency.
    """
    import urllib.request

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    bypass = _should_bypass_proxy(url)

    if bypass:
        # Direct connection — ignore proxy env vars entirely.
        handler = urllib.request.ProxyHandler({})
    else:
        proxy = (
            os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
            or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
        )
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            handler = urllib.request.ProxyHandler()  # respect env (no-op)

    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    return opener.open(req, timeout=timeout)


# ── Endpoint probing ──────────────────────────────────────────────────────────


def _probe_one_endpoint(base_url: str, api_key, model: str, list_models=None) -> list[str]:
    """Probe an OpenAI-compatible endpoint; return model id list on success."""
    if list_models is not None:
        try:
            resp = list_models()
        except Exception as e:
            raise RuntimeError(f"Cannot connect to {base_url}. Error: {e}") from e
        ids = []
        for item in getattr(resp, "data", []) or []:
            mid = getattr(item, "id", None)
            if mid:
                ids.append(str(mid))
        return ids

    # Use stdlib urllib to avoid a hard dependency on httpx or the `openai` package.
    import json as _json
    import urllib.error

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = _urlopen_no_env_proxy(url, headers, timeout=10)
        body = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Cannot connect to {base_url}. Error: {e}") from e

    if not body:
        raise RuntimeError(f"Empty response from {url}")

    try:
        payload = _json.loads(body)
    except _json.JSONDecodeError:
        snippet = body[:200].decode("utf-8", errors="replace")
        raise RuntimeError(f"Non-JSON response from {url}: {snippet}") from None

    ids = []
    for item in payload.get("data", []) or []:
        mid = item.get("id") if isinstance(item, dict) else None
        if mid:
            ids.append(str(mid))
    return ids


def probe_openai_configuration(list_models=None) -> dict:
    """Verify the configured OpenAI-compatible endpoint before starting the agent.

    Returns a dict describing the resolved model.  If the configured model is
    missing but the server exposes exactly one model, auto-switch to it.
    """
    preferred = preferred_api_slot()
    candidates = api_slot_candidates()

    if not candidates:
        raise ValueError("LLM probe failed: OPENAI_BASE_URL is not set.")

    errors: list[tuple[str, Exception]] = []
    active = None
    for idx, cand in enumerate(candidates):
        probe_fn = list_models if idx == 0 else None
        try:
            model_ids = _probe_one_endpoint(cand["base"], cand["key"], cand["model"], probe_fn)
        except RuntimeError as err:
            errors.append((cand["slot"], err))
            if idx + 1 < len(candidates):
                print(
                    f"[senza-agent] probe: {_SLOT_LABEL[cand['slot']]} failed ({err}); "
                    f"falling back to {_SLOT_LABEL[candidates[idx + 1]['slot']]}…"
                )
            continue
        active = cand
        break

    if active is None:
        if len(errors) == 1:
            slot, err = errors[0]
            raise RuntimeError(
                f"LLM probe failed: {err}. Check {_SLOT_LABEL[slot]}'s base URL / network / service."
            ) from err
        detail = "\n".join(f"  {_SLOT_LABEL[slot]}: {err}" for slot, err in errors)
        raise RuntimeError("LLM probe failed: all configured APIs are unreachable.\n" + detail) from errors[0][1]

    # Write the winning slot back to OPENAI_*.
    active_base, active_model = active["base"], active["model"]
    os.environ["OPENAI_BASE_URL"] = active_base
    if active["key"] is not None:
        os.environ["OPENAI_API_KEY"] = active["key"]
    if active_model:
        os.environ["OPENAI_MODEL"] = active_model
    if active["temp"]:
        os.environ["OPENAI_TEMPERATURE"] = active["temp"]
    else:
        os.environ.pop("OPENAI_TEMPERATURE", None)
    for _key, _slot_field in (
        ("OPENAI_JSON_MODE", "json_mode"),
        ("OPENAI_THINKING_BUDGET", "thinking"),
    ):
        if active[_slot_field]:
            os.environ[_key] = active[_slot_field]
        else:
            os.environ.pop(_key, None)

    if active_model in model_ids:
        return {
            "base_url": active_base,
            "configured_model": active_model,
            "resolved_model": active_model,
            "available_models": model_ids,
            "auto_selected": False,
            "active_endpoint": active["slot"],
            "preferred_endpoint": preferred,
        }

    if len(model_ids) == 1:
        resolved = model_ids[0]
        os.environ["OPENAI_MODEL"] = resolved
        return {
            "base_url": active_base,
            "configured_model": active_model,
            "resolved_model": resolved,
            "available_models": model_ids,
            "auto_selected": True,
            "active_endpoint": active["slot"],
            "preferred_endpoint": preferred,
        }

    shown = ", ".join(model_ids[:5]) if model_ids else "(empty list)"
    raise ValueError(
        f"LLM probe failed: configured model `{active_model}` is not in the model list "
        f"returned by {active_base}. Available: {shown}"
    )


def format_probe_summary(probe: dict) -> str:
    """Format a one-line startup summary of the probe result."""
    base_url = probe["base_url"]
    configured = probe["configured_model"]
    resolved = probe["resolved_model"]
    active = probe.get("active_endpoint") or "primary"
    preferred = probe.get("preferred_endpoint") or "primary"
    if active != preferred:
        role_tag = (
            f" [preferred {_SLOT_LABEL.get(preferred, preferred)} unavailable, "
            f"fell back to {_SLOT_LABEL.get(active, active)}]"
        )
    elif active != "primary":
        role_tag = f" [using {_SLOT_LABEL.get(active, active)}]"
    else:
        role_tag = ""
    if probe.get("auto_selected"):
        return (
            f"[senza-agent] probe: endpoint ok{role_tag}; "
            f"configured={configured!r}; resolved={resolved!r}; "
            f"auto-selected the only available model from {base_url}"
        )
    return (
        f"[senza-agent] probe: endpoint ok{role_tag}; "
        f"model={resolved!r}; base_url={base_url}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Argument parser
# ═══════════════════════════════════════════════════════════════════════════════


def build_parser() -> argparse.ArgumentParser:
    """Build the ``argparse`` parser for the ``senza-agent`` CLI."""
    parser = argparse.ArgumentParser(
        prog="senza-agent",
        description="Senza-agent — general-purpose AI agent built on the Senza SDK.",
    )
    parser.add_argument("task", nargs="*", help="Task to execute")
    parser.add_argument(
        "--nostop", action="store_true",
        help="Continuous dialogue mode: after task completion, wait for the next task",
    )
    parser.add_argument(
        "--skills", default="",
        help="Comma-separated skill names (e.g. coding,data_analysis), or 'all' to load all skills",
    )
    parser.add_argument(
        "--agents-profile", default="", dest="agents_profile",
        metavar="AGENTS_*.md",
        help="Full filename of the conventions file to use, e.g. AGENTS_WIN_EN.md",
    )
    parser.add_argument(
        "--advisor-profile", default="", dest="advisor_profile",
        metavar="ADVISOR_*.md",
        help="Full filename of the advisor file to use, e.g. ADVISOR_CRITIC.md",
    )
    parser.add_argument(
        "-m", "--model", default=None,
        help="Override the configured model",
    )
    parser.add_argument(
        "--resume", metavar="SESSION_ID", default=None,
        help="Resume a previous session by ID",
    )
    parser.add_argument(
        "--inspect", metavar="PORT", type=int, nargs="?", const=8080, default=8080,
        help="Mount the Inspector web UI on the given port (default 8080)",
    )
    parser.add_argument(
        "--no-inspect", action="store_true",
        help="Disable the Inspector web UI",
    )
    parser.add_argument(
        "--web", metavar="PORT", type=int, nargs="?", const=8090, default=None,
        help="Launch the web dashboard on the given port (default 8090)",
    )
    parser.add_argument(
        "--spawn", action="store_true", default=None,
        help="Enable sub-agent spawning (spawn_agent tool)",
    )
    parser.add_argument(
        "--provider-type", default=None,
        choices=["openai", "anthropic"],
        help="LLM provider type (default: openai)",
    )
    return parser


# ═══════════════════════════════════════════════════════════════════════════════
# Event printing
# ═══════════════════════════════════════════════════════════════════════════════


def _event_type(ev: dict) -> str:
    """Extract the event type string from an event dict."""
    return ev.get("type") or ev.get("event") or ""


def print_event(ev: dict) -> None:
    """Print a single Senza harness event to stdout.

    - ``text_delta``: printed inline (no newline)
    - ``tool_call_start``: ``[tool: <name>]`` on its own line
    - ``settled``: ``[settled]``
    - ``aborted``: ``[aborted]``
    - ``error``: ``[error: <message>]``
    """
    et = _event_type(ev)
    if et == "text_delta":
        text = ev.get("text") or ev.get("delta") or ""
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
    elif et == "thinking_delta":
        # Don't print thinking by default — too noisy.  Could be toggled.
        pass
    elif et == "tool_call_start":
        name = ev.get("name") or ev.get("tool") or ev.get("tool_name") or "?"
        print(f"\n[{YELLOW}tool: {name}{RESET}]")
    elif et == "tool_call_end":
        # Could print results; keep quiet for now.
        pass
    elif et == "settled":
        print(f"\n[{GREEN}settled{RESET}]")
    elif et == "aborted":
        print(f"\n[{RED}aborted{RESET}]")
    elif et == "error":
        msg = ev.get("message") or ev.get("error") or ""
        print(f"\n[{RED}error: {msg}{RESET}]")


def print_events(events: list[dict]) -> None:
    """Print a batch of events."""
    for ev in events:
        print_event(ev)


# ═══════════════════════════════════════════════════════════════════════════════
# Agent creation (lazy import)
# ═══════════════════════════════════════════════════════════════════════════════


def _create_agent(config, model_override: str | None):
    """Create the Senza agent harness via the full agent assembly.

    Delegates to ``senza_agent.agent.create_agent`` which wires all tools,
    behavior bundle, and strategy plugins.  Returns the harness object.
    """
    try:
        import senza  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "The 'senza' SDK is not installed. Install it with: pip install senza-sdk"
        ) from e

    if model_override:
        config.model = model_override

    from senza_agent.agent import create_agent
    return create_agent(config)


def _mount_inspector(harness, port: int):
    """Mount the Inspector web UI if the SDK supports it.

    Returns the inspector handle or ``None``.  Never raises — Inspector is
    optional.
    """
    # Try senza_agent.inspector first (our own module), then the SDK method.
    try:
        from senza_agent.inspector import mount_inspector
        return mount_inspector(harness, port=port)
    except Exception:
        pass
    # SDK-level mount_inspector (not yet in all builds).
    mount = getattr(harness, "mount_inspector", None)
    if callable(mount):
        try:
            return mount(port=port)
        except TypeError:
            return mount(port)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Cost printing
# ═══════════════════════════════════════════════════════════════════════════════


def _print_cost(harness) -> None:
    """Print the total cost / usage at the end of a run."""
    try:
        usage = harness.usage()
    except Exception:
        return
    if not usage:
        return
    # usage is a CostAggregate dict
    cost = usage.get("total_cost") or usage.get("cost") or 0.0
    tokens_in = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
    tokens_out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
    print(f"\n{BLUE}═══ Usage ═══{RESET}")
    print(f"  Cost:   ${cost:.4f}")
    print(f"  Tokens: {tokens_in:,} in / {tokens_out:,} out")


# ═══════════════════════════════════════════════════════════════════════════════
# Run loop
# ═══════════════════════════════════════════════════════════════════════════════


def _run_single_task(harness, task: str, timeout_ms: int = 300000) -> None:
    """Run a single task to completion, printing events as they arrive."""
    print(f"{BLUE}▶ Running task:{RESET} {task[:200]}\n")
    try:
        events = harness.prompt_and_collect(task, timeout_ms=timeout_ms)
        print_events(events)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Interrupted by user.{RESET}")
        try:
            harness.abort()
        except Exception:
            pass


def _interactive_repl(harness, timeout_ms: int = 300000) -> None:
    """Read tasks from stdin and run them one at a time."""
    print(f"{BLUE}Interactive mode. Type 'exit' / 'quit' / 'q' to quit.{RESET}\n")
    while True:
        try:
            line = input(f"{BLUE}>>> {RESET}")
        except (EOFError, KeyboardInterrupt):
            print(f"\n{YELLOW}Bye.{RESET}")
            break
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("exit", "quit", "q"):
            print(f"{YELLOW}Bye.{RESET}")
            break
        _run_single_task(harness, line, timeout_ms=timeout_ms)
        print()  # blank line between rounds


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.  Returns the process exit code."""
    # 1. Fault-tolerant stdio — before any print.
    install_fault_tolerant_stdio()

    # 2. Load .env file (if present).
    load_dotenv_if_present()

    # 3a. Load dashboard-saved settings (~/.senza-agent/settings.json) into env.
    # Must happen BEFORE ensure_env_defaults so that OPENAI_BASE_URL etc.
    # from settings.json are visible when slots are mapped.
    try:
        from senza_agent.config import load_settings_into_env
        load_settings_into_env()
    except Exception:
        pass

    # 3b. Set env defaults and map standard OPENAI_* vars to slot prefixes.
    ensure_env_defaults()

    # 4. Parse args.
    parser = build_parser()
    args = parser.parse_args(argv)

    # 5. Probe API configuration (best-effort — config may use senza_agent.config).
    probe = None
    try:
        probe = probe_openai_configuration()
        print(format_probe_summary(probe))
    except Exception as e:
        print(f"{YELLOW}[senza-agent] probe: skipped ({e}){RESET}")

    # 6. Run depcheck.
    try:
        from senza_agent.depcheck import check_dependencies
        check_dependencies()
    except Exception:
        pass

    # 7. Load config.
    try:
        from senza_agent.config import load_config
        config = load_config()
    except Exception as e:
        print(f"{RED}[senza-agent] config load failed: {e}{RESET}")
        config = None

    # 8. Apply CLI overrides to config.
    model_override = args.model
    if args.spawn:
        config.spawn_enabled = True
    if args.provider_type:
        config.provider_type = args.provider_type

    # 9. Create agent (lazy import — may fail during early development).
    harness = None
    try:
        if config is not None:
            harness = _create_agent(config, model_override)
    except ImportError as e:
        print(f"{RED}[senza-agent] {e}{RESET}")
        print(f"{YELLOW}The CLI will start in limited mode (no agent).{RESET}")
    except Exception as e:
        print(f"{RED}[senza-agent] Agent creation failed: {e}{RESET}")
        print(f"{YELLOW}The CLI will start in limited mode (no agent).{RESET}")

    # 10. Mount Inspector (unless --no-inspect).
    inspector = None
    if harness is not None and not args.no_inspect:
        port = args.inspect if args.inspect else 8080
        inspector = _mount_inspector(harness, port)
        if inspector is not None:
            print(f"{BLUE}[senza-agent] Inspector: http://localhost:{port}{RESET}")
        else:
            print(
                f"{YELLOW}[senza-agent] Inspector not available "
                f"(SDK does not expose mount_inspector).{RESET}"
            )

    # 10b. Launch web dashboard (if --web).
    webserver = None
    if harness is not None and args.web is not None:
        try:
            from senza_agent.webserver import WebServer
            webserver = WebServer(port=args.web)
            webserver.set_harness(harness)
            webserver.start_in_thread()
            print(f"{BLUE}[senza-agent] Dashboard: http://localhost:{args.web}{RESET}")
        except Exception as e:
            print(f"{YELLOW}[senza-agent] Dashboard not available: {e}{RESET}")
            webserver = None

    # 11–13. Run.
    task = " ".join(args.task).strip()
    nostop = args.nostop

    # Expose skills / profile args to env for downstream modules.
    if args.skills:
        os.environ["SENZA_AGENT_SKILLS"] = args.skills
    if args.agents_profile:
        os.environ["SENZA_AGENT_AGENTS_PROFILE"] = args.agents_profile
    if args.advisor_profile:
        os.environ["SENZA_AGENT_ADVISOR_PROFILE"] = args.advisor_profile
    if args.resume:
        os.environ["SENZA_AGENT_RESUME_SESSION"] = args.resume

    if harness is None:
        # Limited mode: can't run tasks, but --help / depcheck already worked.
        if task:
            print(f"{RED}Cannot run task — agent is not available.{RESET}")
            return 1
        print(f"{YELLOW}No agent available. Exiting.{RESET}")
        return 0

    # --resume: try to resume a session.
    if args.resume and hasattr(harness, "navigate_tree"):
        try:
            harness.navigate_tree(args.resume)
            print(f"{BLUE}[senza-agent] Resumed session: {args.resume}{RESET}")
        except Exception as e:
            print(f"{YELLOW}[senza-agent] Resume failed: {e}{RESET}")

    try:
        if task:
            # Single task mode.
            _run_single_task(harness, task)
            if nostop:
                # After completion, drop into interactive REPL.
                _interactive_repl(harness)
        elif webserver is not None:
            # Web mode: keep the process alive for the dashboard.
            print(f"{BLUE}[senza-agent] Web mode — press Ctrl+C to exit.{RESET}")
            import time as _time
            try:
                while True:
                    _time.sleep(1)
            except KeyboardInterrupt:
                print(f"\n{YELLOW}Shutting down.{RESET}")
        else:
            # No task → interactive REPL.
            _interactive_repl(harness)
    finally:
        # 14. Print cost.
        _print_cost(harness)
        # Shut down inspector if we started it.
        if inspector is not None:
            stop = getattr(inspector, "stop", None) or getattr(inspector, "shutdown", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass
        if hasattr(harness, "shutdown"):
            try:
                harness.shutdown()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
