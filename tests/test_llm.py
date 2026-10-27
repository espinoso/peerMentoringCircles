import anthropic
import pytest

from matching.llm import LLMClient, LLMError


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeAnthropic:
    last_kwargs: dict = {}

    def __init__(self, api_key=None):
        self.messages = self

    def create(self, **kwargs):
        _FakeAnthropic.last_kwargs = kwargs

        class _Resp:
            content = [_Block('{"ok": '), _Block("true}")]

        return _Resp()


def test_complete_calls_anthropic_without_temperature(monkeypatch):
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    out = LLMClient("claude-opus-5-5", "k").complete("sys", "usr")
    kw = _FakeAnthropic.last_kwargs
    assert out == '{"ok": true}'
    assert "temperature" not in kw
    assert kw["model"] == "claude-opus-5-5"
    assert kw["max_tokens"] == 16000
    assert kw["system"].startswith("sys")
    assert kw["messages"] == [{"role": "user", "content": "usr"}]


def test_empty_key_raises_llmerror():
    with pytest.raises(LLMError):
        LLMClient("claude-opus-5-5", "")


def test_request_failure_wrapped(monkeypatch):
    class Boom(_FakeAnthropic):
        def create(self, **kwargs):
            raise RuntimeError("400 bad model")

    monkeypatch.setattr(anthropic, "Anthropic", Boom)
    with pytest.raises(LLMError, match="400 bad model"):
        LLMClient("m", "k").complete("s", "u")
