"""Provider-agnostic LLM client.

Two backends behind one interface, selected by config ("openai" | "anthropic").
Both take a system + user prompt and return raw text; the caller parses JSON.
Swapping providers is a one-line config change plus the matching API key.
"""
from __future__ import annotations

import json
import re


class LLMError(Exception):
    """Raised when the model call fails or returns unusable output."""


class LLMClient:
    def __init__(self, provider: str, model: str, api_key: str):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        if not api_key:
            raise LLMError(
                f"No API key provided for '{provider}'. Set it in the app's "
                "secrets (or environment) before generating groups."
            )

    def complete(self, system: str, user: str) -> str:
        if self.provider == "openai":
            return self._openai(system, user)
        if self.provider == "anthropic":
            return self._anthropic(system, user)
        raise LLMError(f"Unknown provider '{self.provider}'.")

    def _openai(self, system: str, user: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise LLMError("The 'openai' package is not installed.") from e
        client = OpenAI(api_key=self.api_key)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        # Reasoning models (o-series, gpt-5+) reject a custom temperature; the
        # gpt-4 family accepts it and benefits from a low value.
        if not self._is_reasoning_model():
            kwargs["temperature"] = 0.2

        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:  # noqa: BLE001
            # Retry once stripping params some models don't support.
            msg = str(e).lower()
            if "temperature" in msg:
                kwargs.pop("temperature", None)
            if "response_format" in msg:
                kwargs.pop("response_format", None)
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as e2:  # noqa: BLE001
                raise LLMError(f"OpenAI request failed: {e2}") from e2
        return resp.choices[0].message.content or ""

    def _is_reasoning_model(self) -> bool:
        m = self.model.lower()
        return m.startswith(("o1", "o3", "o4", "gpt-5"))

    def _anthropic(self, system: str, user: str) -> str:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise LLMError("The 'anthropic' package is not installed.") from e
        client = anthropic.Anthropic(api_key=self.api_key)
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=8000,
                temperature=0.2,
                system=system + "\n\nRespond with a single valid JSON object and nothing else.",
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Anthropic request failed: {e}") from e
        return "".join(block.text for block in resp.content if block.type == "text")


def parse_json_response(text: str) -> dict:
    """Best-effort extraction of a JSON object from a model response."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Strip ```json fences or surrounding prose, grab the outermost { ... }.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise LLMError(f"Model did not return valid JSON: {e}") from e
    raise LLMError("Model response contained no JSON object.")
