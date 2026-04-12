"""
Purpose: Minimal OpenAI-compatible LLM client for planning, review, and local patch generation.
Input/Output: Workers send prompts and receive either plain text or JSON-like content from the configured backend.
Important invariants: The backend is optional, errors are explicit, and callers must treat generated content as advisory until validated.
How to debug: If model calls fail, inspect the base URL, API key, request payload, and the raw response captured here.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.guardrails import sanitize_untrusted_text
from services.shared.agentic_lab.model_routing import (
    ModelProvider,
    WorkerModelRoute,
    resolve_fallback_provider,
    resolve_worker_route,
)


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

    @staticmethod
    def _provider_candidates(primary: ModelProvider, fallback: ModelProvider | None) -> list[ModelProvider]:
        """Return providers in try-order without contacting the same backend twice."""

        candidates = [primary]
        if fallback and fallback.name != primary.name:
            candidates.append(fallback)
        return candidates

    @staticmethod
    def _response_preview(text: str, *, max_length: int = 240) -> str:
        """Keep raw model output readable in operator-visible error messages."""

        compact = " ".join(text.split())
        if len(compact) <= max_length:
            return compact
        return compact[: max_length - 1].rstrip() + "…"

    async def _complete_with_provider(
        self,
        *,
        provider: ModelProvider,
        route: WorkerModelRoute,
        system_prompt: str,
        user_prompt: str,
        worker_name: str,
        temperature: float,
        max_tokens: int | None,
        json_mode: bool = False,
    ) -> str:
        """Run one chat completion against one concrete provider and return plain text content."""

        headers = {"Content-Type": "application/json"}
        if provider.api_key and "replace-me" not in provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

        payload = {
            "model": provider.model_name,
            "messages": [
                {"role": "system", "content": sanitize_untrusted_text(system_prompt, max_length=8_000)},
                {"role": "user", "content": sanitize_untrusted_text(user_prompt, max_length=16_000)},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            # Ollama: forces JSON output at the inference level regardless of prompt phrasing.
            # OpenAI-compatible backends that support response_format will also respect this.
            payload["format"] = "json"
            payload["response_format"] = {"type": "json_object"}
        request_url = f"{provider.base_url.rstrip('/')}/chat/completions"
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
            data = response.json()
        except TimeoutError as exc:
            raise LLMError(
                "LLM request exceeded the worker-stage deadline for "
                f"`{worker_name}` via provider `{provider.name}` using model `{provider.model_name}` at `{provider.base_url}`. "
                f"Configured timeouts: {timeout_summary}. "
                "On slow local hardware this usually means the model is still inferencing longer than the configured stage budget."
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMError(
                "LLM transport timed out for "
                f"`{worker_name}` via provider `{provider.name}` using model `{provider.model_name}` at `{provider.base_url}`. "
                f"Configured timeouts: {timeout_summary}. Transport error: {exc}."
            ) from exc
        except httpx.HTTPStatusError as exc:
            response_snippet = exc.response.text[:300].strip()
            raise LLMError(
                "LLM backend returned an HTTP error for "
                f"`{worker_name}` via provider `{provider.name}` using model `{provider.model_name}` at `{provider.base_url}`: "
                f"{exc.response.status_code} {response_snippet}"
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                "LLM transport failed for "
                f"`{worker_name}` via provider `{provider.name}` using model `{provider.model_name}` at `{provider.base_url}`: {exc}"
            ) from exc
        except ValueError as exc:
            raise LLMError(
                "LLM backend returned invalid JSON for "
                f"`{worker_name}` via provider `{provider.name}` using model `{provider.model_name}` at `{provider.base_url}`: {exc}"
            ) from exc

        try:
            content: str = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                "Unexpected LLM response shape for "
                f"`{worker_name}` via provider `{provider.name}` using model `{provider.model_name}` at `{provider.base_url}`: {data}"
            ) from exc

        # Strip <think>...</think> blocks emitted by reasoning models (qwen3.5, deepseek-r1, etc.)
        # before any further processing. Without this, _extract_json grabs JSON from the thinking
        # block instead of the actual response, and plain-text completions include raw reasoning.
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content

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

        errors: list[str] = []
        for candidate in self._provider_candidates(provider, fallback_provider):
            try:
                return await self._complete_with_provider(
                    provider=candidate,
                    route=route,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    worker_name=worker_name,
                    temperature=request_temperature,
                    max_tokens=request_max_tokens,
                )
            except LLMError as exc:
                errors.append(str(exc))

        raise LLMError(
            "All configured model providers failed for "
            f"`{worker_name}`. " + " | ".join(errors)
        )

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
        provider, route = resolve_worker_route(self.settings, worker_name)
        fallback_provider = resolve_fallback_provider(self.settings, worker_name)
        errors: list[str] = []

        for candidate in self._provider_candidates(provider, fallback_provider):
            try:
                raw = await self._complete_with_provider(
                    provider=candidate,
                    route=route,
                    system_prompt=json_instruction + system_prompt,
                    user_prompt=user_prompt,
                    worker_name=worker_name,
                    temperature=0.1,
                    max_tokens=route.max_tokens,
                    json_mode=True,
                )
            except LLMError as exc:
                errors.append(str(exc))
                continue

            result = self._extract_json(raw)
            if result is not None:
                return result

            errors.append(
                "Provider "
                f"`{candidate.name}` with model `{candidate.model_name}` returned non-JSON text: "
                f"{self._response_preview(raw)}"
            )

            retry_input = raw[:3000] + ("..." if len(raw) > 3000 else "")
            try:
                retry_raw = await self._complete_with_provider(
                    provider=candidate,
                    route=route,
                    system_prompt=(
                        "You must output a valid JSON object and nothing else. "
                        "No prose, no markdown, no explanation. "
                        "Start your reply with '{' and end with '}'. "
                        "Convert the following text into a JSON object that captures its key information."
                    ),
                    user_prompt=retry_input,
                    worker_name=worker_name,
                    temperature=0.0,
                    max_tokens=route.max_tokens,
                    json_mode=True,
                )
            except LLMError as exc:
                errors.append(str(exc))
                continue

            result = self._extract_json(retry_raw)
            if result is not None:
                return result

            errors.append(
                "Provider "
                f"`{candidate.name}` JSON-repair attempt still returned no valid JSON: "
                f"{self._response_preview(retry_raw)}"
            )

        raise LLMError(
            f"Model did not return valid JSON for `{worker_name}`. " + " | ".join(errors)
        )

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
