"""Prometheus 指标查询 MCP server（``query_metrics``）。

数据源：指标平台（Prometheus）。消费 agent：metrics-analyst（DESIGN.md §4.5 / SCENARIOS.md §6.9）。

metrics-analyst 按 system prompt 里的预设 PromQL 逐项查 CPU/磁盘/内存/GC/throttled，
判断是否「资源打满」（利用率 ≥ 阈值 或 throttled 速率显著 > 0），产出 ``MetricsEvidence``。

本工具把 Prometheus 的 instant / range 两种查询收敛为一个入口：

- 只给 ``promql``（+ 可选 ``time``）→ instant 查询（``/api/v1/query``，看当前值）。
- 给 ``start`` 或 ``end`` → range 查询（``/api/v1/query_range``，看趋势）。

配置（环境变量）::

  PROMETHEUS_URL   默认 ``http://localhost:19090``（K8s 内用 ``http://prometheus:9090``）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
from fastmcp import FastMCP

from agentflow.tools.common import evidence, get_env, now_unix, resolve_time

mcp = FastMCP("prometheus-metrics")

PROMETHEUS_URL = get_env("PROMETHEUS_URL", "http://localhost:19090")

_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))


@mcp.tool()
async def query_metrics(
    promql: str,
    time: str | None = None,
    start: str | None = None,
    end: str | None = None,
    step: str = "15s",
) -> dict:
    """查询 Prometheus 指标（PromQL）。

    - promql：PromQL 表达式（必填）。例如
      ``100 * rate(container_cpu_usage_seconds_total{service="order-service"}[5m])``。
    - time：instant 查询的求值时刻（unix 秒 / ISO8601 / 相对如 "-15m"），缺省用当前时间。
    - start / end：给任意一个即走 range 查询；缺省 start=now-1h、end=now。支持 unix / ISO8601 / 相对。
    - step：range 查询步长（默认 "15s"）。

    返回原始 Prometheus 结果（result_type + result），带 query_status（ok/empty/error）。
    """
    promql = (promql or "").strip()
    if not promql:
        return evidence("error", 60, error="promql 不能为空", promql=promql, result_type=None, result=[])

    params: dict[str, str] = {"query": promql}

    if start is not None or end is not None:
        # range 查询
        s = resolve_time(start) or str(now_unix() - 3600)
        e = resolve_time(end) or str(now_unix())
        params.update({"start": s, "end": e, "step": step or "15s"})
        path = "/api/v1/query_range"
    else:
        # instant 查询
        t = resolve_time(time)
        if t is not None:
            params["time"] = t
        path = "/api/v1/query"

    try:
        resp = await _client.get(f"{PROMETHEUS_URL.rstrip('/')}{path}", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        return evidence("error", 60, error=f"{type(e).__name__}: {e}", promql=promql, result_type=None, result=[])

    if data.get("status") != "success":
        return evidence(
            "error", 60, error=data.get("error", "prometheus query failed"),
            promql=promql, result_type=None, result=[],
        )

    d = data.get("data") or {}
    result = d.get("result") or []
    result_type = d.get("resultType")

    return evidence(
        "ok" if result else "empty",
        60,
        promql=promql,
        result_type=result_type,
        result=result,
    )


if __name__ == "__main__":
    mcp.run()
