"""Mock 数据源 MCP server（L1/L2 测试用，SCENARIOS.md §5.2）。

与真实数据源（``agentflow.tools.es_logs`` / ``agentflow.tools.prometheus_metrics``）工具签名一致，
区别仅在数据来源：真实版查 ES/Prometheus，mock 版读 fixture JSON。agent 的 frontmatter 挂载的 MCP
工具名不变，只把 server 从真实换成 mock——逻辑一致，只有数据源被 stub。

用法::

    MOCK_FIXTURE_DIR=testbed/mock-datasource/fixtures/scenario1 \
        python testbed/mock-datasource/server.py

- ``MOCK_FIXTURE_DIR`` 直接指向某个场景目录（含 logs.json / metrics.json 等）即可；
- 若指向 ``fixtures`` 父目录，用 ``MOCK_FIXTURE_SCENARIO=scenario2`` 或工具参数 ``scenario`` 选择子目录。

本文件自包含（不 import agentflow 包），便于测试床独立运行；统一返回封装与
``agentflow.tools.common.evidence`` 保持一致（query_status + collected_at + ttl_seconds）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("mock-datasource")

_DEFAULT_DIR = Path(__file__).resolve().parent / "fixtures"
FIXTURES = Path(os.environ.get("MOCK_FIXTURE_DIR", str(_DEFAULT_DIR)))


# ── 与 agentflow.tools.common 保持一致的最小封装（避免 testbed 依赖 agentflow 包）──


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evidence(query_status: str, ttl_seconds: int, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "query_status": query_status,
        "collected_at": _now_iso(),
        "ttl_seconds": ttl_seconds,
    }
    out.update(extra)
    return out


def _fixture_dir(scenario: str | None) -> Path:
    if scenario:
        return FIXTURES / scenario
    if (FIXTURES / "logs.json").exists() or (FIXTURES / "metrics.json").exists():
        return FIXTURES
    # FIXTURES 是 fixtures 父目录，需要 scenario 选择器
    return FIXTURES / os.environ.get("MOCK_FIXTURE_SCENARIO", "scenario1")


def _load(scenario: str | None, name: str) -> Any:
    path = _fixture_dir(scenario) / name
    return json.loads(path.read_text(encoding="utf-8"))


# ── 工具（签名与真实数据源一致；比真实版多一个可选 scenario 参数用于选 fixture）──


@mcp.tool()
def query_logs(
    scenario: str | None = None,
    service: str | None = None,
    level: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    time_range: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    size: int = 50,
) -> dict:
    """读 fixture 返回预置日志（与真实 ES query_logs 签名一致）。"""
    logs: list[dict] = _load(scenario, "logs.json")
    if service:
        logs = [l for l in logs if l.get("service") == service]
    if level:
        logs = [l for l in logs if str(l.get("level", "")).upper() == level.upper()]
    if trace_id:
        logs = [l for l in logs if l.get("trace_id") == trace_id]
    if query:
        q = query.lower()
        logs = [
            l
            for l in logs
            if q in (l.get("message", "") + " " + (l.get("stack_trace") or "")).lower()
        ]
    # time_range/start_time/end_time 在 mock 中不参与过滤（fixture 为静态快照），仅保持签名一致
    logs = logs[: max(1, min(int(size), 500))]
    return _evidence("ok" if logs else "empty", 600, count=len(logs), logs=logs)


@mcp.tool()
def query_metrics(
    scenario: str | None = None,
    promql: str = "",
    time: str | None = None,
    start: str | None = None,
    end: str | None = None,
    step: str = "15s",
) -> dict:
    """读 fixture 返回预置指标（与真实 Prometheus query_metrics 签名一致）。

    fixture 的 metrics.json 形如 Prometheus 响应（result_type + result 向量）；按 promql 里出现的
    指标名做 best-effort 过滤，匹配不到则返回全部。
    """
    data: dict = _load(scenario, "metrics.json")
    result = data.get("result") or []

    # best-effort：从 promql 提取指标名，过滤 result 里 __name__ 匹配的序列
    if promql:
        names = {r.get("metric", {}).get("__name__") for r in result}
        names.discard(None)
        hit = [n for n in names if n in promql]
        if hit:
            result = [r for r in result if r.get("metric", {}).get("__name__") in hit]

    return _evidence(
        "ok" if result else "empty",
        60,
        promql=promql,
        result_type=data.get("result_type"),
        result=result,
    )


if __name__ == "__main__":
    mcp.run()
