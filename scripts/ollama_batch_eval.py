"""
Purpose: Run a reproducible batch of direct Ollama API probes and summarize real output quality.
Input/Output: Sends 100 tiny prompts to the configured OpenAI-compatible endpoints and
writes a JSON report under `reports/`.
Important invariants:
  - The script talks directly to the model API instead of the worker stack.
  - `success` requires visible assistant content in the expected shape.
  - `degraded` captures reasoning-only replies so operators can still make routing
    decisions without losing evidence.
How to debug: If the batch behaves oddly, inspect the per-probe `content_preview`,
`reasoning_preview`, and `finish_reason` in the generated report first.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.ollama_eval import (
    OllamaProbeClassification,
    ProbeExpectation,
    classify_probe_outcome,
    recommend_provider_actions,
    summarize_provider_results,
)


@dataclass(frozen=True, slots=True)
class ProbeTemplate:
    """One tiny prompt family used in the batch evaluation."""

    name: str
    expectation: ProbeExpectation
    system_prompt: str
    user_prompt: str
    max_tokens: int
    force_json_mode: bool


@dataclass(frozen=True, slots=True)
class ProviderTarget:
    """Resolved provider endpoint for one live probe provider."""

    provider_name: str
    base_url: str
    model_name: str


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Serializable record for one live API probe."""

    provider_name: str
    model_name: str
    template_name: str
    repetition: int
    latency_ms: int
    status_code: int
    finish_reason: str
    content_preview: str
    reasoning_preview: str
    classification: dict[str, Any]


PROBE_TEMPLATES: tuple[ProbeTemplate, ...] = (
    ProbeTemplate(
        name="json_echo",
        expectation="json_visible",
        system_prompt="Reply with valid JSON only. No markdown fences.",
        user_prompt='Return exactly {"ok":true,"kind":"echo"} and nothing else.',
        max_tokens=48,
        force_json_mode=True,
    ),
    ProbeTemplate(
        name="json_edit_plan",
        expectation="json_visible",
        system_prompt=(
            "Return a single JSON object with keys summary, operations, and blocking_reason. "
            "No prose outside JSON."
        ),
        user_prompt='Return {"summary":"OK","operations":[],"blocking_reason":"OK"} exactly.',
        max_tokens=96,
        force_json_mode=True,
    ),
    ProbeTemplate(
        name="json_requirements",
        expectation="json_visible",
        system_prompt=(
            "Return a single JSON object with keys summary, acceptance_criteria, constraints, out_of_scope, "
            "risks, assumptions. No prose outside JSON."
        ),
        user_prompt=(
            'Return {"summary":"OK","acceptance_criteria":["OK"],"constraints":["tiny"],'
            '"out_of_scope":[],"risks":[],"assumptions":["local smoke"]} exactly.'
        ),
        max_tokens=128,
        force_json_mode=True,
    ),
    ProbeTemplate(
        name="markdown_summary",
        expectation="text_visible",
        system_prompt="Reply with a short markdown summary.",
        user_prompt="Return exactly two markdown bullet points about a successful smoke test.",
        max_tokens=80,
        force_json_mode=False,
    ),
    ProbeTemplate(
        name="plain_ok",
        expectation="text_visible",
        system_prompt="Reply with plain text only.",
        user_prompt='Return exactly: OK',
        max_tokens=24,
        force_json_mode=False,
    ),
)

REPETITIONS_PER_TEMPLATE = max(int(os.getenv("OLLAMA_BATCH_REPETITIONS", "10")), 1)
CONCURRENCY = max(int(os.getenv("OLLAMA_BATCH_CONCURRENCY", "2")), 1)
CONNECT_TIMEOUT_SECONDS = 10.0
READ_TIMEOUT_SECONDS = 45.0
WRITE_TIMEOUT_SECONDS = 20.0
POOL_TIMEOUT_SECONDS = 10.0


def _provider_targets(settings: Settings) -> tuple[ProviderTarget, ...]:
    """Resolve the configured live providers once for the whole batch."""

    return (
        ProviderTarget(
            provider_name="mistral",
            base_url=settings.mistral_base_url,
            model_name=settings.mistral_model_name,
        ),
        ProviderTarget(
            provider_name="qwen",
            base_url=settings.qwen_base_url,
            model_name=settings.qwen_model_name,
        ),
    )


def _payload(template: ProbeTemplate, provider: ProviderTarget) -> dict[str, Any]:
    """Build one tiny direct API request payload."""

    payload: dict[str, Any] = {
        "model": provider.model_name,
        "messages": [
            {"role": "system", "content": template.system_prompt},
            {"role": "user", "content": template.user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": template.max_tokens,
    }
    if template.force_json_mode:
        payload["format"] = "json"
        payload["response_format"] = {"type": "json_object"}
    return payload


def _preview(value: str, *, limit: int = 220) -> str:
    """Keep operator-facing previews compact and readable."""

    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1].rstrip() + "…"


async def _run_probe(
    client: httpx.AsyncClient,
    provider: ProviderTarget,
    template: ProbeTemplate,
    repetition: int,
) -> ProbeResult:
    """Execute one direct chat-completions probe and classify the output."""

    start = datetime.now(tz=UTC)
    try:
        response = await client.post(
            provider.base_url.rstrip("/") + "/chat/completions",
            json=_payload(template, provider),
            headers={"Content-Type": "application/json"},
        )
        latency_ms = int((datetime.now(tz=UTC) - start).total_seconds() * 1000)
        response.raise_for_status()
        body = response.json()
        choice = body["choices"][0]
        message = choice.get("message") or {}
        content = str(message.get("content") or "")
        reasoning = str(message.get("reasoning") or "")
        finish_reason = str(choice.get("finish_reason") or "")
        classification = classify_probe_outcome(
            expectation=template.expectation,
            content=content,
            reasoning=reasoning,
            finish_reason=finish_reason,
        )
        status_code = response.status_code
    except (httpx.HTTPError, ValueError) as exc:
        latency_ms = int((datetime.now(tz=UTC) - start).total_seconds() * 1000)
        content = ""
        reasoning = ""
        finish_reason = ""
        status_code = 0
        classification = OllamaProbeClassification(
            outcome="failure",
            content_shape="empty",
            visible_content=False,
            parseable_json=False,
            reasoning_present=False,
            finish_reason="",
            explanation=f"Transport- oder Parsing-Fehler: {exc}",
        )
    return ProbeResult(
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        template_name=template.name,
        repetition=repetition,
        latency_ms=latency_ms,
        status_code=status_code,
        finish_reason=finish_reason,
        content_preview=_preview(content),
        reasoning_preview=_preview(reasoning),
        classification=classification.as_dict(),
    )


async def _run_batch(settings: Settings) -> list[ProbeResult]:
    """Run the full live probe matrix with bounded concurrency."""

    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_SECONDS,
        read=READ_TIMEOUT_SECONDS,
        write=WRITE_TIMEOUT_SECONDS,
        pool=POOL_TIMEOUT_SECONDS,
    )
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results: list[ProbeResult] = []
    total = len(_provider_targets(settings)) * len(PROBE_TEMPLATES) * REPETITIONS_PER_TEMPLATE

    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        async def guarded_run(
            provider: ProviderTarget,
            template: ProbeTemplate,
            repetition: int,
        ) -> ProbeResult:
            async with semaphore:
                return await _run_probe(client, provider, template, repetition)

        tasks = [
            asyncio.create_task(guarded_run(provider, template, repetition))
            for provider in _provider_targets(settings)
            for template in PROBE_TEMPLATES
            for repetition in range(1, REPETITIONS_PER_TEMPLATE + 1)
        ]
        for completed_count, task in enumerate(asyncio.as_completed(tasks), start=1):
            results.append(await task)
            latest = results[-1]
            print(
                f"[{completed_count:03d}/{total}] {latest.provider_name}:{latest.template_name}"
                f"#{latest.repetition} -> {latest.classification['outcome']}",
                flush=True,
            )
    return results


def _build_report(results: list[ProbeResult]) -> dict[str, Any]:
    """Convert raw probe results into a stable JSON report with recommendations."""

    grouped: dict[str, list[OllamaProbeClassification]] = {}
    latencies: dict[str, list[int]] = {}
    for item in results:
        grouped.setdefault(item.provider_name, []).append(OllamaProbeClassification(**item.classification))
        latencies.setdefault(item.provider_name, []).append(item.latency_ms)

    provider_summaries: dict[str, dict[str, Any]] = {}
    recommendations: list[str] = []
    for provider_name, classifications in grouped.items():
        summary = summarize_provider_results(classifications)
        summary["avg_latency_ms"] = round(sum(latencies[provider_name]) / max(len(latencies[provider_name]), 1), 1)
        provider_summaries[provider_name] = summary
        recommendations.extend(recommend_provider_actions(provider_name=provider_name, summary=summary))

    return {
        "generated_at_utc": datetime.now(tz=UTC).isoformat(),
        "total_probes": len(results),
        "repetitions_per_template": REPETITIONS_PER_TEMPLATE,
        "templates": [asdict(template) for template in PROBE_TEMPLATES],
        "providers": provider_summaries,
        "recommendations": recommendations,
        "results": [asdict(item) for item in results],
    }


def _write_report(report: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    """Persist both a timestamped report and a stable latest.json copy."""

    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        target_dir = reports_dir
    except OSError:
        target_dir = Path("./reports")
        target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    timestamped_path = target_dir / f"ollama-batch-eval-{timestamp}.json"
    latest_path = target_dir / "ollama-batch-eval-latest.json"
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    timestamped_path.write_text(report_text, encoding="utf-8")
    latest_path.write_text(report_text, encoding="utf-8")
    return timestamped_path, latest_path


def _print_report_summary(report: dict[str, Any]) -> None:
    """Print a short operator-facing summary after the batch finished."""

    print("Ollama Batch Evaluation")
    print("=======================")
    print(f"Total probes: {report['total_probes']}")
    for provider_name, summary in report["providers"].items():
        print(
            f"- {provider_name}: success={summary['success']} degraded={summary['degraded']} "
            f"failure={summary['failure']} visible_json={summary['visible_json']} "
            f"avg_latency_ms={summary['avg_latency_ms']}"
        )
    print("Empfohlene Verbesserungen:")
    for item in report["recommendations"]:
        print(f"- {item}")


async def main() -> int:
    """Run the batch evaluation end-to-end and return a shell-friendly exit code."""

    settings = Settings()
    results = await _run_batch(settings)
    report = _build_report(results)
    timestamped_path, latest_path = _write_report(report, settings.reports_dir)
    _print_report_summary(report)
    print(f"Report gespeichert: {timestamped_path}")
    print(f"Aktueller Report: {latest_path}")
    hard_failures = sum(1 for item in report["results"] if item["classification"]["outcome"] == "failure")
    fail_on_hard_failure = os.getenv("OLLAMA_BATCH_FAIL_ON_HARD_FAILURE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return 1 if fail_on_hard_failure and hard_failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
