"""Elasticsearch 日志查询 MCP server（``query_logs``）。

数据源：日志平台（ES）。消费 agent：log-analyst / trace-analyst（DESIGN.md §4.5）。

- log-analyst：按 service / level / 关键词 / 时间窗查错误日志、堆栈（得「果」）。
- trace-analyst：按 ``trace_id`` 关联同一调用链的服务日志，重建调用链。

字段映射：测试床用 Filebeat ``decode_json_fields``（``target: "app"``）把应用 JSON 日志解析到
``app.*`` 命名空间，故默认字段为 ``app.service`` / ``app.level`` / ``app.traceId`` /
``app.message`` / ``app.stack_trace``；时间过滤与排序用顶层 ``@timestamp``（Filebeat 采集时间，
ECS date 类型）。均可经环境变量覆盖。

配置（环境变量，本地测试床默认开箱即用）：:

  ES_URL             默认 ``http://localhost:19200``（K8s 内用 ``http://elasticsearch:9200``）
  ES_INDEX           默认 ``app-logs``
  ES_USERNAME        默认空（测试床 ES 关 security；生产配）
  ES_PASSWORD        默认空
  ES_SERVICE_FIELD   默认 ``app.service``
  ES_LEVEL_FIELD     默认 ``app.level``
  ES_TRACE_FIELD     默认 ``app.traceId``
  ES_MESSAGE_FIELD   默认 ``app.message``
  ES_STACK_FIELD     默认 ``app.stack_trace``
  ES_LOGGER_FIELD    默认 ``app.logger_name``
  ES_POD_FIELD       默认 ``app.pod``
  ES_TIMESTAMP_FIELD 默认 ``@timestamp``
"""
from __future__ import annotations

import sys
from pathlib import Path

# 支持两种运行方式：
#   1) `python -m agentflow.tools.es_logs`（需 `pip install -e .`）
#   2) opencode 直接 `python agentflow/tools/es_logs.py` 拉起（把包根目录加进 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
from fastmcp import FastMCP

from agentflow.tools.common import evidence, get_env, resolve_time
from agentflow.tools.masking import mask_data

mcp = FastMCP("es-logs")

# ── 配置（环境变量，见模块 docstring）──
ES_URL = get_env("ES_URL", "http://localhost:19200")
ES_INDEX = get_env("ES_INDEX", "app-logs")
ES_USERNAME = get_env("ES_USERNAME")
ES_PASSWORD = get_env("ES_PASSWORD")
ES_SERVICE_FIELD = get_env("ES_SERVICE_FIELD", "app.service")
ES_LEVEL_FIELD = get_env("ES_LEVEL_FIELD", "app.level")
ES_TRACE_FIELD = get_env("ES_TRACE_FIELD", "app.traceId")
ES_MESSAGE_FIELD = get_env("ES_MESSAGE_FIELD", "app.message")
ES_STACK_FIELD = get_env("ES_STACK_FIELD", "app.stack_trace")
ES_LOGGER_FIELD = get_env("ES_LOGGER_FIELD", "app.logger_name")
ES_POD_FIELD = get_env("ES_POD_FIELD", "app.pod")
ES_TIMESTAMP_FIELD = get_env("ES_TIMESTAMP_FIELD", "@timestamp")

_auth = (ES_USERNAME, ES_PASSWORD) if ES_USERNAME else None
_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), auth=_auth)


def _get(doc: dict, path: str):
    """按点分路径取嵌套字段（``app.service`` → doc["app"]["service"]）。"""
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _term(field: str, value: str) -> dict:
    """精确匹配，兼容 text / keyword 两种映射：text 字段走 .keyword 子字段，keyword 字段直接 term。"""
    return {
        "bool": {
            "should": [
                {"term": {field: value}},
                {"term": {field + ".keyword": value}},
            ],
            "minimum_should_match": 1,
        }
    }


def _normalize(hit: dict) -> dict:
    """把 ES hit 归一化成统一日志记录结构，与 mock-datasource 返回一致。"""
    src = hit.get("_source") or {}
    return {
        "timestamp": _get(src, ES_TIMESTAMP_FIELD),
        "service": _get(src, ES_SERVICE_FIELD),
        "level": _get(src, ES_LEVEL_FIELD),
        "trace_id": _get(src, ES_TRACE_FIELD),
        "message": _get(src, ES_MESSAGE_FIELD),
        "stack_trace": _get(src, ES_STACK_FIELD),
        "logger": _get(src, ES_LOGGER_FIELD),
        "pod": _get(src, ES_POD_FIELD),
        "_id": hit.get("_id"),
        "_index": hit.get("_index"),
    }


@mcp.tool()
async def query_logs(
    service: str | None = None,
    level: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    time_range: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    size: int = 50,
) -> dict:
    """查询 Elasticsearch 日志（索引 app-logs），返回统一日志记录列表。

    参数均为可选，可任意组合过滤：
    - service：服务名精确匹配（如 "order-service"）
    - level：日志级别（INFO/WARN/ERROR，大小写不敏感）
    - trace_id：按链路 traceId 关联同一调用链的所有服务日志（trace-analyst 重建调用链）
    - query：对 message + stack_trace 做全文关键词匹配（如 "No space left on device"）
    - time_range：相对时间窗（如 "15m" / "1h"，等价于 now-15m ~ now）
    - start_time / end_time：绝对时间边界（unix 秒或 ISO8601）
    - size：返回条数上限（默认 50，最大 500）

    返回带 query_status（ok=有数据 / empty=查不到 / error=查询失败），供下游区分「无异常」与「证据不足」。
    """
    must: list[dict] = []
    if service:
        must.append(_term(ES_SERVICE_FIELD, service))
    if level:
        must.append(_term(ES_LEVEL_FIELD, level.upper()))
    if trace_id:
        must.append(_term(ES_TRACE_FIELD, trace_id))
    if query:
        must.append(
            {"multi_match": {"query": query, "fields": [ES_MESSAGE_FIELD, ES_STACK_FIELD]}}
        )

    # 时间范围：time_range（相对）优先，其次 start_time/end_time（绝对）
    rng: dict[str, str] | None = None
    if time_range:
        rng = {"gte": f"now-{time_range.strip()}", "lte": "now"}
    else:
        gte = resolve_time(start_time)
        lte = resolve_time(end_time)
        if gte or lte:
            rng = {}
            if gte:
                rng["gte"] = gte
            if lte:
                rng["lte"] = lte
    if rng:
        must.append({"range": {ES_TIMESTAMP_FIELD: rng}})

    size = max(1, min(int(size), 500))
    body = {
        "query": {"bool": {"must": must}} if must else {"match_all": {}},
        "sort": [{ES_TIMESTAMP_FIELD: {"order": "desc"}}],
        "size": size,
    }

    url = f"{ES_URL.rstrip('/')}/{ES_INDEX}/_search"
    try:
        resp = await _client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001 —— 查询失败要转成 query_status=error，不能抛
        return evidence("error", 600, error=f"{type(e).__name__}: {e}", count=0, total=0, logs=[])

    hits = (data.get("hits") or {}).get("hits") or []
    total_hits = (data.get("hits") or {}).get("total") or {}
    total = total_hits.get("value", len(hits)) if isinstance(total_hits, dict) else len(hits)
    logs = mask_data([_normalize(h) for h in hits])

    return evidence(

        "ok" if logs else "empty",
        600,
        count=len(logs),
        total=total,
        index=ES_INDEX,
        logs=logs,
    )


if __name__ == "__main__":
    mcp.run()
