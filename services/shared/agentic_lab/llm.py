"""
Purpose: Minimal OpenAI-compatible LLM client for planning, review, and local patch generation.
Input/Output: Workers send prompts and receive either plain text or JSON-like content from the configured backend.
Important invariants: The backend is optional, errors are explicit, and callers must treat generated content as advisory until validated.
How to debug: If model calls fail, inspect the base URL, API key, request payload, and the raw response captured here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.edit_ops import validate_edit_plan_payload
from services.shared.agentic_lab.guardrails import sanitize_untrusted_text
from services.shared.agentic_lab.model_routing import (
    ModelProvider,
    WorkerModelRoute,
    resolve_fallback_provider,
    resolve_worker_route,
)

LOGGER = logging.getLogger(__name__)


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

    @staticmethod
    def _missing_required_json_keys(payload: dict[str, Any], required_keys: tuple[str, ...]) -> list[str]:
        """Return missing top-level keys for operator-facing contract validation."""

        return [key for key in required_keys if key not in payload]

    @classmethod
    def _validate_json_contract(
        cls,
        payload: dict[str, Any],
        *,
        output_contract: str,
        required_keys: tuple[str, ...],
    ) -> str | None:
        """Return a human-readable validation error when parsed JSON is still semantically unusable."""

        missing_keys = cls._missing_required_json_keys(payload, required_keys)
        if missing_keys:
            return "Missing required JSON keys: " + ", ".join(missing_keys)

        if output_contract == "edit_plan":
            return validate_edit_plan_payload(payload)

        return None

    @staticmethod
    def _content_to_text(value: Any) -> str:
        """Normalize string or structured content parts into one plain assistant text payload."""

        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    if item.strip():
                        parts.append(item.strip())
                    continue
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").lower() in {"reasoning", "thinking"}:
                    # Some OpenAI-compatible backends mix hidden reasoning and visible answer parts
                    # in the same content list. Only the visible answer must reach worker parsers.
                    continue
                text_value = item.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    parts.append(text_value.strip())
                    continue
                content_value = item.get("content")
                if isinstance(content_value, str) and content_value.strip():
                    parts.append(content_value.strip())
            return "\n".join(parts).strip()
        return ""

    @classmethod
    def _collect_reasoning_fragments(cls, payload: Any) -> list[str]:
        """Collect optional reasoning fields so they can be logged for diagnostics without polluting output."""

        if not isinstance(payload, dict):
            return []
        fragments: list[str] = []
        for key in ("thinking", "reasoning", "reasoning_content"):
            text = cls._content_to_text(payload.get(key))
            if text:
                fragments.append(text)
        content = payload.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").lower() not in {"reasoning", "thinking"}:
                    continue
                text = cls._content_to_text(item.get("text") or item.get("content"))
                if text:
                    fragments.append(text)
        return fragments

    @classmethod
    def _extract_response_text(cls, data: Any) -> tuple[str, dict[str, Any]]:
        """Read the primary assistant text from OpenAI-compatible and Ollama-like response shapes."""

        diagnostics: dict[str, Any] = {
            "content_source": "",
            "reasoning_discarded": False,
            "reasoning_preview": "",
        }
        reasoning_fragments: list[str] = []

        if isinstance(data, dict):
            if isinstance(data.get("choices"), list) and data["choices"]:
                first_choice = data["choices"][0]
                if isinstance(first_choice, dict):
                    reasoning_fragments.extend(cls._collect_reasoning_fragments(first_choice))
                    message = first_choice.get("message")
                    if isinstance(message, dict):
                        reasoning_fragments.extend(cls._collect_reasoning_fragments(message))
                        content = cls._content_to_text(message.get("content"))
                        if content:
                            diagnostics["content_source"] = "choices[0].message.content"
                            break_content = content
                            diagnostics["reasoning_discarded"] = bool(reasoning_fragments)
                            diagnostics["reasoning_preview"] = cls._response_preview("\n".join(reasoning_fragments))
                            return break_content, diagnostics
                    text = cls._content_to_text(first_choice.get("text"))
                    if text:
                        diagnostics["content_source"] = "choices[0].text"
                        diagnostics["reasoning_discarded"] = bool(reasoning_fragments)
                        diagnostics["reasoning_preview"] = cls._response_preview("\n".join(reasoning_fragments))
                        return text, diagnostics

            message = data.get("message")
            if isinstance(message, dict):
                reasoning_fragments.extend(cls._collect_reasoning_fragments(message))
                content = cls._content_to_text(message.get("content"))
                if content:
                    diagnostics["content_source"] = "message.content"
                    diagnostics["reasoning_discarded"] = bool(reasoning_fragments)
                    diagnostics["reasoning_preview"] = cls._response_preview("\n".join(reasoning_fragments))
                    return content, diagnostics

            response = cls._content_to_text(data.get("response"))
            if response:
                diagnostics["content_source"] = "response"
                diagnostics["reasoning_discarded"] = bool(reasoning_fragments)
                diagnostics["reasoning_preview"] = cls._response_preview("\n".join(reasoning_fragments))
                return response, diagnostics

            content = cls._content_to_text(data.get("content"))
            if content:
                diagnostics["content_source"] = "content"
                diagnostics["reasoning_discarded"] = bool(reasoning_fragments)
                diagnostics["reasoning_preview"] = cls._response_preview("\n".join(reasoning_fragments))
                return content, diagnostics

            reasoning_fragments.extend(cls._collect_reasoning_fragments(data))

        diagnostics["reasoning_discarded"] = bool(reasoning_fragments)
        diagnostics["reasoning_preview"] = cls._response_preview("\n".join(reasoning_fragments)) if reasoning_fragments else ""
        return "", diagnostics

    @staticmethod
    def _strip_embedded_thinking(content: str) -> tuple[str, bool]:
        """Remove embedded <think> blocks from models that mix reasoning and final output."""

        stripped = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return stripped, stripped != content.strip()

    @staticmethod
    def _json_contract_instruction(output_contract: str) -> str:
        """Tailor JSON-format instructions to the kind of structured worker output that is expected."""

        instructions = {
            "edit_plan": (
                "Return one parseable JSON object for file-edit operations. "
                "If a safe code change is possible, provide concrete operations. "
                "If no safe code change is possible, keep operations empty and include blocking_reason. "
                "For explicit code-change goals, zero operations are only valid when the blocker names one concrete "
                "candidate file and why changing it would be unsafe."
            ),
            "json": "Return one parseable JSON object that matches the requested schema exactly.",
            "text": "Return one parseable JSON object and nothing else.",
        }
        return instructions.get(output_contract, instructions["json"])

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

        content, diagnostics = self._extract_response_text(data)
        if not content:
            raise LLMError(
                "Unexpected LLM response shape for "
                f"`{worker_name}` via provider `{provider.name}` using model `{provider.model_name}` at `{provider.base_url}`. "
                f"No usable assistant content was found. "
                f"Detected content source: `{diagnostics.get('content_source') or 'none'}`. "
                f"Reasoning discarded: {diagnostics.get('reasoning_discarded')}. "
                f"Response preview: {self._response_preview(json.dumps(data, ensure_ascii=True, default=str))}"
            )

        if diagnostics.get("reasoning_discarded"):
            LOGGER.debug(
                "Discarded separate reasoning fields for worker `%s` via provider `%s`: %s",
                worker_name,
                provider.name,
                diagnostics.get("reasoning_preview") or "reasoning hidden",
            )

        content, stripped_thinking = self._strip_embedded_thinking(content)
        if stripped_thinking:
            LOGGER.debug(
                "Discarded embedded <think> blocks for worker `%s` via provider `%s` (content source `%s`).",
                worker_name,
                provider.name,
                diagnostics.get("content_source") or "unknown",
            )
        if not content:
            raise LLMError(
                "LLM backend returned only reasoning/debug content for "
                f"`{worker_name}` via provider `{provider.name}` using model `{provider.model_name}` at `{provider.base_url}`. "
                "No final assistant output remained after removing reasoning blocks."
            )
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

        text, _trace = await self.complete_with_trace(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            worker_name=worker_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return text

    async def complete_with_trace(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        worker_name: str = "default",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Return plain text plus provider diagnostics for operator-facing probe flows."""

        if not self.settings.has_llm_backend():
            raise LLMError("No LLM backend configured.")

        provider, route = resolve_worker_route(self.settings, worker_name)
        fallback_provider = resolve_fallback_provider(self.settings, worker_name)
        LOGGER.debug(
            "LLM route for `%s`: primary=%s fallback=%s timeout=%ss reasoning=%s contract=%s note=%s",
            worker_name,
            provider.name,
            fallback_provider.name if fallback_provider else "none",
            route.request_timeout_seconds,
            route.reasoning,
            route.output_contract,
            route.routing_note or route.purpose,
        )

        request_temperature = temperature if temperature is not None else route.temperature
        request_max_tokens = max_tokens if max_tokens is not None else route.max_tokens

        errors: list[str] = []
        candidates = self._provider_candidates(provider, fallback_provider)
        for index, candidate in enumerate(candidates):
            try:
                text = await self._complete_with_provider(
                    provider=candidate,
                    route=route,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    worker_name=worker_name,
                    temperature=request_temperature,
                    max_tokens=request_max_tokens,
                )
                return text, {
                    "provider": candidate.name,
                    "model_name": candidate.model_name,
                    "base_url": candidate.base_url,
                    "used_fallback": index > 0,
                    "repair_pass_used": False,
                    "output_contract": route.output_contract,
                    "request_timeout_seconds": route.request_timeout_seconds,
                    "max_tokens": request_max_tokens,
                }
            except LLMError as exc:
                errors.append(str(exc))
                next_candidate = candidates[index + 1] if index + 1 < len(candidates) else None
                if next_candidate is not None:
                    LOGGER.warning(
                        "LLM provider `%s` failed for worker `%s`. Falling back to `%s`. Cause: %s",
                        candidate.name,
                        worker_name,
                        next_candidate.name,
                        exc,
                    )

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
        required_keys: list[str] | tuple[str, ...] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Ask the model for JSON and parse the first JSON object found in its reply.

        Extraction order per attempt:
        1. Direct parse of the whole response.
        2. Content of the first ```...``` code block.
        3. Substring from the first '{' to the last '}' (handles prose wrappers).

        If the first attempt yields no parseable JSON at all, one retry is made with
        a minimal system prompt that asks the model to reformat its previous answer.
        """

        payload, _trace = await self.complete_json_with_trace(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            worker_name=worker_name,
            required_keys=required_keys,
            max_tokens=max_tokens,
        )
        return payload

    async def complete_json_with_trace(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        worker_name: str = "default",
        required_keys: list[str] | tuple[str, ...] | None = None,
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return parsed JSON plus provider diagnostics for structured-model probes."""

        # JSON instruction goes first so instruction-tuned models see it before
        # any role description that might pull them toward prose answers.
        provider, route = resolve_worker_route(self.settings, worker_name)
        fallback_provider = resolve_fallback_provider(self.settings, worker_name)
        json_instruction = (
            "IMPORTANT: Your entire reply must be a single valid JSON object. "
            "Do not write any text before or after the JSON. "
            "Do not use markdown. Start with '{' and end with '}'. "
            f"{self._json_contract_instruction(route.output_contract)}\n\n"
        )
        LOGGER.debug(
            "Structured LLM route for `%s`: primary=%s fallback=%s timeout=%ss contract=%s note=%s",
            worker_name,
            provider.name,
            fallback_provider.name if fallback_provider else "none",
            route.request_timeout_seconds,
            route.output_contract,
            route.routing_note or route.purpose,
        )
        errors: list[str] = []
        candidates = self._provider_candidates(provider, fallback_provider)
        required_key_tuple = tuple(required_keys or ())
        request_max_tokens = max_tokens if max_tokens is not None else route.max_tokens

        for index, candidate in enumerate(candidates):
            try:
                raw = await self._complete_with_provider(
                    provider=candidate,
                    route=route,
                    system_prompt=json_instruction + system_prompt,
                    user_prompt=user_prompt,
                    worker_name=worker_name,
                    temperature=0.1,
                    max_tokens=request_max_tokens,
                    json_mode=True,
                )
            except LLMError as exc:
                errors.append(str(exc))
                next_candidate = candidates[index + 1] if index + 1 < len(candidates) else None
                if next_candidate is not None:
                    LOGGER.warning(
                        "Structured provider `%s` failed for worker `%s`. Falling back to `%s`. Cause: %s",
                        candidate.name,
                        worker_name,
                        next_candidate.name,
                        exc,
                    )
                continue

            result = self._extract_json(raw)
            validation_error = None
            if result is not None:
                validation_error = self._validate_json_contract(
                    result,
                    output_contract=route.output_contract,
                    required_keys=required_key_tuple,
                )
            if result is not None and validation_error is None:
                return result, {
                    "provider": candidate.name,
                    "model_name": candidate.model_name,
                    "base_url": candidate.base_url,
                    "used_fallback": index > 0,
                    "repair_pass_used": False,
                    "output_contract": route.output_contract,
                    "request_timeout_seconds": route.request_timeout_seconds,
                    "max_tokens": request_max_tokens,
                    "raw_response_text": raw,
                }

            if validation_error is None:
                LOGGER.warning(
                    "Structured output from provider `%s` for worker `%s` was not valid JSON. "
                    "Trying one stricter repair pass. Preview: %s",
                    candidate.name,
                    worker_name,
                    self._response_preview(raw),
                )
                errors.append(
                    "Provider "
                    f"`{candidate.name}` with model `{candidate.model_name}` returned non-JSON text: "
                    f"{self._response_preview(raw)}"
                )
            else:
                LOGGER.warning(
                    "Structured output from provider `%s` for worker `%s` failed JSON contract validation. "
                    "Issue: %s. Trying one stricter repair pass. Preview: %s",
                    candidate.name,
                    worker_name,
                    validation_error,
                    self._response_preview(raw),
                )
                errors.append(
                    "Provider "
                    f"`{candidate.name}` with model `{candidate.model_name}` returned JSON that did not satisfy the "
                    f"`{route.output_contract}` contract: {validation_error}. "
                    f"Preview: {self._response_preview(raw)}"
                )

            retry_input = raw[:3000] + ("..." if len(raw) > 3000 else "")
            try:
                retry_raw = await self._complete_with_provider(
                    provider=candidate,
                    route=route,
                    system_prompt=(
                        "You must output a valid JSON object and nothing else. "
                        + "No prose, no markdown, no explanation. "
                        + "Start your reply with '{' and end with '}'. "
                        + f"{self._json_contract_instruction(route.output_contract)} "
                        + (
                            f"The previous reply failed this validation rule: {validation_error}. "
                            if validation_error
                            else ""
                        )
                        + "Convert the following text into a JSON object that captures its key information."
                    ),
                    user_prompt=retry_input,
                    worker_name=worker_name,
                    temperature=0.0,
                    max_tokens=request_max_tokens,
                    json_mode=True,
                )
            except LLMError as exc:
                errors.append(str(exc))
                continue

            result = self._extract_json(retry_raw)
            validation_error = None
            if result is not None:
                validation_error = self._validate_json_contract(
                    result,
                    output_contract=route.output_contract,
                    required_keys=required_key_tuple,
                )
            if result is not None and validation_error is None:
                return result, {
                    "provider": candidate.name,
                    "model_name": candidate.model_name,
                    "base_url": candidate.base_url,
                    "used_fallback": index > 0,
                    "repair_pass_used": True,
                    "output_contract": route.output_contract,
                    "request_timeout_seconds": route.request_timeout_seconds,
                    "max_tokens": request_max_tokens,
                    "raw_response_text": retry_raw,
                }

            next_candidate = candidates[index + 1] if index + 1 < len(candidates) else None
            if next_candidate is not None:
                LOGGER.warning(
                    "Provider `%s` still did not produce valid JSON for worker `%s`; falling back to `%s`.",
                    candidate.name,
                    worker_name,
                    next_candidate.name,
                )
            errors.append(
                "Provider "
                f"`{candidate.name}` JSON-repair attempt still returned no valid JSON: "
                + (
                    f"{validation_error}. Preview: {self._response_preview(retry_raw)}"
                    if validation_error
                    else self._response_preview(retry_raw)
                )
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
