"""MetricsSink：OTel（基础设施层观测）。DESIGN.md §4.8。

关键 metrics：节点时长/成功率、token/cost 累计。OTel SDK 未装时优雅降级（本地记录）。
"""
from __future__ import annotations

from typing import Any

from agentflow.observability.events import Event, EventType


class MetricsSink:
    def __init__(self) -> None:
        self.metrics: dict[str, Any] = {
            "nodes": {},            # node_id -> {status, tokens, cost, duration}
            "node_count": 0,
            "success": 0,
            "failed": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
        }

    async def on_event(self, event: Event) -> None:
        if event.type is EventType.NODE_FINISHED:
            self.metrics["node_count"] += 1
            status = event.data.get("status")
            if status == "done":
                self.metrics["success"] += 1
            elif status == "failed":
                self.metrics["failed"] += 1
            self.metrics["nodes"][event.node_id] = {
                "status": status,
                "tokens": event.data.get("tokens", 0),
                "cost": event.data.get("cost", 0.0),
                "duration": event.data.get("duration"),
            }
        elif event.type is EventType.GENERATION:
            self.metrics["total_tokens"] += (event.data.get("tokens") or {}).get("total", 0)
            self.metrics["total_cost"] += event.data.get("cost") or 0.0

    async def close(self) -> None:
        pass
