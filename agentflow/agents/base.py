"""可插拔 agent 的基础类型。DESIGN.md §4.2。

每个职能 agent = ``agents/<name>/agent.md``（system prompt 角色方法论 + frontmatter 配
model/permission/tools）+ 输出 schema（见 domain.schemas）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import BaseModel


@dataclass
class AgentSpec:
    """一个职能 agent 的运行时定义。"""
    name: str
    description: str = ""
    system_prompt: str = ""                       # agent.md 正文（角色方法论）
    output_schema: type[BaseModel] | None = None
    tools: list[str] = field(default_factory=list)  # frontmatter tools 挂载的 MCP 工具名
    model: str | None = None
    permissions: dict[str, Any] = field(default_factory=dict)
    input_view: str = "summary"                       # 输入裁剪默认（spec.py 可覆盖；节点级 input_view 优先）
    requires_sandbox: bool = False                    # 是否需要沙箱（tester / fix-implementer 为 True）


_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_agent_md(text: str) -> tuple[str, dict[str, Any]]:
    """解析 agent.md，返回 ``(正文 system_prompt, frontmatter dict)``。"""
    m = _FM_RE.match(text)
    if not m:
        return text.strip(), {}
    raw, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(raw) or {}
    except Exception:  # noqa: BLE001 —— frontmatter 解析失败不致命
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return body.strip(), fm
