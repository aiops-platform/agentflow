"""REST API：workflow 管理 + run 触发/查询。供前端（Vue 流程图）使用。DESIGN.md §4.11.7。

- workflow CRUD：POST/GET/DELETE /workflows（保存 YAML，复用）。
- run：POST /run 异步触发（返回 run_id），GET /runs/:run_id 轮询节点详情 + token 统计。
- 生产入口：K8s Deployment 无状态横向扩容，状态走 Postgres/Redis（config 双态）。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agentflow.agents import AgentRegistry
from agentflow.config import AGENTS_DIR, STATE_DB, build_store, settings
from agentflow.engine import DAGExecutor
from agentflow.engine.approval import ApprovalManager
from agentflow.observability import EventBus, LlmTraceSink, MetricsSink
from agentflow.opencode import OpenCodeAdapter
from agentflow.workflow.dag import build_edges
from agentflow.workflow.parser import parse, parse_yaml_str

app = FastAPI(title="agentflow", description="AIOps Bug Fix 工作流平台 REST API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 请求模型 ──

class WorkflowCreate(BaseModel):
    name: str
    yaml: str


class RunRequest(BaseModel):
    workflow: str | None = None        # workflow YAML 路径（兼容旧用法）
    yaml: str | None = None            # 或直接传 YAML 文本
    workflow_id: str | None = None     # 或已保存的 workflow id
    ticket: dict[str, Any] = {}
    run_id: str | None = None
    resume: bool = False


# ── WorkflowStore（复用 state.db 的 SQLite，加 workflows 表）──

class WorkflowStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS workflows ("
            "id TEXT PRIMARY KEY, name TEXT, yaml TEXT, created_at TEXT)"
        )
        conn.commit()
        conn.close()

    def save(self, name: str, yaml: str) -> str:
        wid = uuid.uuid4().hex[:12]
        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO workflows (id, name, yaml, created_at) VALUES (?,?,?,?)",
                     (wid, name, yaml, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
        return wid

    def list(self) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT id, name, created_at FROM workflows ORDER BY created_at DESC").fetchall()
        conn.close()
        return [{"id": r[0], "name": r[1], "created_at": r[2]} for r in rows]

    def get(self, wid: str) -> dict | None:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT id, name, yaml FROM workflows WHERE id=?", (wid,)).fetchone()
        conn.close()
        return {"id": row[0], "name": row[1], "yaml": row[2]} if row else None

    def delete(self, wid: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute("DELETE FROM workflows WHERE id=?", (wid,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0


workflow_store = WorkflowStore(str(STATE_DB))


def _read_nodes(run_id: str) -> dict:
    """读 nodes 表所有节点状态（节点级 checkpoint，比 runs 表的 context 快照更新）。"""
    conn = sqlite3.connect(str(STATE_DB))
    try:
        rows = conn.execute("SELECT node_id, state FROM nodes WHERE run_id=?", (run_id,)).fetchall()
        return {nid: json.loads(state) for nid, state in rows}
    finally:
        conn.close()


def _workflow_graph(wf) -> dict:
    """从 WorkflowDef 提取节点/边结构（供前端画图）。"""
    return {
        "name": wf.name,
        "nodes": [{"id": nid, "agent": node.agent} for nid, node in wf.nodes.items()],
        "edges": [{"from": e.from_, "to": e.to, "when": e.when} for e in wf.edges],
    }


def _build_executor(runtime: OpenCodeAdapter) -> DAGExecutor:
    registry = AgentRegistry(AGENTS_DIR).load()
    event_bus = EventBus()
    event_bus.subscribe(LlmTraceSink(settings.langfuse_url,
                                     settings.langfuse_public_key, settings.langfuse_secret_key))
    event_bus.subscribe(MetricsSink())
    approval = ApprovalManager(mode=settings.approval_mode, timeout_seconds=settings.approval_timeout)
    return DAGExecutor(runtime, store=build_store(), registry=registry,
                       event_bus=event_bus, approval=approval,
                       max_cost=settings.max_cost, max_tokens=settings.max_tokens)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── workflow CRUD ──

@app.post("/workflows")
async def create_workflow(req: WorkflowCreate) -> dict:
    try:
        wf = parse_yaml_str(req.yaml)  # 校验 YAML 合法性
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"YAML 解析失败: {e}")
    wid = workflow_store.save(req.name, req.yaml)
    return {"id": wid, "name": req.name, "graph": _workflow_graph(wf)}


@app.get("/workflows")
async def list_workflows() -> list[dict]:
    return workflow_store.list()


@app.get("/workflows/{wid}")
async def get_workflow(wid: str) -> dict:
    wf = workflow_store.get(wid)
    if wf is None:
        raise HTTPException(status_code=404, detail="workflow 不存在")
    try:
        graph = _workflow_graph(parse_yaml_str(wf["yaml"]))
    except Exception:  # noqa: BLE001
        graph = {}
    return {**wf, "graph": graph}


@app.delete("/workflows/{wid}")
async def delete_workflow(wid: str) -> dict:
    if not workflow_store.delete(wid):
        raise HTTPException(status_code=404, detail="workflow 不存在")
    return {"ok": True}


# ── run 触发 + 查询 ──

_run_tasks: dict[str, asyncio.Task] = {}


@app.post("/run")
async def run(req: RunRequest) -> dict:
    # 解析 workflow（workflow_id > yaml 文本 > 文件路径）
    if req.workflow_id:
        wf_row = workflow_store.get(req.workflow_id)
        if wf_row is None:
            raise HTTPException(status_code=404, detail="workflow 不存在")
        wf = parse_yaml_str(wf_row["yaml"])
    elif req.yaml:
        wf = parse_yaml_str(req.yaml)
    elif req.workflow:
        wf = parse(req.workflow)
    else:
        raise HTTPException(status_code=400, detail="需提供 workflow / yaml / workflow_id 之一")

    if req.resume and not req.run_id:
        raise HTTPException(status_code=400, detail="resume 需提供 run_id")
    run_id = req.run_id or f"run_{uuid.uuid4().hex[:12]}"

    runtime = OpenCodeAdapter()
    executor = _build_executor(runtime)

    async def _run_bg():
        try:
            await executor.run(wf, inputs=req.ticket, run_id=run_id, resume=req.resume)
        finally:
            await runtime.aclose()

    _run_tasks[run_id] = asyncio.create_task(_run_bg())
    return {"run_id": run_id, "status": "started"}


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict:
    store = build_store()
    try:
        run = await store.get_run(run_id)
    finally:
        await store.close()
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    # 聚合 nodes 表（节点级 checkpoint，实时）；为空时兜底 run 记录的 context.nodes
    nodes = _read_nodes(run_id) or (run.get("context") or {}).get("nodes", {})
    total_tokens = sum((n.get("tokens") or 0) for n in nodes.values())
    total_cost = sum((n.get("cost") or 0.0) for n in nodes.values())
    return {
        "run_id": run_id,
        "workflow": run.get("workflow"),
        "status": run.get("status"),
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "nodes": nodes,
    }
