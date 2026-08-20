"""EventBus：单一事件源，扇出到多个 sink。DESIGN.md §4.8。

- sink 是 Protocol，只依赖 ``on_event(Event)``；换 Langfuse/OTel/自研都零改动。
- 发布失败不阻断主流程（return_exceptions）。
"""
from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from agentflow.observability.events import Event


@runtime_checkable
class Sink(Protocol):
    async def on_event(self, event: Event) -> None: ...

    async def close(self) -> None: ...


class EventBus:
    def __init__(self) -> None:
        self._sinks: list[Sink] = []

    def subscribe(self, sink: Sink) -> None:
        self._sinks.append(sink)

    @property
    def sinks(self) -> list[Sink]:
        return list(self._sinks)

    async def publish(self, event: Event) -> None:
        if not self._sinks:
            return
        await asyncio.gather(
            *(sink.on_event(event) for sink in self._sinks), return_exceptions=True
        )

    async def close(self) -> None:
        await asyncio.gather(*(sink.close() for sink in self._sinks), return_exceptions=True)


class RecordingSink:
    """内存 sink：测试 / 调试用。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def on_event(self, event: Event) -> None:
        self.events.append(event)

    async def close(self) -> None:
        pass
