"""
Purpose: Minimal OpenAI-compatible LLM client for planning, review, and local patch generation.
Input/Output: Workers send prompts and receive either plain text or JSON-like content from the configured backend.
Important invariants: The backend is optional, errors are explicit, and callers must treat generated content as advisory until validated.
How to debug: If model calls fail, inspect the base URL, API key, request payload, and the raw response captured here.
"""

from __future__ import annotations

import asyncio
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

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Reuse one async HTTP client per worker process to avoid needless reconnect churn on slow hosts."""

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.llm_http_timeout(),
                transport=self.transport,
            )
        return self._client

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

        async def _request(provider_name: str, base_url: str, model_name: str, api_key: str) -> dict[str, Any]:
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
            request_url = f"{base_url.rstrip('/')}/chat/completions"
            timeout_summary = self.settings.llm_timeout_summary(request_deadline_seconds=route.request_timeout_seconds)
            client = self._get_client()
            try:
                async with asyncio.timeout(route.request_timeout_seconds):
                    response = await client.post(
                        request_url,
                        headers=headers,
                        json=payload,
                    )
                response.raise_for_status()
                return response.json()
            except TimeoutError as exc:
                raise LLMError(
                    "LLM request exceeded the worker-stage deadline for "
                    f"`{worker_name}` via provider `{provider_name}` using model `{model_name}` at `{base_url}`. "
                    f"Configured timeouts: {timeout_summary}. "
                    "On slow local hardware this usually means the model is still inferencing longer than the configured stage budget."
                ) from exc
            except httpx.TimeoutException as exc:
                raise LLMError(
                    "LLM transport timed out for "
                    f"`{worker_name}` via provider `{provider_name}` using model `{model_name}` at `{base_url}`. "
                    f"Configured timeouts: {timeout_summary}. Transport error: {exc}."
                ) from exc
            except httpx.HTTPStatusError as exc:
                response_snippet = exc.response.text[:300].strip()
                raise LLMError(
                    "LLM backend returned an HTTP error for "
                    f"`{worker_name}` via provider `{provider_name}` using model `{model_name}` at `{base_url}`: "
                    f"{exc.response.status_code} {response_snippet}"
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(
                    "LLM transport failed for "
                    f"`{worker_name}` via provider `{provider_name}` using model `{model_name}` at `{base_url}`: {exc}"
                ) from exc
            except ValueError as exc:
                raise LLMError(
                    "LLM backend returned invalid JSON for "
                    f"`{worker_name}` via provider `{provider_name}` using model `{model_name}` at `{base_url}`: {exc}"
                ) from exc

        errors: list[str] = []
        try:
            data = await _request(provider.name, provider.base_url, provider.model_name, provider.api_key)
        except LLMError as primary_error:
            errors.append(str(primary_error))
            if fallback_provider is None:
                raise LLMError(str(primary_error)) from primary_error
            try:
                data = await _request(
                    fallback_provider.name,
                    fallback_provider.base_url,
                    fallback_provider.model_name,
                    fallback_provider.api_key,
                )
            except LLMError as fallback_error:
                errors.append(str(fallback_error))
                raise LLMError(
                    "All configured model providers failed for "
                    f"`{worker_name}`. " + " | ".join(errors)
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
        """Ask the model for JSON and parse the first JSON object found in its reply.

        Extraction order per attempt:
        1. Direct parse of the whole response.
        2. Content of the first ```...``` code block.
        3. Substring from the first '{' to the last '}' (handles prose wrappers).

        If the first attempt yields no parseable JSON at all, one retry is made with
        a minimal system prompt that asks the model to reformat its previous answer.
        """

        # JSON instruction goes first so instruction-tuned models see it before
        # any role description that might pull them toward prose answers.
        json_instruction = (
            "IMPORTANT: Your entire reply must be a single valid JSON object. "
            "Do not write any text before or after the JSON. "
            "Do not use markdown. Start with '{' and end with '}'.\n\n"
        )
        raw = await self.complete(
            system_prompt=json_instruction + system_prompt,
            user_prompt=user_prompt,
            worker_name=worker_name,
            temperature=0.1,
        )

        result = self._extract_json(raw)
        if result is not None:
            return result

        # One retry: ask the model to convert its own prose answer into JSON.
        # Truncate the input so local small models are not overwhelmed by the full response.
        retry_input = raw[:3000] + ("..." if len(raw) > 3000 else "")
        retry_raw = await self.complete(
            system_prompt=(
                "You must output a valid JSON object and nothing else. "
                "No prose, no markdown, no explanation. "
                "Start your reply with '{' and end with '}'. "
                "Convert the following text into a JSON object that captures its key information."
            ),
            user_prompt=retry_input,
            worker_name=worker_name,
            temperature=0.0,
        )
        result = self._extract_json(retry_raw)
        if result is not None:
            return result

        raise LLMError(f"Model did not return valid JSON: {raw}")

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        """Try three extraction strategies and return the first parseable dict, or None."""

        candidates: list[str] = [text.strip()]

        if "```" in text:
            block = text.split("```")[1]
            candidates.append(block.replace("json", "", 1).strip())

        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            candidates.append(text[brace_start : brace_end + 1])

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                continue
        return None
