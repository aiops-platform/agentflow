"""出站脱敏：MCP 工具返回前遮蔽敏感数据。DESIGN.md §4.5 / §八（敏感数据脱敏）。

只做轻量正则脱敏（密钥/token/密码/邮箱/长数字），覆盖日志/指标/CMDB 出站的主要敏感面；
不做语义级 PII 识别（那是后续可选项）。默认开启，``MASK_SENSITIVE=false`` 关闭。
"""
from __future__ import annotations

import re
from typing import Any

from agentflow.tools.common import get_env

MASK_SENSITIVE = get_env("MASK_SENSITIVE", True, cast=bool)

_PATTERNS: list[tuple[str, str]] = [
    # 密钥/token/password 赋值
    (r"(?i)(api[_-]?key|token|password|secret|passwd)['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{6,}", r"\1=***"),
    # 邮箱
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "***@***"),
    # 长数字（信用卡/手机号/账号，11 位以上）
    (r"\b\d{11,19}\b", "***"),
]

_RE = [(re.compile(p), r) for p, r in _PATTERNS]


def mask_text(text: str | None) -> str | None:
    if text is None or not MASK_SENSITIVE:
        return text
    for pat, repl in _RE:
        text = pat.sub(repl, text)
    return text


def mask_data(obj: Any) -> Any:
    """递归遮蔽 dict/list/str 中的敏感字符串。"""
    if not MASK_SENSITIVE:
        return obj
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, list):
        return [mask_data(x) for x in obj]
    if isinstance(obj, dict):
        return {k: mask_data(v) for k, v in obj.items()}
    return obj
