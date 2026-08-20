"""观测事件类型（event_bus 单一事件源）。DESIGN.md §4.8。

event_bus 统一消费 opencode 事件（经 adapter 的 NodeEvent）+ executor 编排事件 + 沙箱事件，
打上 run_id/node_id 后扇出到 Langfuse（LLM 层）/ OTel（基础设施层）两个 sink。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    NODE_STARTED = "node_started"
    NODE_FINISHED = "node_finished"
    GENERATION = "generation"           # 一次 LLM step：prompt/completion/tokens/cost
    TOOL_CALL = "tool_call"
    PERMISSION_ASKED = "permission_asked"
    APPROVAL_DECIDED = "approval_decided"


@dataclass
class Event:
    type: EventType
    run_id: str
    node_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
