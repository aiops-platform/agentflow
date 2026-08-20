"""M2 收尾测试：AgentRegistry 加载 + SQLite 断点续跑。"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from agentflow.agents import AgentRegistry
from agentflow.config import AGENTS_DIR
from agentflow.engine import DAGExecutor
from agentflow.engine.state import InMemoryStore, SqliteStore
from agentflow.opencode import NodeEvent, NodeEventType, TokenUsage
from agentflow.workflow.schema import EdgeDef, NodeDef, WorkflowDef


# ── AgentRegistry ──

def test_registry_loads_all_agents():
    reg = AgentRegistry(AGENTS_DIR).load()
    assert len(reg) == 15, f"应 15 个 agent，实际 {len(reg)}: {reg.names()}"
    triage = reg.get("triage")
    assert triage.system_prompt.strip()
    assert triage.output_schema.__name__ == "BugReport"
    assert triage.permissions.get("edit") == "deny"
    assert "query_logs" in reg.get("log-analyst").tools
    assert "query_metrics" in reg.get("metrics-analyst").tools
    assert "root-cause" in reg
    print("  ✓ test_registry_loads_all_agents")


# ── 断点续跑 ──

class FakeRuntime:
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[str] = []

    async def run_node(self, agent: str, prompt: str, tools: list[str] | None = None):
        self.calls.append(agent)
        resp = self.responses[agent]
        if resp == "__ERROR__":
            yield NodeEvent(type=NodeEventType.ERROR, error="boom")
            return
        text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
        yield NodeEvent(type=NodeEventType.SESSION_CREATED, session_id=f"ses_{agent}")
        yield NodeEvent(type=NodeEventType.TEXT, text=text)
        yield NodeEvent(type=NodeEventType.STEP_FINISH, tokens=TokenUsage(total=10))
        yield NodeEvent(type=NodeEventType.DONE)


def _wf() -> WorkflowDef:
    return WorkflowDef(name="resume", nodes={
        "a": NodeDef(agent="triage", params={}),
        "b": NodeDef(agent="log-analyst", params={"x": "$.nodes.a.output"}),
        "c": NodeDef(agent="root-cause", params={"x": "$.nodes.b.output"}),
    }, edges=[EdgeDef(from_="a", to="b"), EdgeDef(from_="b", to="c")])


async def test_resume_skips_done_reruns_failed():
    store = InMemoryStore()
    triage_out = {"summary": "报价单打印失败", "symptom_type": "crash"}
    logs_ok = {"summary": "IOException: No space left on device", "error_type": "IOException"}
    rca_out = {"summary": "磁盘满", "confidence": 0.9}

    # 第 1 次：b 失败 → c cancelled
    rt1 = FakeRuntime({"triage": triage_out, "log-analyst": "__ERROR__", "root-cause": rca_out})
    r1 = await DAGExecutor(rt1, store=store).run(_wf(), run_id="run_test")
    assert r1.nodes["a"].status == "done"
    assert r1.nodes["b"].status == "failed"
    assert r1.nodes["c"].status == "cancelled"

    # 第 2 次：resume，b 修好 → a 跳过、b/c 重跑
    rt2 = FakeRuntime({"triage": triage_out, "log-analyst": logs_ok, "root-cause": rca_out})
    r2 = await DAGExecutor(rt2, store=store).run(_wf(), run_id="run_test", resume=True)

    assert "triage" not in rt2.calls, f"a 已 done 应跳过，实际 calls={rt2.calls}"
    assert rt2.calls == ["log-analyst", "root-cause"], rt2.calls
    assert r2.nodes["a"].status == "done"
    assert r2.nodes["b"].status == "done"
    assert r2.nodes["c"].status == "done"
    assert r2.ok
    # 复用 done 节点的缓存 output
    assert r2.context["nodes"]["a"]["output"]["summary"] == "报价单打印失败"
    print("  ✓ test_resume_skips_done_reruns_failed")


def test_sqlite_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        store = SqliteStore(Path(d) / "state.db")
        store.put_run("run1", {"run_id": "run1", "workflow": "w", "status": "success",
                               "context": {"meta": {}, "nodes": {"a": {"status": "done", "output": {"x": 1}}}}})
        store.put_node("run1", "a", {"status": "done", "output": {"x": 1}})

        assert store.get_run("run1")["status"] == "success"
        assert store.get_node("run1", "a")["output"] == {"x": 1}
        assert store.get_run("nope") is None
        assert store.get_node("run1", "ghost") is None
        store.close()
    print("  ✓ test_sqlite_roundtrip")


async def main() -> None:
    test_registry_loads_all_agents()
    await test_resume_skips_done_reruns_failed()
    test_sqlite_roundtrip()
    print("\nALL M2 FINISH TESTS PASS ✅")


if __name__ == "__main__":
    asyncio.run(main())
