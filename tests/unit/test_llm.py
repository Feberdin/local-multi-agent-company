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
async def test_complete_with_trace_reports_provider_and_fallback_usage(
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
        if "qwen.test" in str(request.url):
            raise httpx.ReadTimeout("primary timeout", request=request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "fallback-ok"}}]},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response, trace = await client.complete_with_trace("system", "user", worker_name="research", max_tokens=123)

    assert response == "fallback-ok"
    assert trace["provider"] == "mistral"
    assert trace["used_fallback"] is True
    assert trace["max_tokens"] == 123


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
async def test_complete_json_falls_back_when_json_is_missing_required_keys(
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
            content = '{"response":"wrapped answer instead of direct contract"}'
        else:
            call_counter["mistral"] += 1
            content = '{"summary":"Recovered JSON plan","operations":[],"blocking_reason":"No safe patch."}'
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete_json(
        "system",
        "user",
        worker_name="coding",
        required_keys=["summary", "operations"],
    )

    assert response == {
        "summary": "Recovered JSON plan",
        "operations": [],
        "blocking_reason": "No safe patch.",
    }
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1


@pytest.mark.asyncio
async def test_complete_json_falls_back_when_edit_plan_operations_are_semantically_incomplete(
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
            content = '{"summary":"Broken plan","operations":[{"action":"create_or_update"}]}'
        else:
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Recovered JSON plan",'
                '"operations":['
                '{"action":"replace_lines","file_path":"worker_target.py","start_line":2,"end_line":2,'
                '"reason":"Fallback provider returned one complete operation.",'
                '"new_content":"    return \\"ok\\""}'
                "],"
                '"blocking_reason":""}'
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete_json(
        "system",
        "user",
        worker_name="coding",
        required_keys=["summary", "operations"],
    )

    assert response["summary"] == "Recovered JSON plan"
    assert response["operations"][0]["file_path"] == "worker_target.py"
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1


@pytest.mark.asyncio
async def test_complete_json_normalizes_nested_change_operations_from_fallback_provider(
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
            content = '{"summary":"Kein konkreter Code-Änderungsauftrag erkannt.","operations":[]}'
        else:
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Recover from nested repair output.",'
                '"operations":['
                '{'
                '"file":"worker_target.py",'
                '"changes":['
                '{'
                '"location":{"type":"function","name":"target_function"},'
                '"new_code":"def target_function():\\n    return \\"normalized-change-ops\\"\\n",'
                '"description":"Flatten one nested change object into one canonical edit operation."'
                '}'
                ']'
                '}'
                ']}'
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete_json(
        "system",
        "user",
        worker_name="coding",
        required_keys=["summary", "operations"],
    )

    assert response["summary"] == "Recover from nested repair output."
    assert response["operations"] == [
        {
            "action": "replace_symbol_body",
            "file_path": "worker_target.py",
            "reason": "Flatten one nested change object into one canonical edit operation.",
            "new_content": 'def target_function():\n    return "normalized-change-ops"\n',
            "symbol_name": "target_function",
        }
    ]
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1


@pytest.mark.asyncio
async def test_complete_json_falls_back_when_edit_plan_returns_generic_empty_blocker(
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

    call_counter = {"mistral": 0, "qwen": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if "mistral.test" in str(request.url):
            call_counter["mistral"] += 1
            content = (
                '{"summary":"User shared a comprehensive setup for a self-hosted AI agent system on Unraid.",'
                '"operations":[],'
                '"blocking_reason":"No specific code change or operation requested."}'
            )
        else:
            call_counter["qwen"] += 1
            content = (
                '{"summary":"Recovered concrete coding plan.",'
                '"operations":['
                '{"action":"replace_symbol_body","file_path":"worker_target.py","symbol_name":"target_function",'
                '"reason":"Fallback provider must stop the generic no-op drift.",'
                '"new_content":"def target_function():\\n    return \\"qwen-fallback\\"\\n"}'
                "],"
                '"blocking_reason":""}'
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete_json(
        "system",
        "user",
        worker_name="coding",
        required_keys=["summary", "operations"],
    )

    assert response["summary"] == "Recovered concrete coding plan."
    assert response["operations"][0]["file_path"] == "worker_target.py"
    assert call_counter["mistral"] == 2
    assert call_counter["qwen"] == 1


@pytest.mark.asyncio
async def test_complete_json_falls_back_when_edit_plan_returns_german_empty_summary_without_blocker(
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

    call_counter = {"mistral": 0, "qwen": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if "mistral.test" in str(request.url):
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Keine Dateiänderungen erforderlich: Es wurden keine spezifischen Änderungen oder Aufgaben '
                'bereitgestellt, die eine Bearbeitung erfordern.",'
                '"operations":[]}'
            )
        else:
            call_counter["qwen"] += 1
            content = (
                '{"summary":"Recovered concrete coding plan.",'
                '"operations":['
                '{"action":"replace_symbol_body","file_path":"worker_target.py","symbol_name":"target_function",'
                '"reason":"Fallback provider must recover from the German generic empty summary.",'
                '"new_content":"def target_function():\\n    return \\"qwen-fallback\\"\\n"}'
                "]}"
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete_json(
        "system",
        "user",
        worker_name="coding",
        required_keys=["summary", "operations"],
    )

    assert response["summary"] == "Recovered concrete coding plan."
    assert response["operations"][0]["file_path"] == "worker_target.py"
    assert call_counter["mistral"] == 2
    assert call_counter["qwen"] == 1


@pytest.mark.asyncio
async def test_complete_json_falls_back_when_edit_plan_claims_no_target_file_was_provided(
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
            content = (
                '{"summary":"Keine Dateiänderungen möglich: Es wurden keine Ziel-Dateien oder konkreten '
                'Änderungsaufträge bereitgestellt.",'
                '"operations":[],'
                '"blocking_reason":"Keine Ziel-Datei angegeben: Ohne einen spezifischen Dateipfad und eine klare '
                'Anforderung ist keine sichere Code-Änderung möglich."}'
            )
        else:
            call_counter["mistral"] += 1
            content = (
                '{"summary":"Recovered concrete coding plan.",'
                '"operations":['
                '{"action":"replace_symbol_body","file_path":"worker_target.py","symbol_name":"target_function",'
                '"reason":"Fallback provider must recover from the fake no-target-file blocker.",'
                '"new_content":"def target_function():\\n    return \\"qwen-target-recovered\\"\\n"}'
                "]}"
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
            request=request,
        )

    client = LLMClient(settings, transport=httpx.MockTransport(handler))

    response = await client.complete_json(
        "system",
        "user",
        worker_name="coding",
        required_keys=["summary", "operations"],
    )

    assert response["summary"] == "Recovered concrete coding plan."
    assert response["operations"][0]["file_path"] == "worker_target.py"
    assert call_counter["qwen"] == 2
    assert call_counter["mistral"] == 1


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
                                '{"summary": "real answer", "operations": ['
                                '{"action": "create_file", "file_path": "worker_target.py", '
                                '"reason": "Create a tiny file in the valid visible JSON payload.", '
                                '"new_content": "print(\\"ok\\")\\n"}]}'
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
