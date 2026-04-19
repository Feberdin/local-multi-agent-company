"""
Purpose: Run opt-in live integration tests directly against the configured Ollama OpenAI-compatible API.
Input/Output: Sends tiny chat-completions requests to the real model endpoints and validates both the raw HTTP shape
and the normalized `LLMClient` contract output.
Important invariants:
  - These tests are opt-in and must never run accidentally during the normal unit suite.
  - Requests stay tiny so operators can observe real output without burning large amounts of inference time.
  - Failures must explain whether the breakage came from transport, raw response shape, or contract normalization.
How to debug: Re-run with `RUN_OLLAMA_LIVE_TESTS=1` and inspect the printed raw output previews per provider first.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.llm import LLMClient, LLMError


def _require_live_ollama() -> None:
    """Skip the suite unless the operator explicitly opted into real model calls."""

    if not _env_flag("RUN_OLLAMA_LIVE_TESTS"):
        pytest.skip("Live Ollama integration tests are opt-in. Set RUN_OLLAMA_LIVE_TESTS=1 to enable them.")


def _env_flag(name: str) -> bool:
    """Interpret common truthy environment values without importing extra config layers."""

    from os import getenv

    return str(getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def _live_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, provider_name: str) -> Settings:
    """Create isolated settings that still point at the real configured Ollama endpoints."""

    default_settings = Settings()
    routing_file = tmp_path / f"model-routing-{provider_name}.yaml"
    routing_file.write_text(
        (
            "workers:\n"
            "  coding:\n"
            f"    primary_provider: {provider_name}\n"
            "    fallback_provider:\n"
            "    request_timeout_seconds: 90.0\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(routing_file))
    monkeypatch.setenv("MISTRAL_BASE_URL", default_settings.mistral_base_url)
    monkeypatch.setenv("QWEN_BASE_URL", default_settings.qwen_base_url)
    monkeypatch.setenv("MISTRAL_MODEL_NAME", default_settings.mistral_model_name)
    monkeypatch.setenv("QWEN_MODEL_NAME", default_settings.qwen_model_name)
    monkeypatch.setenv("NO_PROXY", "192.168.57.10,localhost,127.0.0.1")
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("LLM_READ_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("LLM_WRITE_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("LLM_POOL_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("LLM_REQUEST_DEADLINE_SECONDS", "95")
    return Settings()


def _provider_target(settings: Settings, provider_name: str) -> tuple[str, str]:
    """Resolve base URL and model name for one configured live provider."""

    if provider_name == "mistral":
        return settings.mistral_base_url, settings.mistral_model_name
    if provider_name == "qwen":
        return settings.qwen_base_url, settings.qwen_model_name
    raise AssertionError(f"Unsupported live provider: {provider_name}")


def _openai_endpoint(base_url: str, suffix: str) -> str:
    """Join a base URL and suffix without depending on the app's internal readiness helpers."""

    return base_url.rstrip("/") + suffix


def _json_candidates(text: str) -> list[str]:
    """Return likely JSON substrings so slightly wrapped model output can still be parsed in a helpful way."""

    candidates = [text.strip()]
    if "```" in text:
        parts = text.split("```")
        for part in parts[1::2]:
            candidates.append(part.replace("json", "", 1).strip())
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(text[brace_start : brace_end + 1])
    return [candidate for candidate in candidates if candidate]


def _parse_json_from_content(content: str) -> dict[str, object]:
    """Parse the first valid JSON object from the raw model content and raise a readable assertion otherwise."""

    for candidate in _json_candidates(content):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise AssertionError(f"Model content was not parseable as JSON. Preview: {content[:400]!r}")


def _reasoning_preview(message: dict[str, object]) -> str:
    """Keep the live test diagnostics readable when a reasoning model returns no visible assistant content."""

    reasoning = str(message.get("reasoning") or "").strip()
    if len(reasoning) <= 400:
        return reasoning
    return reasoning[:399].rstrip() + "…"


def _raw_request_payload(model_name: str, provider_name: str) -> dict[str, object]:
    """Keep the live raw smoke request tiny and deterministic so it stays cheap and easy to inspect."""

    return {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "Reply with valid JSON only. No markdown fences."},
            {
                "role": "user",
                "content": (
                    f"Return exactly {{\"ok\":true,\"provider\":\"{provider_name}\",\"kind\":\"live_raw\"}} "
                    "and nothing else."
                ),
            },
        ],
        "temperature": 0.0,
        "max_tokens": 64,
        "format": "json",
        "response_format": {"type": "json_object"},
    }


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["mistral", "qwen"])
async def test_ollama_live_chat_completions_raw_output_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    """Validate the raw OpenAI-compatible response shape from the real Ollama endpoint."""

    _require_live_ollama()
    settings = _live_settings(tmp_path, monkeypatch, provider_name=provider_name)
    base_url, model_name = _provider_target(settings, provider_name)
    target = _openai_endpoint(base_url, "/chat/completions")
    payload = _raw_request_payload(model_name, provider_name)
    timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(target, json=payload)

    assert response.status_code == 200, response.text[:600]
    body = response.json()
    assert isinstance(body.get("choices"), list) and body["choices"], body
    choice = body["choices"][0]
    message = choice["message"]
    assert isinstance(message, dict), body
    assert "content" in message, body
    content = str(message.get("content") or "").strip()
    print(f"\n[{provider_name}] raw content preview: {content[:220]!r}")
    if content:
        parsed = _parse_json_from_content(content)
        assert parsed["ok"] is True
        assert parsed["provider"] == provider_name
        assert parsed["kind"] == "live_raw"
        return

    # qwen on this Ollama endpoint currently emits visible reasoning while leaving
    # assistant content empty. We keep that behavior under test so operators see it
    # explicitly instead of assuming the raw endpoint always exposes final JSON.
    reasoning_preview = _reasoning_preview(message)
    print(f"[{provider_name}] raw reasoning preview: {reasoning_preview!r}")
    assert provider_name == "qwen", body
    assert reasoning_preview
    assert "\"provider\":\"qwen\"" in reasoning_preview or "\"provider\": \"qwen\"" in reasoning_preview
    assert choice.get("finish_reason") == "length"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["mistral", "qwen"])
async def test_ollama_live_llm_client_coding_contract_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    """Validate that the shared LLM client can normalize a real live coding-contract response from Ollama."""

    _require_live_ollama()
    settings = _live_settings(tmp_path, monkeypatch, provider_name=provider_name)
    client = LLMClient(settings)

    kwargs = {
        "system_prompt": (
            "You are a careful coding worker. "
            "Return a single JSON object with keys summary, operations, and blocking_reason. "
            "This is a contract smoke test without repository access. "
            "Return summary 'OK', operations [], and blocking_reason 'OK'. "
            "No prose outside the JSON object."
        ),
        "user_prompt": (
            "Live contract smoke test against the real Ollama API. "
            "Do not invent file operations. Prove only that the edit_plan contract can be emitted cleanly."
        ),
        "worker_name": "coding",
        "required_keys": ["summary", "operations"],
        "max_tokens": 160,
    }

    if provider_name == "qwen":
        with pytest.raises(LLMError) as exc_info:
            await client.complete_json_with_trace(**kwargs)
        message = str(exc_info.value)
        print(f"\n[{provider_name}] normalized coding failure: {message[:320]}")
        assert "reasoning-only output without visible assistant content" in message
        assert exc_info.value.trace["provider"] == provider_name
        assert exc_info.value.trace["response_shape"] == "reasoning_only_empty_content"
        assert exc_info.value.trace["finish_reason"] == "length"
        assert exc_info.value.trace["used_fallback"] is False
        return

    payload, trace = await client.complete_json_with_trace(**kwargs)
    print(f"\n[{provider_name}] normalized coding payload: {json.dumps(payload, ensure_ascii=False)[:260]}")
    assert isinstance(payload, dict)
    assert "summary" in payload
    assert "operations" in payload
    assert isinstance(payload["operations"], list)
    assert trace["provider"] == provider_name
    assert trace["used_fallback"] is False
