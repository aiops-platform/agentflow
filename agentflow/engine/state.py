"""StateStore：checkpoint / 断点续跑 / 审批等待的持久化接口。DESIGN.md §4.3 / §4.12。

M2 用 InMemoryStore（单测 + MVP）；本地 SQLite / 生产 Postgres+Redis 在 M5 补齐。
接口契约：节点级 upsert，各节点只写自己的 slot，并行分支互不覆盖。
"""
from __future__ import annotations

from typing import Any, Protocol


class StateStore(Protocol):
    """状态存储接口（谁创建谁销毁：executor 持有）。"""

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def put_run(self, run_id: str, data: dict[str, Any]) -> None: ...

    def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None: ...

    def put_node(self, run_id: str, node_id: str, data: dict[str, Any]) -> None: ...


class InMemoryStore:
    """内存实现（单测 / MVP；跨进程不持久）。"""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._runs.get(run_id)

    def put_run(self, run_id: str, data: dict[str, Any]) -> None:
        self._runs[run_id] = data

    def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        if not run:
            return None
        return (run.get("nodes") or {}).get(node_id)

    def put_node(self, run_id: str, node_id: str, data: dict[str, Any]) -> None:
        run = self._runs.setdefault(run_id, {})
        nodes = run.setdefault("nodes", {})
        nodes[node_id] = data
