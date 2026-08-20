"""opencode 适配层事件类型（防腐层，DESIGN.md §4.12）。

``run_node`` 产出的是我们自己的 ``NodeEvent``，屏蔽 opencode 的 SSE 原始事件结构——
换底层运行时（自研 / AgentScope）核心代码零改动。

事件类型（对齐 DESIGN.md §4.4 捕获的关键事件）：
- session_created：建会话
- reasoning / text：模型推理/文本输出
- tool_call：工具调用（⚠️ 只能从 SSE 捕获，同步 POST /message 返回里没有）
- step_finish：token / cost（喂 Langfuse 的数据源）
- permission_asked：审批门禁
- done：session idle（完成信号）
- error：出错
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class NodeEventType(str, Enum):
    SESSION_CREATED = "session_created"
    REASONING = "reasoning"
    TEXT = "text"
    TOOL_CALL = "tool_call"
    STEP_FINISH = "step_finish"
    PERMISSION_ASKED = "permission_asked"
    DONE = "done"
    ERROR = "error"


class TokenUsage(BaseModel):
    input: int = 0
    output: int = 0
    reasoning: int = 0
    total: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TokenUsage":
        if not data:
            return cls()
        return cls(
            input=data.get("input", 0) or 0,
            output=data.get("output", 0) or 0,
            reasoning=data.get("reasoning", 0) or 0,
            total=data.get("total", 0) or 0,
        )


class ToolCall(BaseModel):
    name: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    output: str | None = None


class NodeEvent(BaseModel):
    type: NodeEventType
    session_id: str | None = None
    text: str | None = None
    tool: ToolCall | None = None
    tokens: TokenUsage | None = None
    cost: float | None = None
    permission: dict[str, Any] | None = None
    error: str | None = None
    raw: dict[str, Any] | None = None     # 原始事件（溯源）
