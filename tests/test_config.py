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
