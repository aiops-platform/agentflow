"""AgentRuntime 接口（防腐层，DESIGN.md §4.12）。

接口按我们的需求定义，不按库的 API 定义：``run_node`` 产出我们自己的 ``NodeEvent``，
不是 opencode 原始事件。当前实现：``server_adapter.OpenCodeAdapter``（HTTP+SSE）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from agentflow.opencode.events import NodeEvent


@runtime_checkable
class AgentRuntime(Protocol):
    """智能体运行时：每个节点 = 一个独立 session，产出事件流。"""

    async def run_node(
        self,
        agent: str,
        prompt: str,
        tools: list[str] | None = None,
    ) -> AsyncIterator[NodeEvent]:
        """运行单个节点：建 session → 发 prompt → 流式产出事件（文本/工具调用/token/cost）。

        agent：职能 agent 名（对应 agents/<name>/agent.md）；M2 由 AgentRegistry 接线。
        tools：该 agent 挂载的 MCP 工具集；opencode 经 agent frontmatter 挂载。
        """
        ...
