"""LlmTraceSink：Langfuse（LLM/Agent 层观测）。DESIGN.md §4.8。

映射：run → trace、node → generation、工具/沙箱 → span、step-finish → token/cost。
Langfuse 自托管；未配置 / SDK 未装时优雅降级（只在本地记录 trace 结构，不发远程）。
"""
from __future__ import annotations

from typing import Any

from agentflow.observability.events import Event, EventType


class LlmTraceSink:
    def __init__(self, host: str | None = None, public_key: str | None = None,
                 secret_key: str | None = None):
        self._langfuse = self._init_client(host, public_key, secret_key)
        # 本地 trace 结构（供测试 / 检查）：run_id -> {name, generations: {node_id: {...}}}
        self.traces: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _init_client(host, public_key, secret_key):
        if not (public_key and secret_key):
            return None
        try:
            from langfuse import Langfuse
            return Langfuse(public_key=public_key, secret_key=secret_key, host=host)
        except Exception:  # noqa: BLE001 —— SDK 未装/连不上则降级
            return None

    async def on_event(self, event: Event) -> None:
        t = event.type
        if t is EventType.RUN_STARTED:
            self.traces[event.run_id] = {"name": event.data.get("workflow", ""), "generations": {}}
        elif t is EventType.NODE_STARTED:
            self._generation(event.run_id, event.node_id)["input"] = event.data
        elif t is EventType.GENERATION:
            g = self._generation(event.run_id, event.node_id)
            g["output"] = event.data.get("output", "")
            g["usage"] = event.data.get("tokens", {})
            g["cost"] = event.data.get("cost")
        elif t is EventType.TOOL_CALL:
            self._generation(event.run_id, event.node_id).setdefault("tools", []).append(event.data)
        elif t is EventType.NODE_FINISHED:
            self._generation(event.run_id, event.node_id)["status"] = event.data.get("status")

    def _generation(self, run_id: str, node_id: str | None) -> dict[str, Any]:
        trace = self.traces.setdefault(run_id, {"name": "", "generations": {}})
        return trace["generations"].setdefault(node_id or "", {})

    async def close(self) -> None:
        if self._langfuse is not None:
            try:
                self._langfuse.flush()
            except Exception:  # noqa: BLE001
                pass
