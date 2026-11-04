"""Configuration loading for senza-agent.

Reads ``~/.senza-agent/config.json`` then applies environment variable overrides.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


@dataclass
class WebConfig:
    """Web search/fetch tool configuration."""

    # duckduckgo is unreachable from CN networks (lite/html endpoints blocked);
    # bing needs no API key and is reachable from both CN and international egress.
    provider: str = "bing"
    base_url: str = "https://www.bing.com/search"
    api_key: Optional[str] = None
    max_results: int = 5
    fetch_timeout_secs: int = 12
    max_fetch_chars: int = 12000


@dataclass
class BehaviorConfig:
    """Agent behavior tuning parameters."""

    advisor_interval: int = 15
    advisor_model: Optional[str] = None
    budget_limit: float = 10.0
    wrapup_turns: int = 2
    thinking_level: Optional[str] = None  # "off", "minimal", "low", "medium", "high"


@dataclass
class CompactionConfig:
    """Deep compression (auto-compaction) tuning parameters.

    All fields default to None, meaning the runtime's built-in defaults
    (context_window=200_000, reserve_tokens=16_384, keep_recent=20_000)
    are used.
    """

    context_window: Optional[int] = None
    max_tokens: Optional[int] = None
    reserve_tokens: Optional[int] = None
    keep_recent_tokens: Optional[int] = None
    model: Optional[str] = None
    model_context_window: Optional[int] = None
    model_max_tokens: Optional[int] = None


@dataclass
class Config:
    """Top-level senza-agent configuration."""

    model: str = "gpt-4o"
    api_key: str = ""
    api_base: str = ""
    working_dir: str = ""
    sessions_dir: str = ""
    audit_path: str = ""
    skills_dir: str = ""
    knowledge_dir: Optional[str] = None
    memory_enabled: bool = False
    mcp_config: Optional[str] = None
    spawn_enabled: bool = False
    provider_type: str = "openai"
    web: WebConfig = field(default_factory=WebConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)


def _home() -> Path:
    # Windows uses USERPROFILE; Unix uses HOME.  Check both so the config
    # directory is found correctly regardless of platform or shell (Git Bash
    # sets HOME to a Unix-style path that confuses pathlib).
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or "~"
    return Path(home).expanduser()


def _config_dir() -> Path:
    return _home() / ".senza-agent"


def _config_file() -> Path:
    return _config_dir() / "config.json"


def _settings_file() -> Path:
    """Path to the runtime settings file (key-value pairs from the dashboard)."""
    return _config_dir() / "settings.json"


def _load_file() -> dict[str, Any]:
    path = _config_file()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _apply_file(cfg: Config, data: dict[str, Any]) -> None:
    """Apply values from config file dict onto the Config dataclass in place."""
    if "model" in data:
        cfg.model = data["model"]
    if "api_key" in data:
        cfg.api_key = data["api_key"]
    if "api_base" in data:
        cfg.api_base = data["api_base"]
    if "working_dir" in data:
        cfg.working_dir = data["working_dir"]
    if "sessions_dir" in data:
        cfg.sessions_dir = data["sessions_dir"]
    if "audit_path" in data:
        cfg.audit_path = data["audit_path"]
    if "skills_dir" in data:
        cfg.skills_dir = data["skills_dir"]
    if "knowledge_dir" in data:
        cfg.knowledge_dir = data["knowledge_dir"]
    if "memory_enabled" in data:
        cfg.memory_enabled = bool(data["memory_enabled"])
    if "mcp_config" in data:
        cfg.mcp_config = data["mcp_config"]
    if "spawn_enabled" in data:
        cfg.spawn_enabled = bool(data["spawn_enabled"])
    if "provider_type" in data:
        cfg.provider_type = str(data["provider_type"])

    def _to_int(v: Any, fallback: Optional[int]) -> Optional[int]:
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return fallback

    def _to_float(v: Any, fallback: Optional[float]) -> Optional[float]:
        try:
            return float(str(v).strip())
        except (TypeError, ValueError):
            return fallback

    compaction_data = data.get("compaction")
    if isinstance(compaction_data, dict):
        c = cfg.compaction
        c.context_window = _to_int(compaction_data.get("context_window", c.context_window), c.context_window)
        c.max_tokens = _to_int(compaction_data.get("max_tokens", c.max_tokens), c.max_tokens)
        c.reserve_tokens = _to_int(compaction_data.get("reserve_tokens", c.reserve_tokens), c.reserve_tokens)
        c.keep_recent_tokens = _to_int(compaction_data.get("keep_recent_tokens", c.keep_recent_tokens), c.keep_recent_tokens)
        if "model" in compaction_data:
            c.model = compaction_data["model"]
        c.model_context_window = _to_int(compaction_data.get("model_context_window", c.model_context_window), c.model_context_window)
        c.model_max_tokens = _to_int(compaction_data.get("model_max_tokens", c.model_max_tokens), c.model_max_tokens)

    web_data = data.get("web")
    if isinstance(web_data, dict):
        if "provider" in web_data:
            cfg.web.provider = str(web_data["provider"])
        if "base_url" in web_data:
            cfg.web.base_url = str(web_data["base_url"])
        if "api_key" in web_data:
            cfg.web.api_key = str(web_data["api_key"])
        if "max_results" in web_data:
            cfg.web.max_results = _to_int(web_data["max_results"], cfg.web.max_results)
        if "fetch_timeout_secs" in web_data:
            cfg.web.fetch_timeout_secs = _to_int(web_data["fetch_timeout_secs"], cfg.web.fetch_timeout_secs)
        if "max_fetch_chars" in web_data:
            cfg.web.max_fetch_chars = _to_int(web_data["max_fetch_chars"], cfg.web.max_fetch_chars)

    behavior_data = data.get("behavior")
    if isinstance(behavior_data, dict):
        if "advisor_interval" in behavior_data:
            cfg.behavior.advisor_interval = _to_int(behavior_data["advisor_interval"], cfg.behavior.advisor_interval)
        if "advisor_model" in behavior_data:
            cfg.behavior.advisor_model = behavior_data["advisor_model"]
        if "budget_limit" in behavior_data:
            cfg.behavior.budget_limit = _to_float(behavior_data["budget_limit"], cfg.behavior.budget_limit)
        if "wrapup_turns" in behavior_data:
            cfg.behavior.wrapup_turns = _to_int(behavior_data["wrapup_turns"], cfg.behavior.wrapup_turns)
        if "thinking_level" in behavior_data:
            cfg.behavior.thinking_level = behavior_data["thinking_level"]


def _apply_env(cfg: Config) -> None:
    """Apply environment variable overrides (take precedence over file).

    Falls back to standard ``OPENAI_*`` env vars if ``SENZA_AGENT_*``
    variants are not set, so users can ``export OPENAI_API_KEY=...``
    without learning the senza-agent-specific names.
    """
    env_model = os.environ.get("SENZA_AGENT_MODEL") or os.environ.get("OPENAI_MODEL")
    if env_model:
        cfg.model = env_model

    env_api_key = os.environ.get("SENZA_AGENT_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if env_api_key:
        cfg.api_key = env_api_key

    # OPENAI_BASE_URL before OPENAI_API_BASE: the dashboard reads and writes
    # OPENAI_BASE_URL, and ensure_env_defaults() builds the probe's slot URL
    # from it first.  Resolving them the other way round made the probe
    # validate one host while the harness talked to another, which surfaces as
    # "llm provider error: unauthorized" with perfectly good credentials.
    env_api_base = (
        os.environ.get("SENZA_AGENT_API_BASE")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
    )
    if env_api_base:
        cfg.api_base = env_api_base

    env_working_dir = os.environ.get("SENZA_AGENT_WORKING_DIR")
    if env_working_dir:
        cfg.working_dir = env_working_dir

    env_provider_type = os.environ.get("SENZA_AGENT_PROVIDER_TYPE")
    if env_provider_type:
        cfg.provider_type = env_provider_type

    env_spawn = os.environ.get("SENZA_AGENT_SPAWN_ENABLED")
    if env_spawn:
        cfg.spawn_enabled = env_spawn.lower() in ("1", "true", "yes")
    env_thinking = os.environ.get("SENZA_AGENT_THINKING_LEVEL")
    if env_thinking:
        cfg.behavior.thinking_level = env_thinking
    env_advisor_interval = os.environ.get("SENZA_AGENT_ADVISOR_INTERVAL")
    if env_advisor_interval:
        try:
            cfg.behavior.advisor_interval = int(env_advisor_interval)
        except ValueError:
            pass
    env_wrapup = os.environ.get("SENZA_AGENT_WRAPUP_TURNS")
    if env_wrapup:
        try:
            cfg.behavior.wrapup_turns = int(env_wrapup)
        except ValueError:
            pass
    env_budget = os.environ.get("SENZA_AGENT_BUDGET_LIMIT")
    if env_budget:
        try:
            cfg.behavior.budget_limit = float(env_budget)
        except ValueError:
            pass


def _apply_derived(cfg: Config) -> None:
    """Fill in derived paths if not already set."""
    cdir = _config_dir()
    if not cfg.sessions_dir:
        cfg.sessions_dir = str(cdir / "sessions")
    if not cfg.audit_path:
        cfg.audit_path = str(cdir / "audit.jsonl")
    if not cfg.skills_dir:
        # Candidates, in priority order:
        #   1. $SENZA_AGENT_DIR/SKILLS   (set by desktop/main.js and CLI wrappers)
        #   2. <repo root>/SKILLS        (resolved relative to this package)
        #   3. ./senza-agent/SKILLS      (legacy: relative to cwd when run from ~)
        candidates = []
        agent_dir = os.environ.get("SENZA_AGENT_DIR")
        if agent_dir:
            candidates.append(Path(agent_dir) / "SKILLS")
        candidates.append(Path(__file__).resolve().parent.parent / "SKILLS")
        candidates.append(Path("senza-agent") / "SKILLS")
        for candidate in candidates:
            if candidate.is_dir():
                cfg.skills_dir = str(candidate.resolve())
                break

def load_config() -> Config:
    """Load configuration from ``~/.senza-agent/config.json`` then apply env overrides.

    Derived paths are filled last:
      - ``sessions_dir`` → ``~/.senza-agent/sessions/``
      - ``audit_path`` → ``~/.senza-agent/audit.jsonl``
      - ``skills_dir`` → ``$SENZA_AGENT_DIR/SKILLS`` or ``<repo root>/SKILLS/`` if it exists

    ``settings.json`` (the dashboard's write path) is hydrated into the
    environment here, not only in ``cli.main()``: every entry point — the
    webserver's ``rebuild_harness()``, tool helpers, ``scripts/diag_llm_auth.py``
    — must see the same credentials.  Previously a caller that invoked
    ``load_config()`` directly (without ``load_settings_into_env()`` first)
    reported an empty api_key/api_base while the app used the dashboard values.
    Existing non-empty env vars still win (see ``load_settings_into_env``).
    """
    load_settings_into_env()
    cfg = Config()
    _apply_file(cfg, _load_file())
    _apply_env(cfg)
    _apply_derived(cfg)
    return cfg


def create_provider(cfg: Config):
    """Create a Senza LLM Provider from the given config.

    Supports ``openai`` (default) and ``anthropic`` provider types.
    The provider type can be set via ``config.provider_type`` or the
    ``SENZA_AGENT_PROVIDER_TYPE`` env var.

    Returns a ``senza.Provider`` instance.
    """
    import senza

    api_key = cfg.api_key or os.environ.get("OPENAI_API_KEY", "")
    api_base = cfg.api_base or os.environ.get("OPENAI_API_BASE", "")

    if cfg.provider_type == "anthropic":
        return senza.providers.anthropic(
            api_key=api_key,
            base_url=api_base if api_base else None,
        )
    # default: openai-compatible
    return senza.providers.openai(
        api_key=api_key,
        base_url=api_base if api_base else None,
    )


def _fingerprint(value: str | None) -> str:
    """Secret-safe key fingerprint: enough to compare two keys, not to leak one."""
    if value is None:
        return "<unset>"
    if value == "":
        return "<empty>"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    shown = f"{value[:6]}…{value[-4:]}" if len(value) > 12 else value
    return f"len={len(value)} sha256={digest} {shown}"


_CRED_ENV = (
    "SENZA_AGENT_API_KEY", "OPENAI_API_KEY", "OPENAI_OPENAI_API_KEY",
    "BACKUP_OPENAI_API_KEY", "BACKUP2_OPENAI_API_KEY",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "SENZA_AGENT_API_BASE",
    "OPENAI_OPENAI_BASE_URL", "PREFERRED_API", "SENZA_AGENT_PROVIDER_TYPE",
)


def credential_diag() -> str:
    """One-line, secret-safe description of what the next LLM call will use.

    Appended to provider errors (``provider error: unauthorized``) so a 401 can
    be traced instead of guessed: it names the effective provider / base /
    model and the key fingerprint, and reports each credential env var as
    ``set`` / ``<empty>`` / ``<unset>``.
    """
    try:
        cfg = load_config()
        head = (
            f"provider={cfg.provider_type} model={cfg.model} "
            f"base={cfg.api_base or '<empty>'} key={_fingerprint(cfg.api_key)}"
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        head = f"<config load failed: {exc}>"
    states = []
    for name in _CRED_ENV:
        value = os.environ.get(name)
        if name not in os.environ:
            states.append(f"{name}=<unset>")
        elif not value:
            states.append(f"{name}=<empty>")
        else:
            states.append(f"{name}=set")
    override = ""
    dedicated_key = os.environ.get("SENZA_AGENT_API_KEY")
    panel_key = os.environ.get("OPENAI_API_KEY")
    if dedicated_key and panel_key and dedicated_key != panel_key:
        override = (
            " !! SENZA_AGENT_API_KEY overrides OPENAI_API_KEY"
            f" (used key={_fingerprint(dedicated_key)},"
            f" panel key={_fingerprint(panel_key)})"
        )
    return "[diag] " + head + " | " + " ".join(states) + override


def diag_suffix_for(message: str) -> str:
    """Return ``"\\n[diag] …"`` when *message* looks like an auth failure."""
    low = (message or "").lower()
    if "unauthorized" not in low and "401" not in low:
        return ""
    try:
        return "\n" + credential_diag()
    except Exception:  # noqa: BLE001
        return ""


def diag_suffix(exc: BaseException) -> str:
    """Return ``"\\n[diag] …"`` for auth-ish failures, else an empty string."""
    return diag_suffix_for(str(exc))



# ── Runtime settings file (~/.senza-agent/settings.json) ───────────────────

def load_settings_into_env() -> None:
    """Load ``~/.senza-agent/settings.json`` into ``os.environ``.

    Called at startup so the dashboard-saved settings take effect without
    a separate ``.env`` file.  Existing *non-empty* env vars win; an env var
    that is present but empty (``OPENAI_API_KEY=`` in a .env, or a blank
    Windows user variable) is treated as unset, otherwise it would silently
    shadow the dashboard value and send requests out with no credentials.
    """
    path = _settings_file()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    for key, val in data.items():
        if isinstance(val, dict):
            # Only ``behavior`` maps 1:1 to ``SENZA_AGENT_*`` env vars — that
            # is the set ``_apply_env()`` actually reads.  Flattening every
            # section collided with the credential namespace: ``web.api_key``
            # (the web-search token) became ``SENZA_AGENT_API_KEY``, which
            # outranks ``OPENAI_API_KEY`` in ``_apply_env()``, so the LLM was
            # called with the web-search token → "llm provider error:
            # unauthorized".  ``web`` / ``compaction`` reach ``load_config()``
            # through the config.json mirror instead.
            if key != "behavior":
                continue
            for sub_key, sub_val in val.items():
                ssub = str(sub_val).strip() if sub_val is not None else ""
                env_key = f"SENZA_AGENT_{sub_key.upper()}"
                if ssub and not os.environ.get(env_key):
                    os.environ[env_key] = ssub
            continue
        if isinstance(val, str) and val and not os.environ.get(key):
            os.environ[key] = val


def _mirror_to_config_file(existing: dict[str, Any]) -> None:
    """Mirror the structured sections of settings.json into config.json.

    settings.json is the single write path from the dashboard; config.json is
    what ``load_config()`` reads at startup.  Keeping the ``web`` /
    ``compaction`` sections mirrored (not merged — a full replace with the
    latest saved values) keeps the two files semantically consistent without
    changing the startup sequence.
    """
    cfg_path = _config_file()
    cfg: dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    for section in ("web", "compaction"):
        data = existing.get(section)
        if isinstance(data, dict) and data:
            cfg[section] = dict(data)
        else:
            cfg.pop(section, None)
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # settings.json is already persisted; mirroring is best-effort


def save_settings(data: dict[str, Any]) -> dict[str, str]:
    """Persist settings to ``~/.senza-agent/settings.json`` and update ``os.environ``.

    Only non-empty string values are kept; empty values remove the key
    (matching the frontend's "empty = delete" convention).  Nested dicts
    (``behavior`` / ``compaction`` / ``web``) merge sub-key-wise.  After
    writing, the ``web`` / ``compaction`` sections are mirrored into
    ``config.json`` so ``load_config()`` picks them up on the next start
    (and on ``rebuild_harness()``).
    Returns the new settings dict as it was written.
    """
    # Merge with existing file contents so partial updates work.
    path = _settings_file()
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    _API_KEY_SUFFIX = "_API_KEY"
    for key, val in data.items():
        # Handle nested dicts (e.g. behavior, compaction, web) — merge into
        # existing sub-objects
        if isinstance(val, dict):
            sub = existing.setdefault(key, {})
            if not isinstance(sub, dict):
                sub = {}
                existing[key] = sub
            for sub_key, sub_val in val.items():
                ssub = str(sub_val).strip() if sub_val is not None else ""
                if ssub:
                    sub[sub_key] = ssub
                else:
                    sub.pop(sub_key, None)
            # Also set env vars for known behavior keys
            if key == "behavior":
                for sub_key, sub_val in sub.items():
                    os.environ[f"SENZA_AGENT_{sub_key.upper()}"] = sub_val
            continue

        sval = str(val).strip() if val is not None else ""
        if sval:
            existing[key] = sval
            os.environ[key] = sval
        elif key.endswith(_API_KEY_SUFFIX):
            # Empty API key = user didn't change it (frontend sends empty
            # because the key field is blanked for security). Keep existing.
            pass
        else:
            # Empty value → remove key
            existing.pop(key, None)
            os.environ.pop(key, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    _mirror_to_config_file(existing)
    return existing
