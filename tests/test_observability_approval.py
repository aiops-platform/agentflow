"""M4 审批 + 观测测试。"""
from __future__ import annotations

import asyncio
import json

from agentflow.engine import ApprovalManager, DAGExecutor
from agentflow.observability import (Event, EventBus, EventType, LlmTraceSink,
                                     MetricsSink, RecordingSink)
from agentflow.opencode import NodeEvent, NodeEventType, TokenUsage, ToolCall
from agentflow.workflow.schema import EdgeDef, NodeDef, WorkflowDef


class FakeRuntime:
    def __init__(self, resp: dict | str = None):
        self.resp = resp if resp is not None else {"summary": "ok", "symptom_type": "crash"}

    async def run_node(self, agent, prompt, tools=None, session_id=None):
        text = self.resp if isinstance(self.resp, str) else json.dumps(self.resp, ensure_ascii=False)
        yield NodeEvent(type=NodeEventType.SESSION_CREATED, session_id="s")
        yield NodeEvent(type=NodeEventType.TEXT, text=text)
        yield NodeEvent(type=NodeEventType.TOOL_CALL,
                        tool=ToolCall(name="query_logs", input={"service": "x"}))
        yield NodeEvent(type=NodeEventType.STEP_FINISH, tokens=TokenUsage(total=10), cost=0.01)
        yield NodeEvent(type=NodeEventType.DONE)


def _wf(approve=None) -> WorkflowDef:
    node = NodeDef(agent="triage", params={})
    if approve:
        node = NodeDef(agent="triage", params={}, approve=approve)
    return WorkflowDef(name="w", nodes={"a": node}, edges=[])


# ── event_bus + sink ──

async def test_event_bus_fanout():
    bus = EventBus()
    rec = RecordingSink()
    lang = LlmTraceSink()
    met = MetricsSink()
    bus.subscribe(rec)
    bus.subscribe(lang)
    bus.subscribe(met)

    await bus.publish(Event(EventType.RUN_STARTED, "r1", data={"workflow": "w"}))
    await bus.publish(Event(EventType.NODE_STARTED, "r1", "a", data={"agent": "triage"}))
    await bus.publish(Event(EventType.GENERATION, "r1", "a",
                            data={"output": "ok", "tokens": {"total": 10}, "cost": 0.01}))
    await bus.publish(Event(EventType.NODE_FINISHED, "r1", "a",
                            data={"status": "done", "tokens": 10, "cost": 0.01, "duration": 1.2}))

    assert len(rec.events) == 4
    # Langfuse 映射：run→trace，node→generation，token/cost 映射
    assert lang.traces["r1"]["name"] == "w"
    assert lang.traces["r1"]["generations"]["a"]["usage"]["total"] == 10
    assert lang.traces["r1"]["generations"]["a"]["cost"] == 0.01
    assert lang.traces["r1"]["generations"]["a"]["status"] == "done"
    # Metrics 映射
    assert met.metrics["node_count"] == 1 and met.metrics["success"] == 1
    assert met.metrics["total_tokens"] == 10 and met.metrics["total_cost"] == 0.01
    print("  ✓ test_event_bus_fanout")


async def test_executor_emits_events():
    bus = EventBus()
    rec = RecordingSink()
    bus.subscribe(rec)
    ex = DAGExecutor(FakeRuntime(), event_bus=bus)
    result = await ex.run(_wf(), run_id="r1")

    types = [e.type for e in rec.events]
    assert types[0] is EventType.RUN_STARTED
    assert EventType.NODE_STARTED in types
    assert EventType.GENERATION in types
    assert EventType.NODE_FINISHED in types
    assert EventType.TOOL_CALL in types
    assert types[-1] is EventType.RUN_FINISHED
    gen = next(e for e in rec.events if e.type is EventType.GENERATION)
    assert gen.data["tokens"]["total"] == 10 and gen.data["cost"] == 0.01
    assert result.ok
    print("  ✓ test_executor_emits_events")


# ── 审批 ──

async def test_approval_manager_modes():
    # auto 直接通过
    auto = ApprovalManager(mode="auto")
    assert await auto.request("r", "a", {"trigger": "write"}) is True

    # manual + approve / reject
    man = ApprovalManager(mode="manual")
    task = asyncio.create_task(man.request("r", "b", {"trigger": "write"}))
    await asyncio.sleep(0.02)
    assert man.approve("r", "b")
    assert await task is True

    task = asyncio.create_task(man.request("r", "c", {"trigger": "write"}))
    await asyncio.sleep(0.02)
    assert man.reject("r", "c")
    assert await task is False

    # 超时 auto-deny
    short = ApprovalManager(mode="manual", timeout_seconds=0.05)
    assert await short.request("r", "d", {"trigger": "write"}) is False
    print("  ✓ test_approval_manager_modes")


async def test_executor_approval_reject():
    # manual 模式 + 驳回 → approval_rejected
    approval = ApprovalManager(mode="manual", timeout_seconds=5)
    ex = DAGExecutor(FakeRuntime(), approval=approval)
    task = asyncio.create_task(ex.run(_wf(approve="write"), run_id="r1"))

    while not approval.pending():
        await asyncio.sleep(0.01)
    approval.reject("r1", "a")
    result = await task

    assert result.nodes["a"].status == "failed"
    assert result.nodes["a"].error == "approval_rejected"
    assert not result.ok
    print("  ✓ test_executor_approval_reject")


async def test_executor_approval_auto():
    ex = DAGExecutor(FakeRuntime(), approval=ApprovalManager(mode="auto"))
    result = await ex.run(_wf(approve="write"), run_id="r1")
    assert result.nodes["a"].status == "done" and result.ok
    print("  ✓ test_executor_approval_auto")


async def main() -> None:
    await test_event_bus_fanout()
    await test_executor_emits_events()
    await test_approval_manager_modes()
    await test_executor_approval_reject()
    await test_executor_approval_auto()
    print("\nALL M4 OBSERVABILITY/APPROVAL TESTS PASS ✅")


if __name__ == "__main__":
    asyncio.run(main())
