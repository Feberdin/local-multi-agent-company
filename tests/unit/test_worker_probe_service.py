"""
Purpose: Verify that the worker probe service runs short model probes, persists readable results,
and survives restarts cleanly.
Input/Output: Tests feed a fake LLM into the service and inspect the persisted run registry
after start, execution, and orphan recovery.
Important invariants: Probes must stay side-effect free, preserve worker ordering,
and never leave stale running states after a restart.
How to debug: If this fails, inspect `services/shared/agentic_lab/worker_probe_service.py`
and the generated JSON registry in the temp data directory.
"""

from __future__ import annotations

from pathlib import Path

from services.shared.agentic_lab.config import get_settings
from services.shared.agentic_lab.llm import LLMError
from services.shared.agentic_lab.schemas import WorkerProbeMode, WorkerProbeRunStatus, WorkerProbeStartRequest
from services.shared.agentic_lab.worker_probe_service import PROBE_WORKERS, WorkerProbeService


class _FakeLLM:
    def __init__(self) -> None:
        self.prompt_log: dict[str, tuple[str, str]] = {}

    async def complete_json_with_trace(
        self,
        *,
        system_prompt,
        user_prompt,
        worker_name,
        required_keys=None,
        max_tokens=None,
    ):
        del required_keys, max_tokens
        self.prompt_log[worker_name] = (system_prompt, user_prompt)
        base_payload = {"summary": f"{worker_name} summary"}
        if worker_name == "requirements":
            base_payload.update(
                {
                    "requirements": ["Antwort sichtbar machen"],
                    "wishes": [],
                    "assumptions": ["Synthetischer Probe-Kontext."],
                    "risks": ["Keine echten Repo-Daten."],
                    "acceptance_criteria": ["Antwort ist lesbar."],
                    "open_questions": ["Keine"],
                    "recommended_workers": ["research"],
                }
            )
        elif worker_name == "architecture":
            base_payload.update(
                {
                    "components": ["web-ui", "orchestrator"],
                    "responsibilities": {"web-ui": "anzeigen"},
                    "data_flows": ["UI -> API"],
                    "module_boundaries": ["Services bleiben getrennt."],
                    "deployment_strategy": ["Nur lesen."],
                    "logging_strategy": ["Knappe Logs."],
                    "implementation_plan": ["Klein anfangen."],
                    "test_strategy": ["Unit-Test."],
                    "risks": ["Keine"],
                    "approval_gates": ["Keine"],
                    "touched_areas": ["services/web_ui/app.py"],
                }
            )
        elif worker_name == "coding":
            base_payload.update(
                {
                    "operations": [
                        {
                            "type": "replace_lines",
                            "path": "services/web_ui/app.py",
                            "start_line": 10,
                            "end_line": 12,
                            "content": "print('probe')",
                        }
                    ]
                }
            )
        elif worker_name == "reviewer":
            base_payload.update({"findings": ["Kleiner Review-Hinweis."], "warnings": []})
        elif worker_name == "security":
            base_payload.update(
                {
                    "findings": ["Debug-Header pruefen."],
                    "residual_risks": ["Header-Leak"],
                    "requires_human_approval": False,
                    "approval_reason": "",
                }
            )
        elif worker_name == "validation":
            base_payload.update(
                {
                    "fulfilled": ["Antwort gesammelt."],
                    "partially_verified": [],
                    "unverified": [],
                    "residual_risks": [],
                    "release_readiness": "benchmark",
                    "recommendation": "Anzeige pruefen.",
                }
            )
        else:
            raise AssertionError(f"Unexpected JSON worker: {worker_name}")

        return base_payload, {
            "provider": "mistral" if worker_name != "research" else "qwen",
            "model_name": "demo-model",
            "base_url": "http://llm.local/v1",
            "used_fallback": worker_name == "coding",
            "repair_pass_used": worker_name == "coding",
        }

    async def complete_with_trace(
        self,
        *,
        system_prompt,
        user_prompt,
        worker_name,
        max_tokens=None,
        temperature=None,
    ):
        del max_tokens, temperature
        self.prompt_log[worker_name] = (system_prompt, user_prompt)
        return (
            f"{worker_name} antwortet mit einer kurzen, lesbaren Probe.",
            {
                "provider": "qwen" if worker_name == "research" else "mistral",
                "model_name": "demo-model",
                "base_url": "http://llm.local/v1",
                "used_fallback": False,
                "repair_pass_used": False,
            },
        )


def _prepare_settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("STAGING_STACK_ROOT", str(tmp_path / "staging-stacks"))
    monkeypatch.setenv("MISTRAL_BASE_URL", "http://mistral.local/v1")
    monkeypatch.setenv("MISTRAL_MODEL_NAME", "mistral-small3.2:latest")
    monkeypatch.setenv("QWEN_BASE_URL", "http://qwen.local/v1")
    monkeypatch.setenv("QWEN_MODEL_NAME", "qwen3.5:35b-a3b")
    get_settings.cache_clear()
    return get_settings()


async def test_worker_probe_service_executes_and_persists_results(tmp_path, monkeypatch) -> None:
    settings = _prepare_settings(tmp_path, monkeypatch)
    storage_path = Path(settings.data_dir) / "worker_probe_runs.json"
    fake_llm = _FakeLLM()
    service = WorkerProbeService(settings=settings, llm=fake_llm, storage_path=storage_path)

    run = await service.start_run(
        WorkerProbeStartRequest(probe_goal="Teste kurze Modellantworten fuer eine observability-lastige Aufgabe.")
    )
    finished = await service.execute_run(run.id)

    assert finished.status == WorkerProbeRunStatus.COMPLETED
    assert finished.probe_mode is WorkerProbeMode.FULL
    assert finished.total_workers == len(PROBE_WORKERS)
    assert finished.completed_workers == len(PROBE_WORKERS)
    assert finished.failed_workers == 0
    assert [item.worker_name for item in finished.results] == list(PROBE_WORKERS)
    coding_result = next(item for item in finished.results if item.worker_name == "coding")
    assert coding_result.used_fallback is True
    assert coding_result.repair_pass_used is True
    assert storage_path.exists() is True


async def test_worker_probe_service_supports_ok_contract_smoke_runs(tmp_path, monkeypatch) -> None:
    settings = _prepare_settings(tmp_path, monkeypatch)
    storage_path = Path(settings.data_dir) / "worker_probe_runs.json"
    fake_llm = _FakeLLM()
    service = WorkerProbeService(settings=settings, llm=fake_llm, storage_path=storage_path)

    run = await service.start_run(
        WorkerProbeStartRequest(
            probe_goal="Leerer OK-Kurztest fuer alle Worker-Vertraege ohne Repository-Aenderungen.",
            probe_mode=WorkerProbeMode.OK_CONTRACT,
        )
    )
    finished = await service.execute_run(run.id)

    assert finished.status == WorkerProbeRunStatus.COMPLETED
    assert finished.probe_mode is WorkerProbeMode.OK_CONTRACT
    assert all(item.status == "ok" for item in finished.results)
    coding_system_prompt, coding_user_prompt = fake_llm.prompt_log["coding"]
    assert "contract smoke test" in coding_user_prompt.lower()
    assert "blocking_reason" in coding_system_prompt
    architecture_system_prompt, architecture_user_prompt = fake_llm.prompt_log["architecture"]
    assert '"logging_strategy":"OK"' in architecture_system_prompt
    assert "do not omit any key" in architecture_user_prompt.lower()
    research_system_prompt, _research_user_prompt = fake_llm.prompt_log["research"]
    assert "reply with exactly `ok`" in research_system_prompt.lower()


async def test_worker_probe_service_runs_only_selected_workers_in_canonical_order(tmp_path, monkeypatch) -> None:
    settings = _prepare_settings(tmp_path, monkeypatch)
    storage_path = Path(settings.data_dir) / "worker_probe_runs.json"
    fake_llm = _FakeLLM()
    service = WorkerProbeService(settings=settings, llm=fake_llm, storage_path=storage_path)

    run = await service.start_run(
        WorkerProbeStartRequest(
            probe_goal="Pruefe nur Architektur und Code nach einem gezielten Fix.",
            selected_workers=["coding", "architecture", "coding"],
            focus_paths=["tests/unit/test_worker_probe_service.py", "services/web_ui/app.py"],
        )
    )
    finished = await service.execute_run(run.id)

    assert finished.status == WorkerProbeRunStatus.COMPLETED
    assert finished.selected_workers == ["architecture", "coding"]
    assert finished.focus_paths == ["tests/unit/test_worker_probe_service.py", "services/web_ui/app.py"]
    assert finished.total_workers == 2
    assert finished.completed_workers == 2
    assert [item.worker_name for item in finished.results] == ["architecture", "coding"]
    assert set(fake_llm.prompt_log) == {"architecture", "coding"}
    architecture_system_prompt, architecture_user_prompt = fake_llm.prompt_log["architecture"]
    assert "tests/unit/test_worker_probe_service.py" in architecture_user_prompt
    assert "services/web_ui/app.py" in architecture_user_prompt
    coding_system_prompt, coding_user_prompt = fake_llm.prompt_log["coding"]
    assert "replace_lines" in coding_system_prompt
    assert "`file`, `operation`, or `description`" in coding_system_prompt
    assert "Verfuegbarer Dateikontext" in coding_user_prompt
    assert "do not claim missing file access" in coding_user_prompt.lower()
    assert "Letzter Commit-Diff" in coding_user_prompt or "Aktueller Auszug" in coding_user_prompt


class _FailingLLM:
    async def complete_json_with_trace(self, **kwargs):
        del kwargs
        raise LLMError(
            "Model did not return valid JSON for `coding`. "
            "Provider `mistral` with model `mistral-small3.2:latest` returned JSON that did not satisfy the `edit_plan` contract. "
            "Provider `mistral` JSON-repair attempt still returned no valid JSON."
        )

    async def complete_with_trace(self, **kwargs):
        del kwargs
        raise AssertionError("Text probe path should not be used in this test.")


async def test_worker_probe_service_surfaces_fallback_and_repair_details_on_failure(tmp_path, monkeypatch) -> None:
    settings = _prepare_settings(tmp_path, monkeypatch)
    storage_path = Path(settings.data_dir) / "worker_probe_runs.json"
    service = WorkerProbeService(settings=settings, llm=_FailingLLM(), storage_path=storage_path)

    run = await service.start_run(
        WorkerProbeStartRequest(
            probe_goal="Pruefe nur Code nach einem gezielten Fix.",
            selected_workers=["coding"],
            focus_paths=["services/shared/agentic_lab/worker_probe_service.py"],
        )
    )
    finished = await service.execute_run(run.id)

    assert finished.status == WorkerProbeRunStatus.COMPLETED
    result = finished.results[0]
    assert result.status == "failed"
    assert result.used_fallback is True
    assert result.repair_pass_used is True
    assert "JSON-repair attempt" in result.response_text


async def test_worker_probe_service_marks_running_runs_as_failed_on_resume(tmp_path, monkeypatch) -> None:
    settings = _prepare_settings(tmp_path, monkeypatch)
    storage_path = Path(settings.data_dir) / "worker_probe_runs.json"
    service = WorkerProbeService(settings=settings, llm=_FakeLLM(), storage_path=storage_path)

    run = await service.start_run(
        WorkerProbeStartRequest(probe_goal="Teste Restart-Verhalten der Modell-Probe.")
    )
    await service._update_run(run.id, status=WorkerProbeRunStatus.RUNNING, active_worker_name="requirements")

    service.resume_orphaned_runs()
    recovered = service.load_registry().runs[0]

    assert recovered.status == WorkerProbeRunStatus.FAILED
    assert recovered.active_worker_name is None
    assert "unterbrochen" in recovered.errors[-1]
