"""Tests for the system prompt builder."""
from __future__ import annotations

from senza_agent.config import Config
from senza_agent.system_prompt import build_system_prompt


def test_prompt_contains_completion_instruction():
    prompt = build_system_prompt(Config())
    assert "submit_completion_report" in prompt


def test_prompt_lists_tools():
    prompt = build_system_prompt(Config())
    assert "web_search" in prompt
    assert "code_exec" in prompt
    assert "todo_write" in prompt
