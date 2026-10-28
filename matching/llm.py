"""Claude LLM client: takes a system + user prompt and returns raw text.

The caller parses the JSON (see parse_json_response).
"""
from __future__ import annotations

import json
import re


class LLMError(Exception):
    """Raised when the model call fails or returns unusable output."""


class LLMClient:
    def __init__(self, model: str, api_key: str):
        self.model = model
        self.api_key = api_key
        if not api_key:
            raise LLMError(
                "No Anthropic API key provided. Set ANTHROPIC_API_KEY in the app's "
                "secrets (or environment) before generating groups."
            )

    def complete(self, system: str, user: str) -> str:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise LLMError("The 'anthropic' package is not installed.") from e
        client = anthropic.Anthropic(api_key=self.api_key)
        try:
            # No temperature: Opus 5.5 rejects a custom value with a 400.
            resp = client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system + "\n\nRespond with a single valid JSON object and nothing else.",
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:
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
