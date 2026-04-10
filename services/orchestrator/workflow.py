"""
Purpose: LangGraph-based orchestration for the Feberdin multi-agent workflow.
Input/Output: The orchestrator loads persisted task state, routes work to specialist workers, and stores every result and approval gate.
Important invariants: The workflow is resumable, approvals are required before risky GitHub or deployment steps.
Each worker stays specialized instead of acting as an unbounded all-purpose agent.
How to debug: If a task stops unexpectedly, compare the last persisted status, resume target, and stored worker result.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from services.shared.agentic_lab.config import Settings
from services.shared.agentic_lab.logging_utils import TaskLoggerAdapter, configure_logging
from services.shared.agentic_lab.policy_service import RepositoryPolicyError, RepositoryPolicyService
from services.shared.agentic_lab.schemas import DeploymentConfig, SmokeCheck, TaskDetail, TaskStatus, WorkerRequest
from services.shared.agentic_lab.task_service import TaskService
from services.shared.agentic_lab.worker_client import WorkerCallError, call_worker
from services.shared.agentic_lab.worker_governance import WorkerGovernanceService


class WorkflowState(TypedDict, total=False):
    task_id: str
    goal: str
    repository: str
    repo_url: str | None
    local_repo_path: str
    base_branch: str
    branch_name: str | None
    current_status: str
    resume_target: str | None
    approval_required: bool
    approval_reason: str | None
    auto_deploy_staging: bool
    issue_number: int | None
    metadata: dict[str, Any]
    worker_results: dict[str, Any]
    risk_flags: list[str]
    test_commands: list[str]
    lint_commands: list[str]
    typing_commands: list[str]
    smoke_checks: list[SmokeCheck]
    deployment: DeploymentConfig | None
    pull_request_url: str | None
    latest_error: str | None


class WorkflowOrchestrator:
    """LangGraph wrapper that routes the Auftrag through the specialist worker team."""

    def __init__(
        self,
        settings: Settings,
        task_service: TaskService,
        policy_service: RepositoryPolicyService | None = None,
        worker_governance_service: WorkerGovernanceService | None = None,
    ) -> None:
        self.settings = settings
        self.task_service = task_service
        self.policy_service = policy_service or RepositoryPolicyService(settings)
        self.worker_governance_service = worker_governance_service or WorkerGovernanceService(settings)
        self.logger = configure_logging(settings.service_name, settings.log_level)
        self.graph = self._build_graph().compile()

    async def run_task(self, task_id: str) -> TaskDetail:
        """Load the latest persisted state and continue the workflow from the correct stage."""

        task = self.task_service.get_task(task_id)
        try:
            self.policy_service.assert_repository_allowed(task.repository)
        except RepositoryPolicyError as exc:
            failed = self.task_service.update_status(
                task_id,
                TaskStatus.FAILED,
                message="Repository access policy blocked the task.",
                details={"repository": task.repository, "error": str(exc)},
                latest_error=str(exc),
            )
            return failed
        await self.graph.ainvoke(self._task_to_state(task))
        return self.task_service.get_task(task_id)

    def _build_graph(self) -> StateGraph:
        graph = StateGraph(WorkflowState)

        graph.add_node("requirements", self._requirements_node)
        graph.add_node("cost", self._cost_node)
        graph.add_node("human_resources", self._human_resources_node)
        graph.add_node("research", self._research_node)
        graph.add_node("architecture", self._architecture_node)
        graph.add_node("data", self._data_node)
        graph.add_node("ux", self._ux_node)
        graph.add_node("coding", self._coding_node)
        graph.add_node("review", self._review_node)
        graph.add_node("testing", self._testing_node)
        graph.add_node("security", self._security_node)
        graph.add_node("validation", self._validation_node)
        graph.add_node("documentation", self._documentation_node)
        graph.add_node("github", self._github_node)
        graph.add_node("deploy", self._deploy_node)
        graph.add_node("qa", self._qa_node)
        graph.add_node("memory", self._memory_node)

        graph.add_conditional_edges(
            START,
            self._route_entry,
            {
                "requirements": "requirements",
                "cost": "cost",
                "human_resources": "human_resources",
                "research": "research",
                "architecture": "architecture",
                "data": "data",
                "ux": "ux",
                "coding": "coding",
                "review": "review",
                "testing": "testing",
                "security": "security",
                "validation": "validation",
                "documentation": "documentation",
                "github": "github",
                "deploy": "deploy",
                "qa": "qa",
                "memory": "memory",
                "stop": END,
            },
        )
        graph.add_conditional_edges("requirements", self._route_after_requirements, {"cost": "cost", "stop": END})
        graph.add_conditional_edges("cost", self._route_after_cost, {"human_resources": "human_resources", "stop": END})
        graph.add_conditional_edges(
            "human_resources",
            self._route_after_human_resources,
            {"research": "research", "stop": END},
        )
        graph.add_conditional_edges(
            "research",
            self._route_after_research,
            {"architecture": "architecture", "stop": END},
        )
        graph.add_conditional_edges(
            "architecture",
            self._route_after_architecture,
            {"data": "data", "ux": "ux", "coding": "coding", "stop": END},
        )
        graph.add_conditional_edges("data", self._route_after_data, {"ux": "ux", "coding": "coding", "stop": END})
        graph.add_conditional_edges("ux", self._route_after_ux, {"coding": "coding", "stop": END})
        graph.add_conditional_edges("coding", self._route_after_coding, {"review": "review", "stop": END})
        graph.add_conditional_edges("review", self._route_after_review, {"testing": "testing", "stop": END})
        graph.add_conditional_edges("testing", self._route_after_testing, {"security": "security", "stop": END})
        graph.add_conditional_edges(
            "security",
            self._route_after_security,
            {"validation": "validation", "stop": END},
        )
        graph.add_conditional_edges(
            "validation",
            self._route_after_validation,
            {"documentation": "documentation", "stop": END},
        )
        graph.add_conditional_edges(
            "documentation",
            self._route_after_documentation,
            {"github": "github", "stop": END},
        )
        graph.add_conditional_edges(
            "github",
            self._route_after_github,
            {"deploy": "deploy", "memory": "memory", "stop": END},
        )
        graph.add_conditional_edges("deploy", self._route_after_deploy, {"qa": "qa", "stop": END})
        graph.add_conditional_edges("qa", self._route_after_qa, {"memory": "memory", "stop": END})
        graph.add_conditional_edges("memory", self._route_after_memory, {"stop": END})
        return graph

    def _route_entry(self, state: WorkflowState) -> str:
        if state.get("current_status") in {TaskStatus.DONE.value, TaskStatus.FAILED.value}:
            return "stop"
        if state.get("approval_required"):
            return "stop"
        if state.get("resume_target"):
            return state["resume_target"]  # type: ignore[return-value]

        if state.get("current_status") == TaskStatus.PR_CREATED.value:
            return "deploy" if state.get("auto_deploy_staging", True) else "memory"

        mapping = {
            TaskStatus.NEW.value: "requirements",
            TaskStatus.REQUIREMENTS.value: "requirements",
            TaskStatus.RESOURCE_PLANNING.value: "cost",
            TaskStatus.RESEARCHING.value: "research",
            TaskStatus.ARCHITECTING.value: "architecture",
            TaskStatus.CODING.value: "coding",
            TaskStatus.REVIEWING.value: "review",
            TaskStatus.TESTING.value: "testing",
            TaskStatus.SECURITY_REVIEW.value: "security",
            TaskStatus.VALIDATING.value: "validation",
            TaskStatus.DOCUMENTING.value: "documentation",
            TaskStatus.STAGING_DEPLOYED.value: "qa",
            TaskStatus.QA_PENDING.value: "qa",
            TaskStatus.MEMORY_UPDATING.value: "memory",
        }
        return mapping.get(state.get("current_status", TaskStatus.NEW.value), "requirements")

    def _route_after_requirements(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "cost"

    def _route_after_cost(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "human_resources"

    def _route_after_human_resources(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "research"

    def _route_after_research(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "architecture"

    def _route_after_architecture(self, state: WorkflowState) -> str:
        if self._should_stop(state):
            return "stop"
        if self._specialist_requested(state, "data"):
            return "data"
        if self._specialist_requested(state, "ux"):
            return "ux"
        return "coding"

    def _route_after_data(self, state: WorkflowState) -> str:
        if self._should_stop(state):
            return "stop"
        return "ux" if self._specialist_requested(state, "ux") else "coding"

    def _route_after_ux(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "coding"

    def _route_after_coding(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "review"

    def _route_after_review(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "testing"

    def _route_after_testing(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "security"

    def _route_after_security(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "validation"

    def _route_after_validation(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "documentation"

    def _route_after_documentation(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "github"

    def _route_after_github(self, state: WorkflowState) -> str:
        if self._should_stop(state):
            return "stop"
        return "deploy" if state.get("auto_deploy_staging", True) else "memory"

    def _route_after_deploy(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "qa"

    def _route_after_qa(self, state: WorkflowState) -> str:
        return "stop" if self._should_stop(state) else "memory"

    def _route_after_memory(self, state: WorkflowState) -> str:
        return "stop"

    def _should_stop(self, state: WorkflowState) -> bool:
        return state.get("current_status") in {
            TaskStatus.APPROVAL_REQUIRED.value,
            TaskStatus.DONE.value,
            TaskStatus.FAILED.value,
        }

    def _specialist_requested(self, state: WorkflowState, worker_name: str) -> bool:
        hr_outputs = state.get("worker_results", {}).get("human_resources", {}).get("outputs", {})
        recommended = hr_outputs.get("recommended_workers", [])
        return worker_name in recommended

    def _service_url(self, worker_name: str) -> str:
        mapping = {
            "requirements": self.settings.requirements_worker_url,
            "cost": self.settings.cost_worker_url,
            "human_resources": self.settings.human_resources_worker_url,
            "research": self.settings.research_worker_url,
            "architecture": self.settings.architecture_worker_url,
            "data": self.settings.data_worker_url,
            "ux": self.settings.ux_worker_url,
            "coding": self.settings.coding_worker_url,
            "reviewer": self.settings.reviewer_worker_url,
            "tester": self.settings.test_worker_url,
            "security": self.settings.security_worker_url,
            "validation": self.settings.validation_worker_url,
            "documentation": self.settings.documentation_worker_url,
            "github": self.settings.github_worker_url,
            "deploy": self.settings.deploy_worker_url,
            "qa": self.settings.qa_worker_url,
            "memory": self.settings.memory_worker_url,
        }
        return mapping[worker_name]

    async def _requirements_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="requirements",
            service_url=self._service_url("requirements"),
            stage_status=TaskStatus.REQUIREMENTS,
        )

    async def _cost_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="cost",
            service_url=self._service_url("cost"),
            stage_status=TaskStatus.RESOURCE_PLANNING,
        )

    async def _human_resources_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="human_resources",
            service_url=self._service_url("human_resources"),
            stage_status=TaskStatus.RESOURCE_PLANNING,
        )

    async def _research_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="research",
            service_url=self._service_url("research"),
            stage_status=TaskStatus.RESEARCHING,
        )

    async def _architecture_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="architecture",
            service_url=self._service_url("architecture"),
            stage_status=TaskStatus.ARCHITECTING,
        )

    async def _data_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="data",
            service_url=self._service_url("data"),
            stage_status=TaskStatus.ARCHITECTING,
        )

    async def _ux_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="ux",
            service_url=self._service_url("ux"),
            stage_status=TaskStatus.ARCHITECTING,
        )

    async def _coding_node(self, state: WorkflowState) -> WorkflowState:
        if self._modification_approval_required(state):
            task = self.task_service.set_approval_required(
                state["task_id"],
                reason=self._modification_approval_reason(state),
                resume_target="coding",
                gate_name="repository-modification",
            )
            return self._task_to_state(task)
        return await self._run_stage(
            state=state,
            worker_name="coding",
            service_url=self._service_url("coding"),
            stage_status=TaskStatus.CODING,
        )

    async def _review_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="reviewer",
            service_url=self._service_url("reviewer"),
            stage_status=TaskStatus.REVIEWING,
        )

    async def _testing_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="tester",
            service_url=self._service_url("tester"),
            stage_status=TaskStatus.TESTING,
        )

    async def _security_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="security",
            service_url=self._service_url("security"),
            stage_status=TaskStatus.SECURITY_REVIEW,
        )

    async def _validation_node(self, state: WorkflowState) -> WorkflowState:
        return await self._run_stage(
            state=state,
            worker_name="validation",
            service_url=self._service_url("validation"),
            stage_status=TaskStatus.VALIDATING,
        )

    async def _documentation_node(self, state: WorkflowState) -> WorkflowState:
        stage_state = await self._run_stage(
            state=state,
            worker_name="documentation",
            service_url=self._service_url("documentation"),
            stage_status=TaskStatus.DOCUMENTING,
        )
        if stage_state.get("current_status") == TaskStatus.FAILED.value:
            return stage_state
        if self._approval_needed_before_github(stage_state):
            reason = self._approval_reason(stage_state)
            task = self.task_service.set_approval_required(
                stage_state["task_id"],
                reason=reason,
                resume_target="github",
                gate_name="risk-review",
            )
            return self._task_to_state(task)
        return stage_state

    async def _github_node(self, state: WorkflowState) -> WorkflowState:
        stage_state = await self._run_stage(
            state=state,
            worker_name="github",
            service_url=self._service_url("github"),
            stage_status=TaskStatus.DOCUMENTING,
        )
        if stage_state.get("current_status") == TaskStatus.FAILED.value:
            return stage_state

        github_result = stage_state.get("worker_results", {}).get("github", {})
        pull_request_url = github_result.get("outputs", {}).get("pull_request_url")
        if pull_request_url:
            self.task_service.set_pull_request(stage_state["task_id"], pull_request_url)
        task = self.task_service.update_status(
            stage_state["task_id"],
            TaskStatus.PR_CREATED,
            message="Draft pull request created successfully.",
            details={"pull_request_url": pull_request_url},
        )
        return self._task_to_state(task)

    async def _deploy_node(self, state: WorkflowState) -> WorkflowState:
        stage_state = await self._run_stage(
            state=state,
            worker_name="deploy",
            service_url=self._service_url("deploy"),
            stage_status=TaskStatus.PR_CREATED,
        )
        if stage_state.get("current_status") == TaskStatus.FAILED.value:
            return stage_state

        task = self.task_service.update_status(
            stage_state["task_id"],
            TaskStatus.STAGING_DEPLOYED,
            message="Staging deployment completed.",
            details={"staging_healthcheck_url": self.settings.staging_healthcheck_url},
        )
        return self._task_to_state(task)

    async def _qa_node(self, state: WorkflowState) -> WorkflowState:
        self.task_service.update_status(
            state["task_id"],
            TaskStatus.QA_PENDING,
            message="Smoke and health checks started.",
            details={},
        )
        return await self._run_stage(
            state={**state, "current_status": TaskStatus.QA_PENDING.value},
            worker_name="qa",
            service_url=self._service_url("qa"),
            stage_status=TaskStatus.QA_PENDING,
        )

    async def _memory_node(self, state: WorkflowState) -> WorkflowState:
        stage_state = await self._run_stage(
            state=state,
            worker_name="memory",
            service_url=self._service_url("memory"),
            stage_status=TaskStatus.MEMORY_UPDATING,
        )
        if stage_state.get("current_status") == TaskStatus.FAILED.value:
            return stage_state

        task = self.task_service.update_status(
            stage_state["task_id"],
            TaskStatus.DONE,
            message="Workflow completed successfully.",
            details={"pull_request_url": stage_state.get("pull_request_url")},
        )
        return self._task_to_state(task)

    def _approval_needed_before_github(self, state: WorkflowState) -> bool:
        review_result = state.get("worker_results", {}).get("reviewer", {})
        security_result = state.get("worker_results", {}).get("security", {})
        review_requires = review_result.get("requires_human_approval", False)
        security_requires = security_result.get("requires_human_approval", False)
        return bool(state.get("risk_flags") or review_requires or security_requires)

    def _modification_approval_required(self, state: WorkflowState) -> bool:
        metadata = state.get("metadata", {})
        return not metadata.get("allow_repository_modifications", False)

    def _modification_approval_reason(self, state: WorkflowState) -> str:
        metadata = state.get("metadata", {})
        worker_project_label = metadata.get("worker_project_label", "Feberdin local-multi-agent-company worker project")
        return (
            f"Explizite Freigabe erforderlich: Das erlaubte Repository `{state['repository']}` darf erst geändert werden, "
            f"wenn du bestätigst, dass das `{worker_project_label}` Änderungen vornehmen darf."
        )

    def _approval_reason(self, state: WorkflowState) -> str:
        review_reason = state.get("worker_results", {}).get("reviewer", {}).get("approval_reason")
        security_reason = state.get("worker_results", {}).get("security", {}).get("approval_reason")
        return review_reason or security_reason or "Risky changes detected before GitHub publication."

    async def _run_stage(
        self,
        *,
        state: WorkflowState,
        worker_name: str,
        service_url: str,
        stage_status: TaskStatus,
    ) -> WorkflowState:
        """Shared worker-stage behavior with logging, retries, persistence, and failure handling."""

        task_id = state["task_id"]
        logger = TaskLoggerAdapter(self.logger.logger, {"service": self.settings.service_name, "task_id": task_id})
        self.task_service.update_status(
            task_id,
            stage_status,
            message=f"{worker_name} stage started.",
            details={"service_url": service_url},
        )
        logger.info("Starting %s stage against %s", worker_name, service_url)

        worker_request = self._build_worker_request(state, worker_name)

        try:
            response = await call_worker(service_url, worker_request)
        except WorkerCallError as exc:
            logger.error("%s stage failed: %s", worker_name, exc)
            failed_task = self.task_service.update_status(
                task_id,
                TaskStatus.FAILED,
                message=f"{worker_name} stage failed.",
                details={"error": str(exc)},
                latest_error=str(exc),
            )
            return self._task_to_state(failed_task)

        annotated_response = self.worker_governance_service.annotate_worker_response(
            worker_name,
            worker_request,
            response,
        )
        self.worker_governance_service.register_worker_suggestions(
            worker_name=worker_name,
            request=worker_request,
            response=annotated_response,
        )
        self.task_service.store_worker_result(task_id, worker_name, annotated_response)
        logger.info("%s stage completed successfully", worker_name)

        if not annotated_response.success:
            error_text = "; ".join(annotated_response.errors) or f"{worker_name} reported failure."
            failed_task = self.task_service.update_status(
                task_id,
                TaskStatus.FAILED,
                message=f"{worker_name} stage reported a failure.",
                details={"errors": annotated_response.errors, "warnings": annotated_response.warnings},
                latest_error=error_text,
            )
            return self._task_to_state(failed_task)

        return self._task_to_state(self.task_service.get_task(task_id))

    def _build_worker_request(self, state: WorkflowState, worker_name: str) -> WorkerRequest:
        metadata = dict(state.get("metadata", {}))
        guidance_map = self.worker_governance_service.guidance_map()
        metadata["worker_guidance_map"] = guidance_map
        metadata["current_worker_guidance"] = guidance_map.get(worker_name)
        metadata["current_worker_name"] = worker_name
        return WorkerRequest(
            task_id=state["task_id"],
            goal=state["goal"],
            repository=state["repository"],
            repo_url=state.get("repo_url") or metadata.get("repo_url"),
            local_repo_path=state["local_repo_path"],
            base_branch=state["base_branch"],
            branch_name=state.get("branch_name"),
            issue_number=state.get("issue_number") or metadata.get("issue_number"),
            enable_web_research=metadata.get("enable_web_research", False),
            auto_deploy_staging=state.get("auto_deploy_staging", True),
            test_commands=state.get("test_commands", []),
            lint_commands=state.get("lint_commands", []),
            typing_commands=state.get("typing_commands", []),
            smoke_checks=state.get("smoke_checks", []),
            deployment=state.get("deployment"),
            metadata=metadata,
            prior_results=state.get("worker_results", {}),
        )

    def _task_to_state(self, task: TaskDetail) -> WorkflowState:
        metadata = dict(task.metadata)
        return WorkflowState(
            task_id=task.id,
            goal=task.goal,
            repository=task.repository,
            repo_url=task.repo_url,
            local_repo_path=task.local_repo_path,
            base_branch=task.base_branch,
            branch_name=task.branch_name,
            current_status=task.status.value,
            resume_target=task.resume_target,
            approval_required=task.approval_required,
            approval_reason=task.approval_reason,
            auto_deploy_staging=metadata.get("auto_deploy_staging", True),
            issue_number=metadata.get("issue_number"),
            metadata=metadata,
            worker_results=task.worker_results,
            risk_flags=task.risk_flags,
            test_commands=metadata.get("test_commands", []),
            lint_commands=metadata.get("lint_commands", []),
            typing_commands=metadata.get("typing_commands", []),
            smoke_checks=task.smoke_checks,
            deployment=task.deployment,
            pull_request_url=task.pull_request_url,
            latest_error=task.latest_error,
        )
