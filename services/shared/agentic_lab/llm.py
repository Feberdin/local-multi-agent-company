"""
Purpose: Minimal OpenAI-compatible LLM client for planning, review, and local patch generation.
Input/Output: Workers send prompts and receive either plain text or JSON-like content from the configured backend.
Important invariants: The backend is optional, errors are explicit, and callers must treat generated content as advisory until validated.
How to debug: If model calls fail, inspect the base URL, API key, request payload, and the raw response captured here.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.guardrails import sanitize_untrusted_text
from services.shared.agentic_lab.model_routing import resolve_fallback_provider, resolve_worker_route


class LLMError(RuntimeError):
    """Raised when an LLM call or response format is unusable."""


class LLMClient:
    """Small wrapper around an OpenAI-compatible chat completions endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        worker_name: str = "default",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return plain text from the configured chat model backend."""

        if not self.settings.has_llm_backend():
            raise LLMError("No LLM backend configured.")

        provider, route = resolve_worker_route(self.settings, worker_name)
        fallback_provider = resolve_fallback_provider(self.settings, worker_name)

        request_temperature = temperature if temperature is not None else route.temperature
        request_max_tokens = max_tokens if max_tokens is not None else route.max_tokens

        async def _request(base_url: str, model_name: str, api_key: str) -> dict[str, Any]:
            headers = {"Content-Type": "application/json"}
            if api_key and "replace-me" not in api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": sanitize_untrusted_text(system_prompt, max_length=8_000)},
                    {"role": "user", "content": sanitize_untrusted_text(user_prompt, max_length=16_000)},
                ],
                "temperature": request_temperature,
                "max_tokens": request_max_tokens,
            }
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                return response.json()

        try:
            data = await _request(provider.base_url, provider.model_name, provider.api_key)
        except httpx.HTTPError as primary_error:
            if fallback_provider is None:
                raise LLMError(f"Primary model provider `{provider.name}` failed: {primary_error}") from primary_error
            try:
                data = await _request(
                    fallback_provider.base_url,
                    fallback_provider.model_name,
                    fallback_provider.api_key,
                )
            except httpx.HTTPError as fallback_error:
                raise LLMError(
                    f"Both primary `{provider.name}` and fallback `{fallback_provider.name}` model providers failed."
                ) from fallback_error

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {data}") from exc

    async def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        worker_name: str = "default",
    ) -> dict[str, Any]:
        """Ask the model for JSON and parse the first JSON object found in its reply."""

        raw = await self.complete(
            system_prompt=system_prompt + "\nReturn JSON only.",
            user_prompt=user_prompt,
            worker_name=worker_name,
            temperature=0.1,
        )

        json_text = raw.strip()
        if "```" in json_text:
            json_text = json_text.split("```")[1]
            json_text = json_text.replace("json", "", 1).strip()

        try:
            return json.loads(json_text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model did not return valid JSON: {raw}") from exc
