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
from typing import Any, Protocol

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

    def __init__(self, model: str | None = None) -> None:
        # Fail fast with a clear message instead of erroring (and retrying)
        # on every single listing at request time.
        if not (env("ANTHROPIC_API_KEY") or env("ANTHROPIC_AUTH_TOKEN")):
            raise LLMError(
                "ANTHROPIC_API_KEY is not set (add it to .env locally, or as a "
                "GitHub Actions repository secret)"
            )
        try:
            import anthropic
        except Exception as exc:  # broken install must not crash the pipeline
            raise LLMError(f"anthropic package unavailable: {exc}") from exc
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

    def __init__(self, model: str | None = None) -> None:
        if not env("GEMINI_API_KEY"):
            raise LLMError(
                "GEMINI_API_KEY is not set (add it to .env locally, or as a "
                "GitHub Actions repository secret)"
            )
        try:
            from google import genai
        except Exception as exc:  # broken install must not crash the pipeline
            raise LLMError(f"google-genai package unavailable: {exc}") from exc
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


class FallbackProvider:
    """Primary provider with automatic failover to a backup.

    When the primary exhausts its own retries (rate limit, quota, outage),
    the same request is retried once on the backup. There is no debate/
    consensus mode by design: the two-pass scorer plus the in-code APPLY
    gate already provide the review structure, and a second opinion per
    listing would double LLM spend for marginal gain.
    """

    def __init__(self, primary: LLMProvider, backup: LLMProvider) -> None:
        self.primary = primary
        self.backup = backup
        self.name = f"{primary.name}->{backup.name}"

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return getattr(self.primary, method)(*args, **kwargs)
        except Exception as exc:
            log.warning(
                "primary provider %s failed (%s) — falling back to %s",
                self.primary.name, exc, self.backup.name,
            )
            return getattr(self.backup, method)(*args, **kwargs)

    def complete_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return self._call("complete_json", system, user, schema)

    def complete_text(self, system: str, user: str, max_tokens: int = 16000) -> str:
        return self._call("complete_text", system, user, max_tokens=max_tokens)


def get_provider() -> LLMProvider:
    """Build the provider chain from whichever API keys are configured.

    LLM_PROVIDER picks the primary ("anthropic" or "gemini"). If the other
    provider's key is also set, it automatically becomes the backup. If the
    preferred provider's key is missing but the other one is configured,
    that one is used — a misconfigured preference never breaks the run.
    """
    prefer = env("LLM_PROVIDER", "anthropic").lower()
    builders: dict[str, Any] = {"anthropic": AnthropicProvider, "gemini": GeminiProvider}
    order = [prefer] + [name for name in ("anthropic", "gemini") if name != prefer]

    providers: list[LLMProvider] = []
    for name in order:
        if name not in builders:
            log.warning("unknown LLM_PROVIDER %r — skipping", name)
            continue
        try:
            providers.append(builders[name]())
        except Exception as exc:  # unconfigured or broken — try the next one
            log.info("provider %s unavailable: %s", name, exc)

    if not providers:
        raise LLMError(
            "no LLM provider available — set ANTHROPIC_API_KEY and/or GEMINI_API_KEY"
        )
    if len(providers) == 2:
        log.info(
            "LLM providers: %s (primary) with %s as automatic backup",
            providers[0].name, providers[1].name,
        )
        return FallbackProvider(providers[0], providers[1])
    log.info("LLM provider: %s (no backup key configured)", providers[0].name)
    return providers[0]
