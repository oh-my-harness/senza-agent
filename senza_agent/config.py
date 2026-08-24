"""Configuration loading for senza-agent.

Reads ``~/.senza-agent/config.json`` then applies environment variable overrides.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


@dataclass
class WebConfig:
    """Web search/fetch tool configuration."""

    provider: str = "duckduckgo"
    base_url: str = ""
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
    web: WebConfig = field(default_factory=WebConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)


def _home() -> Path:
    return Path(os.environ.get("HOME", "~")).expanduser()


def _config_dir() -> Path:
    return _home() / ".senza-agent"


def _config_file() -> Path:
    return _config_dir() / "config.json"


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

    web_data = data.get("web")
    if isinstance(web_data, dict):
        if "provider" in web_data:
            cfg.web.provider = web_data["provider"]
        if "base_url" in web_data:
            cfg.web.base_url = web_data["base_url"]
        if "api_key" in web_data:
            cfg.web.api_key = web_data["api_key"]
        if "max_results" in web_data:
            cfg.web.max_results = web_data["max_results"]
        if "fetch_timeout_secs" in web_data:
            cfg.web.fetch_timeout_secs = web_data["fetch_timeout_secs"]
        if "max_fetch_chars" in web_data:
            cfg.web.max_fetch_chars = web_data["max_fetch_chars"]

    behavior_data = data.get("behavior")
    if isinstance(behavior_data, dict):
        if "advisor_interval" in behavior_data:
            cfg.behavior.advisor_interval = behavior_data["advisor_interval"]
        if "advisor_model" in behavior_data:
            cfg.behavior.advisor_model = behavior_data["advisor_model"]
        if "budget_limit" in behavior_data:
            cfg.behavior.budget_limit = behavior_data["budget_limit"]
        if "wrapup_turns" in behavior_data:
            cfg.behavior.wrapup_turns = behavior_data["wrapup_turns"]


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

    env_api_base = (
        os.environ.get("SENZA_AGENT_API_BASE")
        or os.environ.get("OPENAI_API_BASE")
        or os.environ.get("OPENAI_BASE_URL")
    )
    if env_api_base:
        cfg.api_base = env_api_base

    env_working_dir = os.environ.get("SENZA_AGENT_WORKING_DIR")
    if env_working_dir:
        cfg.working_dir = env_working_dir


def _apply_derived(cfg: Config) -> None:
    """Fill in derived paths if not already set."""
    cdir = _config_dir()
    if not cfg.sessions_dir:
        cfg.sessions_dir = str(cdir / "sessions")
    if not cfg.audit_path:
        cfg.audit_path = str(cdir / "audit.jsonl")
    if not cfg.skills_dir:
        candidate = Path("senza-agent") / "SKILLS"
        if candidate.is_dir():
            cfg.skills_dir = str(candidate.resolve())


def load_config() -> Config:
    """Load configuration from ``~/.senza-agent/config.json`` then apply env overrides.

    Derived paths are filled last:
      - ``sessions_dir`` → ``~/.senza-agent/sessions/``
      - ``audit_path`` → ``~/.senza-agent/audit.jsonl``
      - ``skills_dir`` → ``senza-agent/SKILLS/`` if it exists
    """
    cfg = Config()
    _apply_file(cfg, _load_file())
    _apply_env(cfg)
    _apply_derived(cfg)
    return cfg
