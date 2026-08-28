"""Tests for config loading."""
from __future__ import annotations

import json
from pathlib import Path

from senza_agent.config import Config, load_config, WebConfig, BehaviorConfig

_OPENAI_ENV_KEYS = (
    "OPENAI_MODEL", "OPENAI_API_KEY", "OPENAI_API_BASE",
    "OPENAI_BASE_URL", "SENZA_AGENT_MODEL", "SENZA_AGENT_API_KEY",
    "SENZA_AGENT_API_BASE",
)


def test_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in _OPENAI_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.model == "gpt-4o"
    assert cfg.api_key == ""
    assert cfg.web.provider == "bing"
    assert cfg.web.base_url == "https://www.bing.com/search"
    assert cfg.behavior.advisor_interval == 15


def test_config_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in _OPENAI_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SENZA_AGENT_MODEL", "deepseek-chat")
    monkeypatch.setenv("SENZA_AGENT_API_KEY", "sk-test")
    cfg = load_config()
    assert cfg.model == "deepseek-chat"
    assert cfg.api_key == "sk-test"


def test_config_from_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in _OPENAI_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    config_dir = tmp_path / ".senza-agent"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "model": "claude-3-5-sonnet",
        "api_key": "sk-from-file",
        "web": {"provider": "brave", "api_key": "brave-key"},
        "behavior": {"advisor_interval": 5},
    }))
    cfg = load_config()
    assert cfg.model == "claude-3-5-sonnet"
    assert cfg.web.provider == "brave"
    assert cfg.web.api_key == "brave-key"
    assert cfg.behavior.advisor_interval == 5


# ── Dashboard full-settings storage (settings.json ⇄ config.json) ──────────

_SETTINGS_ENV_KEYS = (
    "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
    "SENZA_AGENT_THINKING_LEVEL", "SENZA_AGENT_ADVISOR_INTERVAL",
)


def test_save_settings_mirrors_web_compaction_to_config(tmp_path, monkeypatch):
    """save_settings persists structured sections and mirrors them into config.json."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in _OPENAI_ENV_KEYS + _SETTINGS_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    from senza_agent.config import save_settings

    written = save_settings({
        "web": {"provider": "tavily", "api_key": "tvk", "max_results": "8",
                "max_fetch_chars": "9000", "base_url": ""},
        "compaction": {"context_window": "128000", "reserve_tokens": "8192"},
        "behavior": {"thinking_level": "high", "advisor_interval": "20"},
        "OPENAI_BASE_URL": "http://x.test/v1",
    })
    assert written["web"]["provider"] == "tavily"
    # web.api_key survives; empty sub-key (base_url) is dropped
    assert "base_url" not in written["web"]

    settings = json.loads((tmp_path / ".senza-agent" / "settings.json").read_text())
    assert settings["web"] == {"provider": "tavily", "api_key": "tvk",
                               "max_results": "8", "max_fetch_chars": "9000"}
    cfg_file = json.loads((tmp_path / ".senza-agent" / "config.json").read_text())
    # mirror contains exactly the structured sections, not env keys
    assert cfg_file["web"]["provider"] == "tavily"
    assert cfg_file["compaction"] == {"context_window": "128000", "reserve_tokens": "8192"}
    assert "OPENAI_BASE_URL" not in cfg_file


def test_mirrored_sections_load_with_coercion(tmp_path, monkeypatch):
    """load_config() reads the mirrored sections and coerces numeric strings."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in _OPENAI_ENV_KEYS + _SETTINGS_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    from senza_agent.config import save_settings

    save_settings({
        "web": {"provider": "tavily", "max_results": "8", "max_fetch_chars": "9000"},
        "compaction": {"context_window": "128000"},
        "behavior": {"advisor_interval": "20", "budget_limit": "12.5"},
    })
    cfg = load_config()
    assert cfg.web.provider == "tavily"
    assert cfg.web.max_results == 8
    assert cfg.web.max_fetch_chars == 9000
    assert cfg.compaction.context_window == 128000
    assert cfg.compaction.max_tokens is None  # unset → runtime default
    assert cfg.behavior.advisor_interval == 20
    assert cfg.behavior.budget_limit == 12.5


def test_load_settings_into_env_expands_nested(tmp_path, monkeypatch):
    """Startup: nested settings.json sections expand to SENZA_AGENT_* env vars."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in _OPENAI_ENV_KEYS + _SETTINGS_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    config_dir = tmp_path / ".senza-agent"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(json.dumps({
        "OPENAI_BASE_URL": "http://s.test/v1",
        "behavior": {"thinking_level": "low", "advisor_interval": "25"},
    }))
    from senza_agent.config import load_settings_into_env
    load_settings_into_env()
    import os
    assert os.environ["OPENAI_BASE_URL"] == "http://s.test/v1"
    assert os.environ["SENZA_AGENT_THINKING_LEVEL"] == "low"
    assert os.environ["SENZA_AGENT_ADVISOR_INTERVAL"] == "25"
    cfg = load_config()
    assert cfg.behavior.advisor_interval == 25
    assert cfg.behavior.thinking_level == "low"


def test_save_then_reload_roundtrip(tmp_path, monkeypatch):
    """Save → simulated restart (load_settings_into_env + load_config) keeps values."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in _OPENAI_ENV_KEYS + _SETTINGS_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    from senza_agent.config import save_settings, load_settings_into_env

    save_settings({
        "web": {"provider": "searxng", "base_url": "http://lx:8080/search"},
        "compaction": {"reserve_tokens": "4096"},
    })
    load_settings_into_env()
    cfg = load_config()
    assert cfg.web.provider == "searxng"
    assert cfg.web.base_url == "http://lx:8080/search"
    assert cfg.compaction.reserve_tokens == 4096
