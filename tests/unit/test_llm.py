"""
Purpose: Validate LLM routing, timeout handling, and fallback behavior for local OpenAI-compatible backends.
Input/Output: Tests run the shared LLM client against mocked transports and inspect returned content or raised errors.
Important invariants: Lightweight stages must fail fast with clear diagnostics, and heavier stages may fall back instead of hanging.
How to debug: If these tests fail, inspect `services/shared/agentic_lab/llm.py` and `services/shared/agentic_lab/model_routing.py`.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.llm import LLMClient, LLMError


def _settings_with_routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routing_yaml: str) -> Settings:
    routing_file = tmp_path / "model-routing.yaml"
    routing_file.write_text(routing_yaml, encoding="utf-8")
    monkeypatch.setenv("MODEL_ROUTING_CONFIG", str(routing_file))
    monkeypatch.setenv("MISTRAL_BASE_URL", "http://mistral.test/v1")
    monkeypatch.setenv("MISTRAL_MODEL_NAME", "mistral-small3.2:latest")
    monkeypatch.setenv("QWEN_BASE_URL", "http://qwen.test/v1")
    monkeypatch.setenv("QWEN_MODEL_NAME", "qwen3.5:35b-a3b")
    monkeypatch.setenv("LLM_CONNECT_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("LLM_READ_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("LLM_WRITE_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("LLM_POOL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("LLM_REQUEST_DEADLINE_SECONDS", "0.5")
    return Settings()


@pytest.mark.asyncio
async def test_llm_client_timeout_error_mentions_stage_model_and_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_routing(
        tmp_path,
        monkeypatch,
        (
            "workers:\n"
            "  requirements:\n"
            "    primary_provider: mistral\n"
            "    fallback_provider:\n"
            "    request_timeout_seconds: 0.01\n"
        ),
    )

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    client = LLMClient(settings, transport=httpx.MockTransport(slow_handler))

    with pytest.raises(LLMError) as exc_info:
        await client.complete("system", "user", worker_name="requirements")

    message = str(exc_info.value)
    assert "requirements" in message
    assert "mistral-small3.2:latest" in message
    assert "http://mistral.test/v1" in message
    assert "deadline=0.01s" in message


@pytest.mark.asyncio
async def test_llm_client_falls_back_when_primary_provider_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_routing(
        tmp_path,
        monkeypatch,
        (
            "workers:\n"
            "  research:\n"
            "    primary_provider: qwen\n"
            "    fallback_provider: mistral\n"
            "    request_timeout_seconds: 0.01\n"
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if "qwen.test" in str(request.url):
            raise httpx.ReadTimeout("simulated qwen timeout", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "fallback-ok"}}]},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete("system", "user", worker_name="research")

    assert response == "fallback-ok"


@pytest.mark.asyncio
async def test_complete_json_falls_back_when_primary_provider_returns_non_json_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_routing(
        tmp_path,
        monkeypatch,
        (
            "workers:\n"
            "  coding:\n"
            "    primary_provider: qwen\n"
            "    fallback_provider: mistral\n"
            "    request_timeout_seconds: 0.5\n"
        ),
    )

    call_counter = {"qwen": 0, "mistral": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if "qwen.test" in str(request.url):
            call_counter["qwen"] += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "# Feberdin Multi-Agent Architecture Review\nThis answer is prose, not JSON."
                            }
                        }
                    ]
                },
                request=request,
            )
        call_counter["mistral"] += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"summary":"Recovered JSON plan","operations":[]}'
                        }
                    }
                ]
            },
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete_json("system", "user", worker_name="coding")

    assert response == {"summary": "Recovered JSON plan", "operations": []}
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1


@pytest.mark.asyncio
async def test_complete_json_accepts_ollama_generate_style_response_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_routing(
        tmp_path,
        monkeypatch,
        (
            "workers:\n"
            "  coding:\n"
            "    primary_provider: mistral\n"
            "    fallback_provider: qwen\n"
            "    request_timeout_seconds: 0.5\n"
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"response": '{"summary":"Recovered from response field","operations":[]}'},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete_json("system", "user", worker_name="coding")

    assert response == {"summary": "Recovered from response field", "operations": []}


@pytest.mark.asyncio
async def test_complete_accepts_message_content_without_choices_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_routing(
        tmp_path,
        monkeypatch,
        (
            "workers:\n"
            "  research:\n"
            "    primary_provider: qwen\n"
            "    fallback_provider: mistral\n"
            "    request_timeout_seconds: 0.5\n"
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"content": "Antwort aus message.content"}},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete("system", "user", worker_name="research")

    assert response == "Antwort aus message.content"


@pytest.mark.asyncio
async def test_thinking_blocks_are_stripped_before_returning_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """qwen3.5 and other reasoning models emit <think>...</think> before the actual answer.
    The client must strip these so callers receive only the real response."""
    settings = _settings_with_routing(
        tmp_path,
        monkeypatch,
        (
            "workers:\n"
            "  research:\n"
            "    primary_provider: qwen\n"
            "    fallback_provider:\n"
            "    request_timeout_seconds: 0.5\n"
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "<think>\nsome internal reasoning\n</think>\n\nActual answer here."
                        }
                    }
                ]
            },
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))
    response = await client.complete("system", "user", worker_name="research")
    assert response == "Actual answer here."
    assert "<think>" not in response


@pytest.mark.asyncio
async def test_reasoning_list_parts_do_not_override_visible_answer_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_routing(
        tmp_path,
        monkeypatch,
        (
            "workers:\n"
            "  coding:\n"
            "    primary_provider: mistral\n"
            "    fallback_provider: qwen\n"
            "    request_timeout_seconds: 0.5\n"
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "reasoning", "text": "Internal chain of thought"},
                                {
                                    "type": "output_text",
                                    "text": '{"summary":"Visible JSON only","operations":[]}',
                                },
                            ]
                        }
                    }
                ]
            },
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    result = await client.complete_json("system", "user", worker_name="coding")

    assert result == {"summary": "Visible JSON only", "operations": []}


@pytest.mark.asyncio
async def test_thinking_blocks_stripped_before_json_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the thinking block contains a JSON-like object, _extract_json must not grab it.
    The real JSON after the </think> tag must be returned instead."""
    settings = _settings_with_routing(
        tmp_path,
        monkeypatch,
        (
            "workers:\n"
            "  coding:\n"
            "    primary_provider: qwen\n"
            "    fallback_provider:\n"
            "    request_timeout_seconds: 0.5\n"
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '<think>\n{"fake": "json inside thinking block"}\n</think>\n\n'
                                '{"summary": "real answer", "operations": [{"action": "create_file"}]}'
                            )
                        }
                    }
                ]
            },
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))
    result = await client.complete_json("system", "user", worker_name="coding")
    assert result["summary"] == "real answer"
    assert "fake" not in result


@pytest.mark.asyncio
async def test_complete_json_error_mentions_all_provider_attempts_when_json_never_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_with_routing(
        tmp_path,
        monkeypatch,
        (
            "workers:\n"
            "  coding:\n"
            "    primary_provider: qwen\n"
            "    fallback_provider: mistral\n"
            "    request_timeout_seconds: 0.5\n"
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if "qwen.test" in str(request.url):
            content = "# Architecture Review\nStill prose."
        else:
            content = "No JSON here either."
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    with pytest.raises(LLMError) as exc_info:
        await client.complete_json("system", "user", worker_name="coding")

    message = str(exc_info.value)
    assert "Model did not return valid JSON for `coding`" in message
    assert "Provider `qwen`" in message
    assert "Provider `mistral`" in message
