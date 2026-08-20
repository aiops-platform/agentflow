"""服务拓扑 MCP server（``get_service`` / ``get_dependencies`` / ``get_dependents`` / ``get_path``）。

DESIGN.md §4.5 / SCENARIOS.md §四。服务拓扑是「预计算」数据源（离线 job 聚合 trace → 拓扑，
同步 CMDB → 元数据），诊断/修复时经 MCP 查询，不是 agent 运行时现算。

- ``get_service``：service → repo/owner/env 元数据（code-locator 找仓库）
- ``get_dependencies``：下游（X 调用了谁，trace-analyst 预期拓扑）
- ``get_dependents``：上游（谁调用了 X，blast radius 影响面）
- ``get_path``：a→b 调用路径（root-cause 故障传播）

配置（环境变量）：:

  TOPOLOGY_FILE  默认 ``data/topology.json``（{services: {name: {...}}, edges: [{caller, callee, ...}]}）
"""
from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import FastMCP

from agentflow.tools.common import evidence, get_env

mcp = FastMCP("service-topology")

_ROOT = Path(__file__).resolve().parents[2]
TOPOLOGY_FILE = Path(get_env("TOPOLOGY_FILE", str(_ROOT / "data" / "topology.json")))


def _load() -> dict:
    try:
        return json.loads(TOPOLOGY_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"services": {}, "edges": []}


def _adjacency(data: dict) -> dict[str, list[str]]:
    """caller -> [callee...]"""
    adj: dict[str, list[str]] = {}
    for e in data.get("edges", []):
        adj.setdefault(e.get("caller"), []).append(e.get("callee"))
    return adj


def _rev_adjacency(data: dict) -> dict[str, list[str]]:
    """callee -> [caller...]"""
    rev: dict[str, list[str]] = {}
    for e in data.get("edges", []):
        rev.setdefault(e.get("callee"), []).append(e.get("caller"))
    return rev


def _find_path(adj: dict[str, list[str]], a: str, b: str) -> list[str] | None:
    """BFS 找 a→b 路径。"""
    if a == b:
        return [a]
    q = deque([[a]])
    seen = {a}
    while q:
        path = q.popleft()
        for nxt in adj.get(path[-1], []):
            if nxt in seen:
                continue
            npath = path + [nxt]
            if nxt == b:
                return npath
            seen.add(nxt)
            q.append(npath)
    return None


@mcp.tool()
async def get_service(service: str) -> dict:
    """查服务元数据（repo/owner/env）。code-locator 据此 clone 正确仓库。"""
    data = _load()
    meta = (data.get("services") or {}).get(service)
    if meta is None:
        return evidence("empty", 3600, service=service, meta=None)
    return evidence("ok", 3600, service=service, meta=meta)


@mcp.tool()
async def get_dependencies(service: str) -> dict:
    """查下游：service 调用了哪些服务（caller→callee）。"""
    data = _load()
    deps = sorted(set(_adjacency(data).get(service, [])))
    return evidence("ok" if deps else "empty", 3600, service=service, dependencies=deps)


@mcp.tool()
async def get_dependents(service: str) -> dict:
    """查上游：谁调用了 service（影响面 / blast radius）。"""
    data = _load()
    deps = sorted(set(_rev_adjacency(data).get(service, [])))
    return evidence("ok" if deps else "empty", 3600, service=service, dependents=deps)


@mcp.tool()
async def get_path(a: str, b: str) -> dict:
    """查 a→b 调用路径（故障传播分析）。"""
    data = _load()
    path = _find_path(_adjacency(data), a, b)
    if path is None:
        return evidence("empty", 3600, a=a, b=b, path=None)
    return evidence("ok", 3600, a=a, b=b, path=path)


if __name__ == "__main__":
    mcp.run()
