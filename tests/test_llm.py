import anthropic
import pytest

from matching.llm import LLMClient, LLMError


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FinalMessage:
    def __init__(self, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [_Block('{"ok": '), _Block("true}")]


class _Stream:
    def __init__(self, stop_reason="end_turn"):
        self._stop_reason = stop_reason

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return _FinalMessage(self._stop_reason)


class _FakeAnthropic:
    last_kwargs: dict = {}
    stop_reason = "end_turn"

    def __init__(self, api_key=None):
        self.messages = self

    def stream(self, **kwargs):
        _FakeAnthropic.last_kwargs = kwargs
        return _Stream(self.stop_reason)


def test_complete_streams_without_temperature(monkeypatch):
    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    out = LLMClient("claude-opus-5-5", "k").complete("sys", "usr")
    kw = _FakeAnthropic.last_kwargs
    assert out == '{"ok": true}'
    assert "temperature" not in kw
    assert kw["model"] == "claude-opus-5-5"
    assert kw["max_tokens"] == 32000  # room for Opus 5.5 thinking + the JSON
    assert kw["system"].startswith("sys")
    assert kw["messages"] == [{"role": "user", "content": "usr"}]


def test_truncated_response_raises_llmerror(monkeypatch):
    class Truncated(_FakeAnthropic):
        stop_reason = "max_tokens"

    monkeypatch.setattr(anthropic, "Anthropic", Truncated)
    with pytest.raises(LLMError, match="cut off"):
        LLMClient("claude-opus-5-5", "k").complete("sys", "usr")


def test_empty_key_raises_llmerror():
    with pytest.raises(LLMError):
        LLMClient("claude-opus-5-5", "")


def test_request_failure_wrapped(monkeypatch):
    class Boom(_FakeAnthropic):
        def stream(self, **kwargs):
            raise RuntimeError("400 bad model")

    monkeypatch.setattr(anthropic, "Anthropic", Boom)
    with pytest.raises(LLMError, match="400 bad model"):
        LLMClient("m", "k").complete("s", "u")
