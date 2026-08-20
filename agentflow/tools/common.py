"""数据源 MCP 工具层共享设施。

统一约定（DESIGN.md §4.5 / §六）：

- 证据类返回一律带 ``query_status``（``ok`` / ``empty`` / ``error``）+ ``collected_at`` +
  ``ttl_seconds``，让 root-cause 能把「查不到」和「正常」区分开（负证据语义）。
- 所有外部地址/凭据走环境变量，不进代码、不进 YAML（DESIGN.md §4.11.7）。

本模块只放跨数据源通用的东西：配置读取、时长/时间解析、统一响应封装。
ES / Prometheus / K8s / CMDB 各自的服务端在同目录下独立成文件。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# ── 配置 ──


def get_env(name: str, default: str | None = None, cast: Callable[[str], Any] | None = None) -> Any:
    """读环境变量，缺省返回 default；cast 用于类型转换（如 int / bool）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is None:
        return raw
    if cast is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return cast(raw)


# ── 时间 ──

_DURATION_RE = re.compile(r"^(\d+)\s*(ms|s|m|h|d)$")
_UNIT_SECONDS = {"ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(value: str) -> timedelta:
    """解析 ``30s`` / ``15m`` / ``1h`` / ``2d`` 之类时长字符串。"""
    m = _DURATION_RE.match(str(value).strip().lower())
    if not m:
        raise ValueError(f"无法解析时长 {value!r}（支持如 30s/15m/1h/2d）")
    return timedelta(seconds=float(m.group(1)) * _UNIT_SECONDS[m.group(2)])


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat()


def now_unix() -> int:
    return int(utc_now().timestamp())


def resolve_time(value: Any) -> str | None:
    """把用户给的时间解析成 Prometheus/ES 可用的字符串。

    支持：``None`` / ``''`` / ``"now"``（省略，用服务端当前时间）、``"-15m"``（相对时长）、
    纯数字（unix 秒）、RFC3339 / ISO8601（原样透传）。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).strip()
    if s == "" or s.lower() == "now":
        return None
    if s.startswith("-"):
        delta = parse_duration(s[1:])
        return str(int((utc_now() - delta).timestamp()))
    if s.lstrip("-").isdigit():
        return s
    return s  # 视为 RFC3339 / ISO8601，原样透传


# ── 统一响应封装 ──


def evidence(query_status: str, ttl_seconds: int, **extra: Any) -> dict[str, Any]:
    """构造统一证据返回。``query_status``：``ok``（有数据）/ ``empty``（查不到）/ ``error``（查询失败）。"""
    out: dict[str, Any] = {
        "query_status": query_status,
        "collected_at": now_iso(),
        "ttl_seconds": ttl_seconds,
    }
    out.update(extra)
    return out
