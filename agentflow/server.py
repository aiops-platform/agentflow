"""REST API：Alertmanager webhook 触发 → POST /run。DESIGN.md §4.11.7 / §7 P0-①。

生产入口：K8s Deployment 无状态横向扩容，状态走 Postgres/Redis（config 双态）。
本地：``uvicorn agentflow.server:app --reload``。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agentflow.agents import AgentRegistry
from agentflow.config import AGENTS_DIR, build_store, settings
from agentflow.engine import DAGExecutor
from agentflow.engine.approval import ApprovalManager
from agentflow.observability import EventBus, LlmTraceSink, MetricsSink
from agentflow.opencode import OpenCodeAdapter
from agentflow.workflow.parser import parse

app = FastAPI(title="agentflow", description="AIOps Bug Fix 工作流平台 REST API")


class RunRequest(BaseModel):
    workflow: str                   # workflow YAML 路径
    ticket: dict[str, Any] = {}     # 触发入参（bug report / ticket）
    run_id: str | None = None
    resume: bool = False


def _build_executor(runtime: OpenCodeAdapter) -> DAGExecutor:
    registry = AgentRegistry(AGENTS_DIR).load()
    event_bus = EventBus()
    event_bus.subscribe(LlmTraceSink(settings.langfuse_url,
                                     settings.langfuse_public_key, settings.langfuse_secret_key))
    event_bus.subscribe(MetricsSink())
    approval = ApprovalManager(mode=settings.approval_mode, timeout_seconds=settings.approval_timeout)
    return DAGExecutor(runtime, store=build_store(), registry=registry,
                       event_bus=event_bus, approval=approval)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
async def run(req: RunRequest) -> dict[str, Any]:
    try:
        wf = parse(req.workflow)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"workflow 解析失败: {e}")

    runtime = OpenCodeAdapter()
    executor = _build_executor(runtime)
    try:
        result = await executor.run(wf, inputs=req.ticket, run_id=req.run_id, resume=req.resume)
    finally:
        await runtime.aclose()

    return {
        "run_id": result.run_id,
        "status": result.status,
        "nodes": {nid: {"status": nr.status, "error": nr.error} for nid, nr in result.nodes.items()},
    }
