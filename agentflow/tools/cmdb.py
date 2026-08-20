"""CMDB MCP server（``get_ci``）。DESIGN.md §4.5 / SCENARIOS.md §七 P0-②。

数据源：CMDB（service → repo / owner / env 元数据）。消费 agent：triage / code-locator
（跨服务定位时由 service 查 repo）。

M3 最简实现：读一个 JSON 文件（``CMDB_FILE``），离线 job 定期同步真实 CMDB。

配置（环境变量）：:

  CMDB_FILE  默认 ``data/cmdb.json``（顶层为 {service: {repo, owner, env}}）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import FastMCP

from agentflow.tools.common import evidence, get_env

mcp = FastMCP("cmdb")

_ROOT = Path(__file__).resolve().parents[2]
CMDB_FILE = Path(get_env("CMDB_FILE", str(_ROOT / "data" / "cmdb.json")))


def _load() -> dict:
    try:
        return json.loads(CMDB_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


@mcp.tool()
async def get_ci(service: str) -> dict:
    """查 CI（配置项）元数据：service → {repo, owner, env}。用于从故障服务定位代码仓库。"""
    data = _load()
    ci = data.get(service)
    if ci is None:
        return evidence("empty", 3600, service=service, ci=None)
    return evidence("ok", 3600, service=service, ci=ci)


if __name__ == "__main__":
    mcp.run()
