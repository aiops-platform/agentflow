"""领域中间产物 schema（bug fix 专属，边界校验）。DESIGN.md §六。

15 个 agent 的结构化输出 schema + 统一约定（schema_version / summary+details /
query_status / collected_at+ttl / 失败 fallback）。供节点输出校验与 StateStore 版本兼容。
"""
from agentflow.domain.schemas import (  # noqa: F401
    AGENT_OUTPUT_SCHEMAS,
    BugReport,
    CodeLocation,
    CommitResult,
    EvidenceBase,
    FailedOutput,
    FixPlan,
    FixResult,
    InfraEvidence,
    KnowledgeEvidence,
    LogEvidence,
    MetricsEvidence,
    OutputBase,
    Postmortem,
    RemediationResult,
    ReviewResult,
    RootCause,
    ServiceTopology,
    TestResult,
    TraceEvidence,
)

__all__ = [
    "AGENT_OUTPUT_SCHEMAS",
    "OutputBase",
    "EvidenceBase",
    "FailedOutput",
    "BugReport",
    "LogEvidence",
    "TraceEvidence",
    "MetricsEvidence",
    "InfraEvidence",
    "CodeLocation",
    "KnowledgeEvidence",
    "RootCause",
    "FixPlan",
    "FixResult",
    "RemediationResult",
    "TestResult",
    "ReviewResult",
    "CommitResult",
    "Postmortem",
    "ServiceTopology",
]
