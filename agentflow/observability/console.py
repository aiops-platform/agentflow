"""ConsoleSink：把 run 进度 + 每个节点的输入 prompt / 输出 output 打印到 stdout。

供 CLI 场景订阅（server.py 不订阅，避免污染服务日志）。DESIGN.md §4.8 的第三个 sink，
面向本地调试/观察，职责是「人肉看进度」，与 Langfuse/OTel 的「机器观测」互补。
"""
from __future__ import annotations

from agentflow.observability.events import Event, EventType


class ConsoleSink:
    """CLI 进度输出：节点开始 / 每节点输入 prompt + 输出 / 节点结束。"""

    async def on_event(self, event: Event) -> None:
        t = event.type
        if t is EventType.RUN_STARTED:
            print(f"\n▶ run {event.run_id} 开始（workflow: {event.data.get('workflow')}）")
        elif t is EventType.NODE_STARTED:
            print(f"\n{'─' * 72}\n▶ [{event.node_id}] agent={event.data.get('agent')} 开始")
        elif t is EventType.GENERATION:
            print("  ── 输入 prompt ──")
            print(event.data.get("prompt", "") or "(空)")
            print("  ── 输出 output ──")
            print(event.data.get("output", "") or "(空)")
        elif t is EventType.NODE_FINISHED:
            status = event.data.get("status")
            marker = {"done": "✅", "failed": "❌", "skipped": "⏭️", "cancelled": "🚫"}.get(status, "?")
            dur = event.data.get("duration")
            dur_s = f"{dur:.1f}s" if isinstance(dur, (int, float)) else "-"
            print(f"{marker} [{event.node_id}] {status} "
                  f"(tokens={event.data.get('tokens', 0)}, cost={event.data.get('cost', 0.0)}, "
                  f"耗时={dur_s})")
        elif t is EventType.RUN_FINISHED:
            print(f"\n{'=' * 72}\nrun {event.run_id} 结束：{event.data.get('status')}")

    async def close(self) -> None:
        pass
