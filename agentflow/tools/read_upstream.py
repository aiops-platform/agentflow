"""read_upstream_output MCP 工具：让 agent 在 session 内按需拉取上游节点的 details。

汇合节点（rca 等）默认 input_view=summary（只拿 summary），需要深挖某个分支时，
agent 调 ``read_upstream_output(run_id, node_id, field)`` 按需拉 details。

run_id 由 executor 注入 prompt（见 ``_build_prompt``），agent 只传 node_id/field——
不依赖 LLM 自己拼 run_id（DESIGN.md §4.10.2）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import FastMCP

from agentflow.config import STATE_DB

mcp = FastMCP("read-upstream")


def _get_node_state(run_id: str, node_id: str) -> dict | None:
    if not STATE_DB.exists():
        return None
    conn = sqlite3.connect(str(STATE_DB))
    try:
        row = conn.execute(
            "SELECT state FROM nodes WHERE run_id=? AND node_id=?", (run_id, node_id)
        ).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


@mcp.tool()
async def read_upstream_output(run_id: str, node_id: str, field: str | None = None) -> dict:
    """按需拉取上游节点（node_id）的完整 output，或指定字段（field，点分路径如 details.error_stack）。"""
    state = _get_node_state(run_id, node_id)
    if state is None:
        return {"status": "not_found", "node_id": node_id}
    output = state.get("output") or {}
    if not field:
        return {"status": "ok", "node_id": node_id, "output": output}
    cur = output
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return {"status": "not_found", "node_id": node_id, "field": field}
    return {"status": "ok", "node_id": node_id, "field": field, "value": cur}


if __name__ == "__main__":
    mcp.run()
