"""DAGExecutor：asyncio 并发调度。DESIGN.md §4.3 / §4.11。

- 拓扑 ready 集：上游全 settle 的节点并行执行（并发配额 Semaphore）。
- 条件边 ``when``：求值 False → 节点 skipped（并向下游传播）。
- 失败分类：infra 失败（runtime 异常/ERROR 事件）走 retry；logic 失败（schema 校验不过）走 on_failure。
- 输出提取：agent 最终文本 → JSON（按 agent 的 output schema 校验）→ 落 WorkflowContext。
- 断点续跑：节点级 checkpoint；resume 时已 done 节点幂等跳过，只重跑 failed/pending。
- 审批门禁（M4）：节点 ``approve`` 字段 → 经 ApprovalManager；驳回 → approval_rejected 走 on_failure。
- 观测（M4）：编排事件 + LLM 事件扇出到 event_bus（run/node/generation/tool_call）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from agentflow.agents import AgentRegistry
from agentflow.agents.base import AgentSpec
from agentflow.domain import AGENT_OUTPUT_SCHEMAS
from agentflow.engine.approval import ApprovalManager
from agentflow.engine.artifact import ArtifactPaths
from agentflow.engine.context import WorkflowContext, eval_when
from agentflow.engine.state import InMemoryStore, StateStore
from agentflow.observability import Event, EventBus, EventType
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
    def __init__(self, runtime: AgentRuntime, *, store: StateStore | None = None,
                 concurrency: int = 4, registry: AgentRegistry | None = None,
                 event_bus: EventBus | None = None, approval: ApprovalManager | None = None,
                 max_cost: float = 0.0, max_tokens: int = 0):
        self.runtime = runtime
        self.store = store or InMemoryStore()
        self.concurrency = concurrency
        self.registry = registry
        self.event_bus = event_bus
        self.approval = approval
        self.max_cost = max_cost
        self.max_tokens = max_tokens
        self._total_cost = 0.0
        self._total_tokens = 0

    async def run(
        self,
        wf: WorkflowDef,
        inputs: dict[str, Any] | None = None,
        run_id: str | None = None,
        resume: bool = False,
        extra_inputs: dict[str, Any] | None = None,
        invalidate_from: str | None = None,
    ) -> RunResult:
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"

        # 断点续跑：从 store 恢复上下文
        if resume:
            stored = await self.store.get_run(run_id)
            ctx = (WorkflowContext.from_snapshot(stored["context"]) if stored
                   else WorkflowContext(run_id, wf.name, inputs or {}))
        else:
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

        await self._publish(Event(EventType.RUN_STARTED, run_id, data={"workflow": wf.name}))
        # 落盘 run 记录（status=running），供前端轮询进度（结束后更新为 success/failed）
        await self.store.put_run(run_id, {"run_id": run_id, "workflow": wf.name,
                                          "status": "running", "context": ctx.snapshot()})

        # 恢复：已 done 节点幂等跳过（复用缓存 output）
        if resume:
            # 补料：新 input 合并进全局 inputs（只作用于重跑的 failed/pending 节点）
            if extra_inputs:
                ctx.data.setdefault("inputs", {}).update(extra_inputs)
            # 作废：invalidate_from 及其下游作废重跑（不恢复 done）
            invalidated = self._downstream(wf, invalidate_from) if invalidate_from else set()
            for n in wf.nodes:
                if n in invalidated:
                    continue
                nstate = (ctx.data.get("nodes") or {}).get(n)
                if nstate and nstate.get("status") == "done":
                    node_status[n] = "done"
                    node_done[n].set()
                    results[n] = NodeResult(n, "done", output=nstate.get("output"),
                                            stdout=nstate.get("stdout", ""),
                                            attempts=nstate.get("attempts", 1))

        async def run_node(node_id: str) -> None:
            started_at: float | None = None
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
                    await self._publish(Event(EventType.NODE_STARTED, run_id, node_id,
                                              data={"agent": wf.nodes[node_id].agent}))
                    started_at = time.monotonic()
                    result = await self._execute_node(node_id, wf.nodes[node_id], ctx, paths, run_id)
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
                nr = results.get(node_id)
                await self._publish(Event(EventType.NODE_FINISHED, run_id, node_id,
                                          data={"status": node_status[node_id],
                                                "tokens": nr.tokens if nr else 0,
                                                "cost": nr.cost if nr else 0.0,
                                                "duration": (time.monotonic() - started_at) if started_at else None}))
                # 节点级 checkpoint（断点续跑）
                nstate = (ctx.data.get("nodes") or {}).get(node_id)
                await self.store.put_node(run_id, node_id, nstate or {"status": node_status[node_id]})
                node_done[node_id].set()
                # 成本预算检查：累计 cost/tokens，超限触发 abort（后续节点 cancelled）
                if nr:
                    self._total_cost += nr.cost or 0.0
                    self._total_tokens += nr.tokens or 0
                if (self.max_cost and self._total_cost >= self.max_cost) or \
                   (self.max_tokens and self._total_tokens >= self.max_tokens):
                    abort.set()
                # 暂停检查：pause 命令设置了 paused 标志则停止（后续节点 cancelled）
                try:
                    stored = await self.store.get_run(run_id)
                    if stored and (stored.get("context") or {}).get("meta", {}).get("paused"):
                        abort.set()
                except Exception:  # noqa: BLE001 —— 暂停检查失败不影响主流程
                    pass

        tasks = [asyncio.create_task(run_node(n)) for n in wf.nodes if node_status[n] != "done"]
        if tasks:
            await asyncio.gather(*tasks)

        # 停止/pause 检查：paused 标志 → cancelled（否则按节点结果 success/failed）
        try:
            stored = await self.store.get_run(run_id)
            paused = bool(stored and (stored.get("context") or {}).get("meta", {}).get("paused"))
        except Exception:  # noqa: BLE001
            paused = False
        if paused:
            run_status = "cancelled"
        elif any(r.status == "failed" for r in results.values()):
            run_status = "failed"
        else:
            run_status = "success"
        await self.store.put_run(run_id, {"run_id": run_id, "workflow": wf.name,
                                          "status": run_status, "context": ctx.snapshot()})
        await self._publish(Event(EventType.RUN_FINISHED, run_id, data={"status": run_status}))

        return RunResult(run_id=run_id, workflow=wf.name, status=run_status,
                         nodes=dict(results), context=ctx.snapshot())

    @staticmethod
    def _downstream(wf: WorkflowDef, node_id: str) -> set[str]:
        """返回 node_id 及其所有下游节点集合（作废重跑用）。"""
        adj: dict[str, list[str]] = {}
        for u, v in build_edges(wf):
            adj.setdefault(u, []).append(v)
        seen: set[str] = set()
        queue = [node_id]
        while queue:
            u = queue.pop()
            if u in seen:
                continue
            seen.add(u)
            queue.extend(adj.get(u, []))
        return seen

    async def _publish(self, event: Event) -> None:
        if self.event_bus is not None:
            await self.event_bus.publish(event)

    # ── 决策 ──

    def _should_skip(self, wf: WorkflowDef, node_id: str, upstreams: list[str],
                     node_status: dict[str, str], ctx: WorkflowContext) -> bool:
        if any(node_status[u] == "skipped" for u in upstreams):
            return True
        for e in wf.edges:
            if e.to == node_id and e.when and node_status.get(e.from_) == "done":
                if not eval_when(e.when, ctx):
                    return True
        return False

    # ── 单节点执行 ──

    def _spec(self, agent: str) -> AgentSpec | None:
        return self.registry.get(agent) if self.registry else None

    async def _execute_node(self, node_id: str, node: NodeDef,
                            ctx: WorkflowContext, paths: ArtifactPaths, run_id: str) -> NodeResult:
        max_retry = node.retry.max if node.retry else 0
        last_err: str | None = None
        spec = self._spec(node.agent)
        # resume 原 session：从 checkpoint 读 session_id（作废重跑 done 节点时接着原 session）
        nstate = (ctx.data.get("nodes") or {}).get(node_id)
        resume_sid = (nstate or {}).get("session_id")

        for attempt in range(max_retry + 1):
            paths.ensure_node(node_id, attempt)
            params = ctx.resolve_params(node.params)
            view = node.input_view or (spec.input_view if spec else "summary")
            if view == "summary":
                params = self._crop_summary(node.params, params)
            prompt = self._build_prompt(node.agent, params, spec, run_id=run_id)
            try:
                final_text, tokens, cost, resume_sid = await self._run_agent(
                    node.agent, prompt, spec, run_id, node_id, session_id=resume_sid)
            except Exception as e:  # infra 失败 → retry
                last_err = f"{type(e).__name__}: {e}"
                continue

            await self._publish(Event(EventType.GENERATION, run_id, node_id,
                                      data={"agent": node.agent, "prompt": prompt, "output": final_text,
                                            "tokens": {"total": tokens}, "cost": cost}))

            output, schema_err = self._extract_output(node.agent, final_text, spec)
            if schema_err is not None:
                if node.on_schema_error == "coerce":
                    output = {"summary": final_text}
                elif node.on_schema_error == "retry" and attempt < max_retry:
                    last_err = schema_err
                    continue
                else:  # fail（默认）
                    ctx.mark_failed(node_id, schema_err, stdout=final_text, prompt=prompt)
                    return NodeResult(node_id, "failed", error=schema_err,
                                      stdout=final_text, attempts=attempt + 1)

            # 审批门禁：节点 approve 字段 → 需人工批准（驳回 → approval_rejected 走 on_failure）
            if node.approve and self.approval is not None:
                approved = await self.approval.request(run_id, node_id, {"trigger": node.approve})
                await self._publish(Event(EventType.APPROVAL_DECIDED, run_id, node_id,
                                          data={"trigger": node.approve, "approved": approved}))
                if not approved:
                    ctx.mark_failed(node_id, "approval_rejected", stdout=final_text, prompt=prompt)
                    return NodeResult(node_id, "failed", error="approval_rejected",
                                      stdout=final_text, attempts=attempt + 1)

            ctx.set_node(node_id, status="done", output=output,
                         stdout=final_text, attempts=attempt + 1, prompt=prompt,
                         session_id=resume_sid, tokens=tokens, cost=cost)
            return NodeResult(node_id, "done", output=output, stdout=final_text,
                              tokens=tokens, cost=cost, attempts=attempt + 1)

        ctx.mark_failed(node_id, last_err or "unknown error")
        return NodeResult(node_id, "failed", error=last_err, attempts=max_retry + 1)

    async def _run_agent(self, agent: str, prompt: str, spec: AgentSpec | None,
                         run_id: str, node_id: str,
                         session_id: str | None = None) -> tuple[str, int, float, str | None]:
        texts: list[str] = []
        tokens = 0
        cost = 0.0
        sid = session_id
        tools = spec.tools if spec else None
        async for ev in self.runtime.run_node(agent, prompt, tools=tools, session_id=session_id):
            if ev.type is NodeEventType.SESSION_CREATED and ev.session_id:
                sid = ev.session_id
            if ev.type is NodeEventType.ERROR:
                err = ev.error
                if not err and ev.raw:
                    props = ev.raw.get("properties") or {}
                    err = props.get("error") or props.get("message")
                raise RuntimeError(err or "agent runtime error")
            if ev.type is NodeEventType.TEXT and ev.text:
                texts.append(ev.text)
            elif ev.type is NodeEventType.STEP_FINISH:
                if ev.tokens:
                    tokens += ev.tokens.total
                if ev.cost is not None:
                    cost += ev.cost
            elif ev.type is NodeEventType.TOOL_CALL and ev.tool is not None:
                await self._publish(Event(EventType.TOOL_CALL, run_id, node_id,
                                          data={"name": ev.tool.name, "input": ev.tool.input}))
        return " ".join(texts), tokens, cost, sid

    # ── 输出提取 ──

    def _extract_output(self, agent: str, final_text: str,
                        spec: AgentSpec | None) -> tuple[Any, str | None]:
        schema = spec.output_schema if spec else AGENT_OUTPUT_SCHEMAS.get(agent)
        data = self._parse_json(final_text)
        if data is None:
            if schema is not None:
                return None, "模型输出不是合法 JSON，无法校验 output schema"
            return {"summary": final_text}, None
        if schema is not None:
            try:
                return schema.model_validate(data).model_dump(), None
            except ValidationError as e:
                return None, f"output schema 校验失败: {e}"
        return data, None

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
            candidate = m.group(0)
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
            # json5 宽松兜底（尾逗号 / 注释 / 单引号 key 等 LLM 常见瑕疵）
            try:
                import json5
                return json5.loads(candidate)
            except Exception:  # noqa: BLE001 —— json5 不可用或仍解析失败则放弃
                pass
        return None

    @staticmethod
    def _crop_summary(param_refs: dict[str, str], resolved: dict[str, Any]) -> dict[str, Any]:
        """input_view=summary 时，把整个 output 引用（$.nodes.<id>.output）裁剪成去掉 details。

        只对「整个 output」的引用裁剪（去掉 details 大字段，如 error_stack/span_tree/diff）；
        特定字段引用（.output.summary / .output.diff）和全局引用（$.inputs.*）保持原样。
        """
        out: dict[str, Any] = {}
        for key, ref in param_refs.items():
            val = resolved[key]
            if isinstance(ref, str) and ref.endswith(".output") and isinstance(val, dict):
                out[key] = {k: v for k, v in val.items() if k != "details" and v is not None}
            else:
                out[key] = val
        return out

    def _build_prompt(self, agent: str, params: dict[str, Any], spec: AgentSpec | None,
                      run_id: str | None = None) -> str:
        schema = spec.output_schema if spec else AGENT_OUTPUT_SCHEMAS.get(agent)
        system = spec.system_prompt if spec else ""
        parts: list[str] = []
        if system:
            parts.append(system)
        parts.append("输入（JSON）：\n" + json.dumps(params, ensure_ascii=False, indent=2))
        if run_id:
            parts.append(
                f"当前 run_id: {run_id}。上游节点的 output 已按 summary 裁剪（details 未注入）；"
                "若需要某上游节点的完整 details，调用工具 read_upstream_output(run_id, node_id, field) 按需拉取。"
            )
        if schema is not None:
            parts.append("请严格以 JSON 对象输出，符合以下 JSON Schema：\n"
                         + json.dumps(schema.model_json_schema(), ensure_ascii=False))
        return "\n\n".join(parts)
