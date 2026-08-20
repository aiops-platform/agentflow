"""集成测试（L2 workflow，SCENARIOS §5.2/§5.3）。

用 mock-llm（按 agent 返回脚本化 schema 输出）+ 真实 AgentRegistry + DAGExecutor，跑完整的
场景 workflow，验证：编排调度、JSONPath 传参、schema 校验、when 条件边、审批、观测事件，
并断言根因/症状/影响服务与 golden 一致。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from agentflow.agents import AgentRegistry
from agentflow.config import AGENTS_DIR
from agentflow.engine import DAGExecutor
from agentflow.engine.approval import ApprovalManager
from agentflow.engine.state import InMemoryStore
from agentflow.observability import EventBus, EventType, RecordingSink
from agentflow.opencode import NodeEvent, NodeEventType, TokenUsage
from agentflow.workflow.parser import parse


class MockLlm:
    """mock-llm：按 agent 名返回脚本化 schema 输出（模拟真实 LLM 结构化输出）。"""

    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.calls: list[str] = []

    async def run_node(self, agent, prompt, tools=None):
        self.calls.append(agent)
        resp = self.responses[agent]
        yield NodeEvent(type=NodeEventType.SESSION_CREATED, session_id=f"ses_{agent}")
        yield NodeEvent(type=NodeEventType.TEXT, text=json.dumps(resp, ensure_ascii=False))
        yield NodeEvent(type=NodeEventType.STEP_FINISH, tokens=TokenUsage(total=100))
        yield NodeEvent(type=NodeEventType.DONE)


# ── 场景 1：基础设施故障（CPU/磁盘 100%）──
SCENARIO1 = {
    "triage": {"summary": "报价单打印失败", "symptom_type": "crash", "service": "order-service", "severity": "P1"},
    "log-analyst": {"summary": "IOException: No space left on device", "error_type": "IOException",
                    "error_stack": "No space left on device", "query_status": "ok"},
    "trace-analyst": {"summary": "generateQuotation span 超时", "failing_service": "order-service", "query_status": "ok"},
    "metrics-analyst": {"summary": "CPU 100% + 磁盘 100%", "query_status": "ok",
                        "threshold_breaches": [{"metric": "cpu_usage_percent", "value": 100}]},
    "infra-locator": {"summary": "PVC 满 + CPU throttled", "query_status": "ok"},
    "code-locator": {"summary": "generateFile 临时文件未清理", "file_line": ["QuotationService.java:128"]},
    "knowledge-lookup": {"summary": "磁盘满→文件生成失败修复模式", "query_status": "ok"},
    "root-cause": {"summary": "磁盘 100% 导致文件生成失败", "confidence": 0.9,
                   "hypotheses": [{"statement": "磁盘满", "confidence": 0.9}],
                   "affected_services": ["order-service"]},
    "fix-planner": {"summary": "止血+根治两层", "risk_level": "high"},
    "fix-implementer": {"summary": "修 temp leak", "diff": "---", "env_changes": []},
    "infra-remediator": {"summary": "scale + 清盘", "actions": [], "rollback": "..."},
    "tester": {"summary": "回归通过", "passed": True, "pass_rate": 1.0},
    "reviewer": {"summary": "审查通过", "passed": True},
    "committer": {"summary": "提交 PR", "commit": "abc123", "pr_url": "https://github.com/x/1"},
    "postmortem": {"summary": "复盘", "report": "..."},
}

# ── 场景 2：跨服务代码故障（fin 缺参，基础设施为负证据）──
SCENARIO2 = {
    "triage": {"summary": "结账无响应", "symptom_type": "hang", "service": "order-service"},
    "log-analyst": {"summary": "warranty 有 BindingException: fin not found", "error_type": "BindingException", "query_status": "ok"},
    "trace-analyst": {"summary": "故障 span 在 warranty-service", "failing_service": "warranty-service", "query_status": "ok"},
    "metrics-analyst": {"summary": "CPU/磁盘正常", "query_status": "ok"},  # 负证据
    "infra-locator": {"summary": "pod 全部 Running，无重启", "query_status": "ok"},  # 负证据
    "code-locator": {"summary": "checkWarranty 漏传 fin + 空 catch", "file_line": ["WarrantyServiceImpl.java:88"]},
    "knowledge-lookup": {"summary": "跨服务参数缺失模式", "query_status": "ok"},
    "root-cause": {"summary": "fin 缺参 + 吞异常", "confidence": 0.9,
                   "hypotheses": [{"statement": "fin 漏传", "confidence": 0.9}],
                   "affected_services": ["warranty-service"],
                   "ruled_out": [{"statement": "基础设施", "confidence": 0.05}]},
    "fix-planner": {"summary": "传 fin + 修空 catch + Feign 超时", "risk_level": "low"},
    "fix-implementer": {"summary": "跨两仓库修复", "diff": "---", "env_changes": []},
    "tester": {"summary": "结账恢复", "passed": True, "pass_rate": 1.0},
    "reviewer": {"summary": "审查通过", "passed": True},
    "committer": {"summary": "warranty PR", "commit": "def456", "pr_url": "https://github.com/x/2"},
    "postmortem": {"summary": "复盘", "report": "..."},
}


def _executor(llm: MockLlm, recorder: RecordingSink):
    registry = AgentRegistry(AGENTS_DIR).load()
    bus = EventBus()
    bus.subscribe(recorder)
    approval = ApprovalManager(mode="auto")
    return DAGExecutor(llm, store=InMemoryStore(), registry=registry,
                       event_bus=bus, approval=approval)


async def test_scenario1_integration():
    rec = RecordingSink()
    ex = _executor(MockLlm(SCENARIO1), rec)
    wf = parse("examples/order-service-quotation-print-fail.yaml")
    result = await ex.run(wf, inputs={"repo": "https://github.com/x/order", "bug_report": {"title": "x"}})

    assert result.ok, result.nodes
    # 根因：infra 类型 + order-service + 置信度
    rca = result.nodes["rca"].output
    assert "order-service" in rca["affected_services"]
    assert rca["confidence"] >= 0.8
    # 症状分类（triage）
    assert result.nodes["triage"].output["symptom_type"] == "crash"
    # when 条件边：tester.passed=true → review/commit 运行
    assert result.nodes["review"].status == "done"
    assert result.nodes["commit"].status == "done"
    # 观测事件
    types = [e.type for e in rec.events]
    assert types[0] is EventType.RUN_STARTED and types[-1] is EventType.RUN_FINISHED
    assert EventType.NODE_FINISHED in types
    print("  ✓ test_scenario1_integration")


async def test_scenario2_integration():
    rec = RecordingSink()
    ex = _executor(MockLlm(SCENARIO2), rec)
    wf = parse("examples/order-service-checkout-no-response.yaml")
    result = await ex.run(wf, inputs={"repo": "https://github.com/x/order", "bug_report": {"title": "x"}})

    assert result.ok, result.nodes
    rca = result.nodes["rca"].output
    # 根因在下游 warranty-service（非 ticket 报告的 order-service）
    assert "warranty-service" in rca["affected_services"]
    assert rca["confidence"] >= 0.8
    assert result.nodes["triage"].output["symptom_type"] == "hang"
    # 场景2 无 infra-remediator 节点
    assert "remediate" not in result.nodes
    print("  ✓ test_scenario2_integration")


async def main() -> None:
    await test_scenario1_integration()
    await test_scenario2_integration()
    print("\nALL INTEGRATION TESTS PASS ✅")


if __name__ == "__main__":
    asyncio.run(main())
