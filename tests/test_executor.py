"""M2 编排执行器测试（FakeRuntime，免真实 opencode）。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from agentflow.engine import DAGExecutor, NodeResult, RunResult
from agentflow.opencode import NodeEvent, NodeEventType, TokenUsage
from agentflow.workflow.schema import EdgeDef, NodeDef, WorkflowDef


class FakeRuntime:
    """按 agent 名返回脚本化输出；"__ERROR__" 表示 infra 失败。"""

    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}

    async def run_node(self, agent: str, prompt: str, tools: list[str] | None = None):
        self.calls.append(agent)
        self.prompts[agent] = prompt
        resp = self.responses[agent]
        if resp == "__ERROR__":
            yield NodeEvent(type=NodeEventType.ERROR, error="boom")
            return
        text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
        yield NodeEvent(type=NodeEventType.SESSION_CREATED, session_id=f"ses_{agent}")
        yield NodeEvent(type=NodeEventType.TEXT, text=text)
        yield NodeEvent(type=NodeEventType.STEP_FINISH, tokens=TokenUsage(total=10))
        yield NodeEvent(type=NodeEventType.DONE)


def _wf(name: str, nodes: dict[str, NodeDef], edges: list[EdgeDef]) -> WorkflowDef:
    return WorkflowDef(name=name, nodes=nodes, edges=edges)


async def _run(wf: WorkflowDef, responses: dict[str, Any], inputs: dict | None = None) -> tuple[RunResult, FakeRuntime]:
    rt = FakeRuntime(responses)
    ex = DAGExecutor(rt, concurrency=4)
    result = await ex.run(wf, inputs=inputs)
    return result, rt


async def test_basic_dag():
    wf = _wf(
        "basic",
        {
            "triage": NodeDef(agent="triage", params={"bug": "$.inputs.bug"}),
            "logs": NodeDef(agent="log-analyst", params={"bug": "$.nodes.triage.output.summary"}),
            "rca": NodeDef(agent="root-cause", params={"logs": "$.nodes.logs.output",
                                                        "bug": "$.nodes.triage.output.summary"}),
        },
        [EdgeDef(from_="triage", to="logs"), EdgeDef(from_="logs", to="rca")],
    )
    responses = {
        "triage": {"summary": "报价单打印失败", "symptom_type": "crash", "service": "order-service"},
        "log-analyst": {"summary": "IOException: No space left on device", "error_type": "IOException"},
        "root-cause": {"summary": "磁盘满导致文件生成失败", "confidence": 0.9,
                       "hypotheses": [{"statement": "磁盘满", "confidence": 0.9}]},
    }
    result, rt = await _run(wf, responses, inputs={"bug": {"title": "x"}})

    assert result.ok, result.nodes
    assert rt.calls == ["triage", "log-analyst", "root-cause"], rt.calls
    # JSONPath 传参：rca 的 prompt 里应含 logs.output 与 triage.output.summary 的解析值
    assert "No space left on device" in rt.prompts["root-cause"]
    assert "报价单打印失败" in rt.prompts["root-cause"]
    # 输出落 context
    ctx_nodes = result.context["nodes"]
    assert ctx_nodes["logs"]["output"]["error_type"] == "IOException"
    assert ctx_nodes["rca"]["output"]["confidence"] == 0.9
    print("  ✓ test_basic_dag")


async def test_when_condition_skip_and_run():
    # passed=false → review 跳过；passed=true → review 运行
    def make(passed: bool) -> WorkflowDef:
        return _wf(
            "cond",
            {
                "test": NodeDef(agent="tester", params={}),
                "review": NodeDef(agent="reviewer", params={"x": "$.nodes.test.output"}),
            },
            [EdgeDef(from_="test", to="review", when="$.nodes.test.output.passed == true")],
        )

    res_false, _ = await _run(make(False), {"tester": {"passed": False}, "reviewer": {"passed": True}})
    assert res_false.nodes["test"].status == "done"
    assert res_false.nodes["review"].status == "skipped", res_false.nodes

    res_true, _ = await _run(make(True), {"tester": {"passed": True}, "reviewer": {"passed": True}})
    assert res_true.nodes["review"].status == "done"
    assert res_true.ok
    print("  ✓ test_when_condition_skip_and_run")


async def test_on_failure_abort():
    wf = _wf(
        "fail",
        {
            "a": NodeDef(agent="triage", params={}),
            "b": NodeDef(agent="log-analyst", params={}),
            "c": NodeDef(agent="root-cause", params={"x": "$.nodes.b.output"}),
        },
        [EdgeDef(from_="a", to="b"), EdgeDef(from_="b", to="c")],
    )
    responses = {"triage": {"summary": "ok"}, "log-analyst": "__ERROR__", "root-cause": {"summary": "x"}}
    result, _ = await _run(wf, responses)

    assert result.nodes["a"].status == "done"
    assert result.nodes["b"].status == "failed"
    assert result.nodes["c"].status == "cancelled", result.nodes
    assert not result.ok
    # 失败节点落 fallback 结构
    assert result.context["nodes"]["b"]["output"]["status"] == "failed"
    print("  ✓ test_on_failure_abort")


async def test_on_failure_continue():
    # on_failure: continue → 下游仍跑，且 params 里拿到失败节点的 FailedOutput
    wf = _wf(
        "cont",
        {
            "a": NodeDef(agent="log-analyst", params={}, on_failure="continue"),
            "b": NodeDef(agent="root-cause", params={"logs": "$.nodes.a.output"}),
        },
        [EdgeDef(from_="a", to="b")],
    )
    responses = {"log-analyst": "__ERROR__", "root-cause": {"summary": "用缺失证据推理", "confidence": 0.1}}
    result, _ = await _run(wf, responses)

    assert result.nodes["a"].status == "failed"
    assert result.nodes["b"].status == "done", result.nodes
    a_out = result.context["nodes"]["a"]["output"]
    assert a_out["status"] == "failed" and a_out["error_reason"] == "RuntimeError: boom"
    print("  ✓ test_on_failure_continue")


async def test_non_json_output_fails_schema():
    # 声明了 schema 的 agent 返回纯文本 → 应判 schema 校验失败（非静默降级）
    wf = _wf("nonjson", {"a": NodeDef(agent="triage", params={})}, [])
    result, _ = await _run(wf, {"triage": "这不是 JSON，是纯文本"})
    assert result.nodes["a"].status == "failed", result.nodes
    assert "不是合法 JSON" in (result.nodes["a"].error or "")
    print("  ✓ test_non_json_output_fails_schema")


async def test_schema_error_retry():
    # on_schema_error: retry + retry.max → 第二次给出合法输出即成功
    class FlakyRuntime(FakeRuntime):
        def __init__(self):
            super().__init__({})
            self.n = 0

        async def run_node(self, agent, prompt, tools=None):
            self.n += 1
            # 第 1 次：合法 JSON 但 symptom_type 非法 → schema 校验失败；第 2 次合法
            text = '{"symptom_type": "bogus"}' if self.n == 1 else '{"summary": "ok", "symptom_type": "crash"}'
            yield NodeEvent(type=NodeEventType.TEXT, text=text)
            yield NodeEvent(type=NodeEventType.DONE)

    wf = _wf("retry", {"a": NodeDef(agent="triage", params={}, retry=1, on_schema_error="retry")}, [])
    ex = DAGExecutor(FlakyRuntime())
    result = await ex.run(wf)
    assert result.nodes["a"].status == "done", result.nodes
    assert result.nodes["a"].attempts == 2
    print("  ✓ test_schema_error_retry")


async def test_cost_budget():
    wf = _wf(
        "budget",
        {
            "a": NodeDef(agent="a", params={"x": "$.inputs.x"}),
            "b": NodeDef(agent="b", params={"x": "$.nodes.a.output.summary"}),
            "c": NodeDef(agent="c", params={"x": "$.nodes.b.output.summary"}),
        },
        [EdgeDef(from_="a", to="b"), EdgeDef(from_="b", to="c")],
    )
    responses = {"a": {"summary": "1"}, "b": {"summary": "2"}, "c": {"summary": "3"}}
    rt = FakeRuntime(responses)
    ex = DAGExecutor(rt, concurrency=4, max_tokens=15)  # 每节点 tokens=10，第 2 个后累计 20 超限
    result = await ex.run(wf, inputs={"x": "y"})
    assert result.nodes["a"].status == "done"
    assert result.nodes["b"].status == "done"
    assert result.nodes["c"].status == "cancelled"
    print("  ✓ test_cost_budget")


async def test_input_view_summary():
    wf = _wf(
        "input_view_summary",
        {
            "logs": NodeDef(agent="log-analyst", params={"bug": "$.inputs.bug"}),
            "rca": NodeDef(agent="root-cause", params={"logs": "$.nodes.logs.output"}),
        },
        [EdgeDef(from_="logs", to="rca")],
    )
    responses = {
        "log-analyst": {"summary": "磁盘满", "details": {"error_stack": "AAAA"}},
        "root-cause": {"summary": "根因", "confidence": 0.9},
    }
    rt = FakeRuntime(responses)
    ex = DAGExecutor(rt, concurrency=4)
    await ex.run(wf, inputs={"bug": "x"})
    rca_prompt = rt.prompts["root-cause"]
    assert "error_stack" not in rca_prompt  # summary 视图：details 被裁剪
    assert "磁盘满" in rca_prompt           # summary 保留
    print("  ✓ test_input_view_summary")


async def test_input_view_full():
    wf = _wf(
        "input_view_full",
        {
            "logs": NodeDef(agent="log-analyst", params={"bug": "$.inputs.bug"}),
            "rca": NodeDef(agent="root-cause", params={"logs": "$.nodes.logs.output"}, input_view="full"),
        },
        [EdgeDef(from_="logs", to="rca")],
    )
    responses = {
        "log-analyst": {"summary": "磁盘满", "details": {"error_stack": "AAAA"}},
        "root-cause": {"summary": "根因", "confidence": 0.9},
    }
    rt = FakeRuntime(responses)
    ex = DAGExecutor(rt, concurrency=4)
    await ex.run(wf, inputs={"bug": "x"})
    rca_prompt = rt.prompts["root-cause"]
    assert "error_stack" in rca_prompt  # full 视图：保留 details
    print("  ✓ test_input_view_full")


async def main() -> None:
    await test_basic_dag()
    await test_when_condition_skip_and_run()
    await test_on_failure_abort()
    await test_on_failure_continue()
    await test_schema_error_retry()
    await test_non_json_output_fails_schema()
    await test_cost_budget()
    await test_input_view_summary()
    await test_input_view_full()
    print("\nALL M2 EXECUTOR TESTS PASS ✅")


if __name__ == "__main__":
    asyncio.run(main())
