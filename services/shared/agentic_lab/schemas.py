"""
Purpose: Shared API and workflow contracts between the orchestrator, workers, and web UI.
Input/Output: FastAPI request and response models, task statuses, approval payloads, and worker result formats live here.
Important invariants: Task statuses must remain explicit and auditable, and worker contracts must stay stable across services.
How to debug: If a worker call fails validation, compare the payload with the models in this module.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    NEW = "NEW"
    REQUIREMENTS = "REQUIREMENTS"
    RESOURCE_PLANNING = "RESOURCE_PLANNING"
    RESEARCHING = "RESEARCHING"
    ARCHITECTING = "ARCHITECTING"
    CODING = "CODING"
    REVIEWING = "REVIEWING"
    TESTING = "TESTING"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    VALIDATING = "VALIDATING"
    DOCUMENTING = "DOCUMENTING"
    PR_CREATED = "PR_CREATED"
    STAGING_DEPLOYED = "STAGING_DEPLOYED"
    QA_PENDING = "QA_PENDING"
    MEMORY_UPDATING = "MEMORY_UPDATING"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DONE = "DONE"
    FAILED = "FAILED"


class ApprovalDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class Artifact(BaseModel):
    name: str
    path: str
    description: str


class SmokeCheck(BaseModel):
    name: str
    url: str
    expected_status: int = 200
    expected_substring: str | None = None


class DeploymentConfig(BaseModel):
    strategy: str = "docker_compose"
    target: str = "staging"
    project_dir: str | None = None
    compose_file: str | None = None
    healthcheck_url: str | None = None
    service_name: str | None = None


class TaskCreateRequest(BaseModel):
    goal: str = Field(min_length=10, max_length=4000)
    repository: str
    local_repo_path: str | None = None
    repo_url: str | None = None
    base_branch: str = "main"
    issue_number: int | None = None
    enable_web_research: bool = False
    allow_repository_modifications: bool = False
    auto_deploy_staging: bool | None = None
    test_commands: list[str] = Field(default_factory=list)
    lint_commands: list[str] = Field(default_factory=list)
    typing_commands: list[str] = Field(default_factory=list)
    smoke_checks: list[SmokeCheck] = Field(default_factory=list)
    deployment: DeploymentConfig | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskSummary(BaseModel):
    id: str
    goal: str
    repository: str
    repo_url: str | None = None
    local_repo_path: str
    base_branch: str
    branch_name: str | None = None
    status: TaskStatus
    resume_target: str | None = None
    current_approval_gate_name: str | None = None
    approval_required: bool = False
    approval_reason: str | None = None
    allow_repository_modifications: bool = False
    pull_request_url: str | None = None
    latest_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class TaskEventResponse(BaseModel):
    id: int
    task_id: str
    level: str
    stage: str
    message: str
    details: dict[str, Any]
    created_at: datetime


class ApprovalResponse(BaseModel):
    gate_name: str
    decision: ApprovalDecision
    reason: str | None = None
    actor: str
    created_at: datetime


class TaskDetail(TaskSummary):
    worker_results: dict[str, Any] = Field(default_factory=dict)
    risk_flags: list[str] = Field(default_factory=list)
    events: list[TaskEventResponse] = Field(default_factory=list)
    approvals: list[ApprovalResponse] = Field(default_factory=list)
    smoke_checks: list[SmokeCheck] = Field(default_factory=list)
    deployment: DeploymentConfig | None = None


class ApprovalRequest(BaseModel):
    gate_name: str
    decision: ApprovalDecision
    actor: str = "human-operator"
    reason: str | None = None


class RepositoryAccessSettings(BaseModel):
    allowed_repositories: list[str] = Field(default_factory=list)


class WorkerRequest(BaseModel):
    task_id: str
    goal: str
    repository: str
    repo_url: str | None = None
    local_repo_path: str
    base_branch: str
    branch_name: str | None = None
    issue_number: int | None = None
    enable_web_research: bool = False
    auto_deploy_staging: bool = True
    test_commands: list[str] = Field(default_factory=list)
    lint_commands: list[str] = Field(default_factory=list)
    typing_commands: list[str] = Field(default_factory=list)
    smoke_checks: list[SmokeCheck] = Field(default_factory=list)
    deployment: DeploymentConfig | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    prior_results: dict[str, Any] = Field(default_factory=dict)


class WorkerResponse(BaseModel):
    worker: str
    summary: str
    success: bool = True
    outputs: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[Artifact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    requires_human_approval: bool = False
    approval_reason: str | None = None


class HealthResponse(BaseModel):
    service: str
    status: str = "ok"
