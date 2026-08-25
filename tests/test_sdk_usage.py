"""Tests for Senza SDK usage patterns in standard tools."""
import os
import sys
from unittest.mock import MagicMock, patch


def _make_fake_senza(captured):
    """Build a fake senza module that captures harness construction."""

    class FakeHarness:
        def chat(self, text, timeout_ms=30000):
            captured["chat_called"] = True
            captured["text"] = text
            return captured.get("response", "fake response")

    class FakeSenza:
        pass

    FakeSenza.providers = type("providers", (), {
        "openai": staticmethod(lambda api_key, base_url=None, **kw: MagicMock()),
    })
    FakeSenza.HarnessBuilder = type("HarnessBuilder", (), {
        "__init__": lambda self, model: captured.__setitem__("model", model),
        "provider": lambda self, pattern, provider: self,
        "system_prompt": lambda self, prompt: self,
        "build": lambda self: FakeHarness(),
    })
    return FakeSenza


def test_tool_analyze_content_uses_harness_not_provider():
    """analyze_content must build a HarnessBuilder, not call provider.chat() directly."""
    from senza_agent.tools import standard

    captured = {}
    captured["response"] = "analysis result"
    FakeSenza = _make_fake_senza(captured)

    env = {
        "SENZA_AGENT_API_KEY": "test-key",
        "SENZA_AGENT_MODEL": "gpt-4o",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch.dict(sys.modules, {"senza": FakeSenza}):
            result = standard.tool_analyze_content(
                sources=[{"text": "hello world", "label": "test"}],
                question="what is this?",
            )

    assert result["status"] == "ok"
    assert "analysis result" in result["output"]
    assert captured.get("chat_called") is True


def test_tool_consult_advisor_uses_harness_not_provider():
    """consult_advisor must build a HarnessBuilder, not call provider.chat() directly."""
    from senza_agent.tools import standard

    captured = {}
    captured["response"] = "advisor says: do X"
    FakeSenza = _make_fake_senza(captured)

    env = {
        "ADVISOR1_OPENAI_BASE_URL": "http://localhost:8080",
        "ADVISOR1_OPENAI_API_KEY": "test-key",
        "ADVISOR1_OPENAI_MODEL": "gpt-4o",
    }
    with patch.dict(os.environ, env, clear=False):
        with patch.dict(sys.modules, {"senza": FakeSenza}):
            result = standard.tool_consult_advisor(question="what should I do?")

    assert result["status"] == "ok"
    assert "advisor says" in result["output"]
    assert captured.get("chat_called") is True
