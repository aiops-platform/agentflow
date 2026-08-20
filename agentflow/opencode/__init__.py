"""opencode 适配层（DESIGN.md §4.4）。

- ``adapter.py``：``AgentRuntime`` 接口（防腐层）
- ``events.py``：我们自己的事件类型（``NodeEvent``）
- ``server_adapter.py``：HTTP+SSE 直连实现（主实现，spike 已验证）
"""
from agentflow.opencode.adapter import AgentRuntime  # noqa: F401
from agentflow.opencode.events import NodeEvent, NodeEventType, TokenUsage, ToolCall  # noqa: F401
from agentflow.opencode.server_adapter import OpenCodeAdapter  # noqa: F401

__all__ = [
    "AgentRuntime",
    "OpenCodeAdapter",
    "NodeEvent",
    "NodeEventType",
    "TokenUsage",
    "ToolCall",
]
