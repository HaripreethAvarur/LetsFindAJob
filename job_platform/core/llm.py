"""Pluggable LLM providers (Anthropic / Gemini) with reliable JSON output.

Both providers expose:
  complete_json(system, user, schema)  -> validated dict (retries on bad JSON)
  complete_text(system, user)          -> str (long-form, used by resume tailor)

A malformed model response never crashes the pipeline: JSON is validated
against the expected keys and re-requested (with the parse error appended)
up to MAX_JSON_RETRIES times before raising LLMError, which callers catch
per-listing.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Protocol

from .config import env

log = logging.getLogger("llm")

MAX_JSON_RETRIES = 3


class LLMError(Exception):
    pass


class LLMProvider(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]: ...

    def complete_text(self, system: str, user: str, max_tokens: int = 16000) -> str: ...


def _extract_json(text: str) -> dict[str, Any]:
    """Parse JSON from a model response, tolerating code fences / preamble."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in response")
        text = text[start : end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("top-level JSON is not an object")
    return parsed


def _validate_keys(data: dict[str, Any], schema: dict[str, Any]) -> None:
    missing = [k for k in schema.get("required", []) if k not in data]
    if missing:
        raise ValueError(f"missing required keys: {missing}")


class AnthropicProvider:
    """Uses the Claude API with structured outputs for guaranteed-schema JSON."""

    name = "anthropic"

    def __init__(self, model: Optional[str] = None) -> None:
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = model or env("ANTHROPIC_MODEL", "claude-opus-4-8")

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception = LLMError("unreachable")
        prompt = user
        for attempt in range(MAX_JSON_RETRIES):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    system=system,
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                    messages=[{"role": "user", "content": prompt}],
                )
                if response.stop_reason == "refusal":
                    raise LLMError("model refused the request")
                text = next(b.text for b in response.content if b.type == "text")
                data = _extract_json(text)
                _validate_keys(data, schema)
                return data
            except Exception as exc:  # noqa: BLE001 — retried, then surfaced as LLMError
                last_error = exc
                log.warning("anthropic JSON attempt %d failed: %s", attempt + 1, exc)
                prompt = (
                    f"{user}\n\nYour previous response could not be parsed "
                    f"({exc}). Respond with ONLY a valid JSON object matching the schema."
                )
        raise LLMError(f"anthropic JSON failed after {MAX_JSON_RETRIES} attempts: {last_error}")

    def complete_text(self, system: str, user: str, max_tokens: int = 16000) -> str:
        with self.client.messages.stream(
            model=self.model,
            max_tokens=max(max_tokens, 16000),
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            raise LLMError("model refused the request")
        return "".join(b.text for b in message.content if b.type == "text")


class GeminiProvider:
    name = "gemini"

    def __init__(self, model: Optional[str] = None) -> None:
        from google import genai

        self.client = genai.Client(api_key=env("GEMINI_API_KEY"))
        self.model = model or env("GEMINI_MODEL", "gemini-2.5-pro")

    def _generate(self, system: str, user: str, json_mode: bool) -> str:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json" if json_mode else "text/plain",
        )
        response = self.client.models.generate_content(
            model=self.model, contents=user, config=config
        )
        return response.text or ""

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception = LLMError("unreachable")
        prompt = (
            f"{user}\n\nRespond with ONLY a JSON object matching this schema:\n"
            f"{json.dumps(schema, indent=2)}"
        )
        for attempt in range(MAX_JSON_RETRIES):
            try:
                data = _extract_json(self._generate(system, prompt, json_mode=True))
                _validate_keys(data, schema)
                return data
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("gemini JSON attempt %d failed: %s", attempt + 1, exc)
                prompt += f"\n\nPrevious response was invalid ({exc}). JSON only."
        raise LLMError(f"gemini JSON failed after {MAX_JSON_RETRIES} attempts: {last_error}")

    def complete_text(self, system: str, user: str, max_tokens: int = 16000) -> str:
        return self._generate(system, user, json_mode=False)


def get_provider() -> LLMProvider:
    provider = env("LLM_PROVIDER", "anthropic").lower()
    if provider == "gemini":
        return GeminiProvider()
    return AnthropicProvider()
