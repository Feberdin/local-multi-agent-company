"""
Purpose: Shared API and workflow contracts between the orchestrator, workers, and web UI.
Input/Output: FastAPI request and response models, task statuses, approval payloads, and worker result formats live here.
Important invariants: Task statuses must remain explicit and auditable, and worker contracts must stay stable across services.
How to debug: If a worker call fails validation, compare the payload with the models in this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
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


class ImprovementSuggestionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TrustedSourceCategory(StrEnum):
    OFFICIAL_DOCS = "official_docs"
    OFFICIAL_API = "official_api"
    OFFICIAL_REGISTRY = "official_registry"
    REPO_HOSTING = "repo_hosting"
    PACKAGE_REGISTRY = "package_registry"
    STANDARDS_DOCS = "standards_docs"


class TrustedSourceType(StrEnum):
    DOCS = "docs"
    API = "api"
    REGISTRY = "registry"
    REPO = "repo"
    STANDARD = "standard"


class PreferredAccess(StrEnum):
    API = "api"
    HTML = "html"
    MIXED = "mixed"


class SourceAuthType(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    TOKEN = "token"
    HEADER = "header"
    HEADER_TOKEN = "header_token"
    OAUTH = "oauth"


class SearchProviderType(StrEnum):
    SEARXNG = "searxng"
    BRAVE = "brave"


class SearchProviderHealthStatus(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class ResearchQuestionType(StrEnum):
    VERSION = "version"
    DEPENDENCY = "dependency"
    DOCS = "docs"
    API = "api"
    INSTALL = "install"
    STANDARD = "standard"
    RELEASE = "release"
    SECURITY = "security"
    GENERAL = "general"


class ResearchEcosystem(StrEnum):
    PYTHON = "python"
    NODE = "node"
    GITHUB = "github"
    WEB = "web"
    DOCKER = "docker"
    KUBERNETES = "kubernetes"
    RUST = "rust"
    GO = "go"
    LINUX = "linux"
    INFRA = "infra"
    GENERAL = "general"


def _utc_now() -> datetime:
    return datetime.now(UTC)


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


class WorkerGuidancePolicy(BaseModel):
    worker_name: str
    display_name: str
    enabled: bool = True
    role_summary: str
    operator_recommendations: list[str] = Field(default_factory=list)
    decision_preferences: list[str] = Field(default_factory=list)
    competence_boundary: str
    escalate_beyond_boundary: bool = True
    auto_submit_improvement_suggestions: bool = True
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class WorkerGuidanceRegistry(BaseModel):
    workers: list[WorkerGuidancePolicy] = Field(default_factory=list)


class ImprovementSuggestion(BaseModel):
    id: str
    worker_name: str
    task_id: str | None = None
    repository: str | None = None
    title: str
    summary: str
    rationale: str
    suggested_action: str
    impact: str = "medium"
    exceeds_competence_boundary: bool = False
    requires_ceo_approval: bool = False
    status: ImprovementSuggestionStatus = ImprovementSuggestionStatus.PENDING
    actor: str | None = None
    decision_note: str | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class ImprovementSuggestionRegistry(BaseModel):
    suggestions: list[ImprovementSuggestion] = Field(default_factory=list)


class ImprovementSuggestionDecisionRequest(BaseModel):
    decision: ImprovementSuggestionStatus
    actor: str = "ceo-dashboard"
    note: str | None = None


class WorkerDecisionNode(BaseModel):
    id: str
    label: str
    evidence: list[str] = Field(default_factory=list)
    decision: str | None = None
    outcome: str | None = None
    children: list[WorkerDecisionNode] = Field(default_factory=list)


class WorkerDecisionTree(BaseModel):
    worker_name: str
    title: str
    root: WorkerDecisionNode


class TrustedSource(BaseModel):
    id: str
    name: str
    domain: str
    category: TrustedSourceCategory
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=1000)
    source_type: TrustedSourceType
    preferred_access: PreferredAccess = PreferredAccess.MIXED
    base_url: str
    api_description: str | None = None
    auth_type: SourceAuthType = SourceAuthType.NONE
    auth_env_var: str | None = None
    rate_limit_notes: str | None = None
    usage_instructions: str | None = None
    allowed_paths: list[str] = Field(default_factory=list)
    deny_paths: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class TrustedSourceProfile(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool = True
    fallback_to_general_web_search: bool = True
    require_whitelist_match: bool = True
    minimum_source_count: int = Field(default=1, ge=0, le=50)
    require_official_source_for_versions: bool = True
    require_official_source_for_dependencies: bool = True
    require_official_source_for_api_reference: bool = True
    sources: list[TrustedSource] = Field(default_factory=list)


class TrustedSourceRegistry(BaseModel):
    active_profile_id: str
    profiles: list[TrustedSourceProfile] = Field(default_factory=list)


class SearchProvider(BaseModel):
    id: str
    name: str
    provider_type: SearchProviderType
    enabled: bool = False
    priority: int = Field(default=100, ge=0, le=1000)
    base_url: str
    search_path: str = "/search"
    method: str = "GET"
    auth_type: SourceAuthType = SourceAuthType.NONE
    auth_env_var: str | None = None
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    max_results: int = Field(default=10, ge=1, le=50)
    default_language: str = "en"
    default_categories: list[str] = Field(default_factory=list)
    safe_search: int = Field(default=1, ge=0, le=2)
    health_status: SearchProviderHealthStatus = SearchProviderHealthStatus.UNKNOWN
    last_checked_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)


class SearchProviderSettings(BaseModel):
    primary_web_search_provider: SearchProviderType = SearchProviderType.SEARXNG
    fallback_web_search_provider: SearchProviderType = SearchProviderType.BRAVE
    require_trusted_sources_first: bool = True
    allow_general_web_search_fallback: bool = True
    provider_host_allowlist: list[str] = Field(default_factory=list)
    providers: list[SearchProvider] = Field(default_factory=list)


class SearchResultItem(BaseModel):
    title: str
    url: str
    snippet: str = ""
    engine: str | None = None
    category: str | None = None
    result_type: str = "general_web_search"


class SourceTestResult(BaseModel):
    source_id: str
    status: str
    message: str
    request_preview: dict[str, Any] = Field(default_factory=dict)
    connectivity_url: str | None = None
    http_status: int | None = None
    matched: bool = False


class SearchProviderTestResult(BaseModel):
    provider_id: str
    status: SearchProviderHealthStatus
    message: str
    results: list[SearchResultItem] = Field(default_factory=list)
    checked_url: str | None = None


class SourceRoutingDecision(BaseModel):
    query: str
    inferred_question_type: ResearchQuestionType
    inferred_ecosystem: ResearchEcosystem
    active_profile_id: str
    trusted_matches: list[TrustedSource] = Field(default_factory=list)
    general_web_provider_sequence: list[SearchProviderType] = Field(default_factory=list)
    general_web_allowed: bool = False
    fallback_reason: str | None = None
    notes: list[str] = Field(default_factory=list)


class SourceRoutingRequest(BaseModel):
    query: str = Field(min_length=3, max_length=1000)
    ecosystem: ResearchEcosystem | None = None
    question_type: ResearchQuestionType | None = None


class TrustedSourceImportPayload(BaseModel):
    payload_json: str = Field(min_length=2)


class TrustedSourceProfileSelection(BaseModel):
    profile_id: str


class SourceTestRequest(BaseModel):
    source_id: str
    query: str = Field(default="latest stable release", min_length=3, max_length=400)


class SearchProviderTestRequest(BaseModel):
    provider_id: str
    query: str = Field(default="python packaging official docs", min_length=3, max_length=400)


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


WorkerDecisionNode.model_rebuild()
