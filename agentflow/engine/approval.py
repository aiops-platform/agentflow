"""审批门禁：写操作 human-in-the-loop。DESIGN.md §4.7。

- 分级：低危自动执行，高危（节点 ``approve`` 字段标记）走审批。
- 驳回路径：驳回 → 节点 ``approval_rejected``，走 on_failure（不 panic / 卡死）。
- 超时默认 auto-deny（安全优先）。
- 并发审批：按 ``(run_id, node_id)`` 分 slot，互不阻塞。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApprovalRequest:
    run_id: str
    node_id: str
    permission: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    decided: bool = False
    approved: bool = False


class ApprovalManager:
    def __init__(self, mode: str = "auto", timeout_seconds: float = 300.0):
        self.mode = mode                     # auto | manual
        self.timeout_seconds = timeout_seconds
        self._requests: dict[str, ApprovalRequest] = {}
        self._events: dict[str, asyncio.Event] = {}

    def _key(self, run_id: str, node_id: str) -> str:
        return f"{run_id}:{node_id}"

    async def request(self, run_id: str, node_id: str, permission: dict | None = None) -> bool:
        """请求审批，返回是否批准。auto 直接通过；manual 等待人工决定，超时 auto-deny。"""
        if self.mode == "auto":
            return True
        key = self._key(run_id, node_id)
        req = ApprovalRequest(run_id=run_id, node_id=node_id, permission=permission or {})
        self._requests[key] = req
        ev = asyncio.Event()
        self._events[key] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            req.decided = True
            req.approved = False            # 超时 auto-deny
            return False
        return req.approved

    def decide(self, run_id: str, node_id: str, approved: bool) -> bool:
        key = self._key(run_id, node_id)
        req = self._requests.get(key)
        if req is None or req.decided:
            return False
        req.decided = True
        req.approved = approved
        self._events[key].set()
        return True

    def approve(self, run_id: str, node_id: str) -> bool:
        return self.decide(run_id, node_id, True)

    def reject(self, run_id: str, node_id: str) -> bool:
        return self.decide(run_id, node_id, False)

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in self._requests.values() if not r.decided]
