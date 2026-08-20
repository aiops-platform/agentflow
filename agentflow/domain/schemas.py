"""领域中间产物 schema（bug fix 专属，边界校验）。DESIGN.md §六。

统一约定：
- ``schema_version``：StateStore 读取时做版本兼容。
- ``summary`` + ``details`` 双分层：summary（≤500 token）默认传下游，details（完整证据）只落 StateStore。
- 证据类带 ``collected_at`` + ``ttl_seconds``（时效性）+ ``query_status``（ok/empty/error，负证据语义）。
- 失败节点用 ``FailedOutput`` 固定 fallback 结构。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class OutputBase(BaseModel):
    """所有 agent 结构化输出的公共基类。"""
    schema_version: str = SCHEMA_VERSION
    summary: str = ""                              # 核心结论，默认传下游
    details: dict[str, Any] = Field(default_factory=dict)  # 完整证据，只落 StateStore


class EvidenceBase(OutputBase):
    """证据类（日志/链路/指标/基础设施/知识）额外字段。"""
    query_status: Literal["ok", "empty", "error"] = "ok"
    collected_at: str | None = None
    ttl_seconds: int = 300
    source: str | None = None                      # 来源追溯


class FailedOutput(BaseModel):
    """失败节点的固定 fallback（on_failure: continue 时下游读到的结构）。"""
    schema_version: str = SCHEMA_VERSION
    status: Literal["failed"] = "failed"
    error_reason: str = ""
    output: None = None


class BugReport(OutputBase):
    """triage 输出。"""
    title: str = ""
    symptom_type: Literal["crash", "hang", "slow", "wrong_output"] | None = None
    service: str | None = None
    severity: str | None = None
    impact: str | None = None
    reproduction_steps: list[str] = Field(default_factory=list)
    time_window: str | None = None
    trace_id: str | None = None
    first_error: str | None = None


class LogEvidence(EvidenceBase):
    """log-analyst 输出。"""
    error_type: str | None = None
    error_stack: str | None = None
    instance: str | None = None
    time_window: str | None = None
    error_logs: list[dict[str, Any]] = Field(default_factory=list)


class TraceEvidence(EvidenceBase):
    """trace-analyst 输出（按 traceId 关联重建调用链）。"""
    trace_id: str | None = None
    call_sequence: list[dict[str, Any]] = Field(default_factory=list)  # 服务调用序列
    failing_service: str | None = None
    failing_span: str | None = None
    error: str | None = None


class MetricsEvidence(EvidenceBase):
    """metrics-analyst 输出（资源时序，得「因」）。"""
    metrics: list[dict[str, Any]] = Field(default_factory=list)        # 资源时序
    threshold_breaches: list[dict[str, Any]] = Field(default_factory=list)  # 阈值越界
    time_window: str | None = None


class InfraEvidence(EvidenceBase):
    """infra-locator 输出（K8s 对象状态）。"""
    pod_status: dict[str, Any] = Field(default_factory=dict)
    resource_usage: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    oom_killed: bool | None = None
    evicted: bool | None = None


class CodeLocation(OutputBase):
    """code-locator 输出。"""
    file_line: list[str] = Field(default_factory=list)     # 文件:行号
    call_chain: list[str] = Field(default_factory=list)    # 调用链
    suspects: list[str] = Field(default_factory=list)      # 可疑点


class KnowledgeEvidence(EvidenceBase):
    """knowledge-lookup 输出。"""
    similar_cases: list[dict[str, Any]] = Field(default_factory=list)
    root_cause_candidates: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    """单条根因假设。"""
    statement: str
    confidence: float = 0.0
    evidence: list[str] = Field(default_factory=list)


class RootCause(OutputBase):
    """root-cause 输出（假设列表 + 置信度 + 证据链 + 已排除假设）。"""
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    confidence: float = 0.0
    evidence_chain: list[str] = Field(default_factory=list)
    ruled_out: list[Hypothesis] = Field(default_factory=list)   # 负证据：已排除假设
    affected_services: list[str] = Field(default_factory=list)


class FixPlan(OutputBase):
    """fix-planner 输出。"""
    changes: list[dict[str, Any]] = Field(default_factory=list)   # 改动点
    risk_level: str | None = None
    impact: list[str] = Field(default_factory=list)               # 影响面
    test_requirements: list[str] = Field(default_factory=list)


class FixResult(OutputBase):
    """fix-implementer 输出。"""
    diff: str | None = None
    env_changes: list[str] = Field(default_factory=list)          # 环境变更（tester 重放）


class RemediationResult(OutputBase):
    """infra-remediator 输出。"""
    actions: list[dict[str, Any]] = Field(default_factory=list)
    diff: str | None = None
    rollback: str | None = None
    risk: str | None = None


class TestResult(OutputBase):
    """tester 输出。"""
    passed: bool = False
    pass_rate: float = 0.0
    failures: list[dict[str, Any]] = Field(default_factory=list)
    regressions: list[dict[str, Any]] = Field(default_factory=list)


class ReviewResult(OutputBase):
    """reviewer 输出。"""
    passed: bool = False
    issues: list[dict[str, Any]] = Field(default_factory=list)


class CommitResult(OutputBase):
    """committer 输出。"""
    commit: str | None = None
    pr_url: str | None = None


class Postmortem(OutputBase):
    """postmortem 输出。"""
    report: str = ""


class ServiceTopology(BaseModel):
    """预计算服务拓扑（DESIGN.md §4.5 / SCENARIOS.md §四）。"""
    schema_version: str = SCHEMA_VERSION
    services: dict[str, dict[str, str]] = Field(default_factory=dict)
    edges: list[dict[str, Any]] = Field(default_factory=list)


# 职能 agent → 输出 schema（供 executor 输出校验 / registry 使用）
AGENT_OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "triage": BugReport,
    "log-analyst": LogEvidence,
    "trace-analyst": TraceEvidence,
    "metrics-analyst": MetricsEvidence,
    "infra-locator": InfraEvidence,
    "code-locator": CodeLocation,
    "knowledge-lookup": KnowledgeEvidence,
    "root-cause": RootCause,
    "fix-planner": FixPlan,
    "fix-implementer": FixResult,
    "infra-remediator": RemediationResult,
    "tester": TestResult,
    "reviewer": ReviewResult,
    "committer": CommitResult,
    "postmortem": Postmortem,
}
