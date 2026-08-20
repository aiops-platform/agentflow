"""DAGExecutor：asyncio 并发调度。DESIGN.md §4.3 / §4.11。

- 拓扑 ready 集：上游全 settle 的节点并行执行（并发配额 Semaphore）。
- 条件边 ``when``：求值 False → 节点 skipped（并向下游传播）。
- 失败分类：infra 失败（runtime 异常/ERROR 事件）走 retry；logic 失败（schema 校验不过）走 on_failure。
- 输出提取：agent 最终文本 → JSON（按 agent 的 output schema 校验）→ 落 WorkflowContext。
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from agentflow.domain import AGENT_OUTPUT_SCHEMAS
from agentflow.engine.artifact import ArtifactPaths
from agentflow.engine.context import WorkflowContext, eval_when
from agentflow.engine.state import InMemoryStore, StateStore
from agentflow.opencode import AgentRuntime, NodeEventType
from agentflow.workflow.dag import build_edges
from agentflow.workflow.schema import NodeDef, WorkflowDef


@dataclass
class NodeResult:
    node_id: str
    status: str                      # done / failed / skipped / cancelled
    output: Any = None
    stdout: str = ""
    error: str | None = None
    tokens: int = 0
    cost: float = 0.0
    attempts: int = 0


@dataclass
class RunResult:
    run_id: str
    workflow: str
    status: str                      # success / failed
    nodes: dict[str, NodeResult] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"


class DAGExecutor:
    def __init__(self, runtime: AgentRuntime, *, store: StateStore | None = None, concurrency: int = 4):
        self.runtime = runtime
        self.store = store or InMemoryStore()
        self.concurrency = concurrency

    async def run(
        self,
        wf: WorkflowDef,
        inputs: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        ctx = WorkflowContext(run_id, wf.name, inputs or {})
        paths = ArtifactPaths(run_id)

        upstreams: dict[str, list[str]] = {n: [] for n in wf.nodes}
        for u, v in build_edges(wf):
            if u in wf.nodes and v in wf.nodes and u != v and u not in upstreams[v]:
                upstreams[v].append(u)

        node_status: dict[str, str] = {n: "pending" for n in wf.nodes}
        node_done: dict[str, asyncio.Event] = {n: asyncio.Event() for n in wf.nodes}
        results: dict[str, NodeResult] = {}
        sem = asyncio.Semaphore(self.concurrency)
        abort = asyncio.Event()

        async def run_node(node_id: str) -> None:
            try:
                for u in upstreams[node_id]:
                    await node_done[u].wait()

                if abort.is_set():
                    node_status[node_id] = "cancelled"
                    results[node_id] = NodeResult(node_id, "cancelled")
                    ctx.set_node(node_id, status="cancelled", output=None)
                    return

                if self._should_skip(wf, node_id, upstreams[node_id], node_status, ctx):
                    node_status[node_id] = "skipped"
                    results[node_id] = NodeResult(node_id, "skipped")
                    ctx.set_node(node_id, status="skipped", output=None)
                    return

                async with sem:
                    node_status[node_id] = "running"
                    result = await self._execute_node(node_id, wf.nodes[node_id], ctx, paths)
                    results[node_id] = result
                    node_status[node_id] = result.status
                    if result.status == "failed" and wf.nodes[node_id].on_failure == "abort":
                        abort.set()
            except Exception as e:  # noqa: BLE001 —— 意外异常兜底，避免下游死等
                node_status[node_id] = "failed"
                results[node_id] = NodeResult(node_id, "failed", error=f"{type(e).__name__}: {e}")
                ctx.mark_failed(node_id, str(e))
                abort.set()
            finally:
                node_done[node_id].set()

        await asyncio.gather(*(asyncio.create_task(run_node(n)) for n in wf.nodes))

        run_status = "failed" if any(r.status == "failed" for r in results.values()) else "success"
        self.store.put_run(run_id, {"run_id": run_id, "workflow": wf.name,
                                    "status": run_status, "context": ctx.snapshot()})

        return RunResult(run_id=run_id, workflow=wf.name, status=run_status,
                         nodes=dict(results), context=ctx.snapshot())

    # ── 决策 ──

    def _should_skip(self, wf: WorkflowDef, node_id: str, upstreams: list[str],
                     node_status: dict[str, str], ctx: WorkflowContext) -> bool:
        # 上游 skipped → 本节点 skipped（分支整体失效）
        if any(node_status[u] == "skipped" for u in upstreams):
            return True
        # when 条件边：入边条件为 False → 本节点 skipped
        for e in wf.edges:
            if e.to == node_id and e.when and node_status.get(e.from_) == "done":
                if not eval_when(e.when, ctx):
                    return True
        return False

    # ── 单节点执行 ──

    async def _execute_node(self, node_id: str, node: NodeDef,
                            ctx: WorkflowContext, paths: ArtifactPaths) -> NodeResult:
        max_retry = node.retry.max if node.retry else 0
        last_err: str | None = None

        for attempt in range(max_retry + 1):
            paths.ensure_node(node_id, attempt)
            params = ctx.resolve_params(node.params)
            prompt = self._build_prompt(node.agent, params)
            try:
                final_text, tokens, cost = await self._run_agent(node.agent, prompt)
            except Exception as e:  # infra 失败 → retry
                last_err = f"{type(e).__name__}: {e}"
                continue

            output, schema_err = self._extract_output(node.agent, final_text)
            if schema_err is not None:
                if node.on_schema_error == "coerce":
                    output = {"summary": final_text}
                elif node.on_schema_error == "retry" and attempt < max_retry:
                    last_err = schema_err
                    continue
                else:  # fail（默认）
                    ctx.mark_failed(node_id, schema_err, stdout=final_text)
                    return NodeResult(node_id, "failed", error=schema_err,
                                      stdout=final_text, attempts=attempt + 1)

            ctx.set_node(node_id, status="done", output=output,
                         stdout=final_text, attempts=attempt + 1)
            return NodeResult(node_id, "done", output=output, stdout=final_text,
                              tokens=tokens, cost=cost, attempts=attempt + 1)

        ctx.mark_failed(node_id, last_err or "unknown error")
        return NodeResult(node_id, "failed", error=last_err, attempts=max_retry + 1)

    async def _run_agent(self, agent: str, prompt: str) -> tuple[str, int, float]:
        texts: list[str] = []
        tokens = 0
        cost = 0.0
        async for ev in self.runtime.run_node(agent, prompt):
            if ev.type is NodeEventType.ERROR:
                raise RuntimeError(ev.error or "agent runtime error")
            if ev.type is NodeEventType.TEXT and ev.text:
                texts.append(ev.text)
            elif ev.type is NodeEventType.STEP_FINISH:
                if ev.tokens:
                    tokens += ev.tokens.total
                if ev.cost is not None:
                    cost += ev.cost
        return " ".join(texts), tokens, cost

    # ── 输出提取 ──

    def _extract_output(self, agent: str, final_text: str) -> tuple[Any, str | None]:
        schema = AGENT_OUTPUT_SCHEMAS.get(agent)
        data = self._parse_json(final_text)
        if data is None:
            # 模型没返回合法 JSON：声明了 schema 的 agent 视为校验失败（DESIGN §4.10.4）
            if schema is not None:
                return None, "模型输出不是合法 JSON，无法校验 output schema"
            return {"summary": final_text}, None    # 未声明 schema → 非结构化降级
        if schema is not None:
            try:
                return schema.model_validate(data).model_dump(), None
            except ValidationError as e:
                return None, f"output schema 校验失败: {e}"
        return data, None                           # 无 schema → 接受原始 JSON

    @staticmethod
    def _parse_json(text: str) -> Any | None:
        text = (text or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return None

    def _build_prompt(self, agent: str, params: dict[str, Any]) -> str:
        schema = AGENT_OUTPUT_SCHEMAS.get(agent)
        hint = ""
        if schema is not None:
            hint = (
                "\n请严格以 JSON 对象输出，符合以下 JSON Schema：\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=False)
            )
        return f"你是 {agent} 职能 agent。输入：\n{json.dumps(params, ensure_ascii=False, indent=2)}{hint}"
