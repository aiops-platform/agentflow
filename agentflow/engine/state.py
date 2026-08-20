"""StateStore：checkpoint / 断点续跑 / 审批等待的持久化接口。DESIGN.md §4.3 / §4.12。

实现：InMemoryStore（单测）/ SqliteStore（本地持久化，断点续跑）。生产 Postgres+Redis 在 M5 补齐。
接口契约：节点级 upsert，各节点只写自己的 slot，并行分支互不覆盖。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
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


class SqliteStore:
    """SQLite 本地持久化（断点续跑）。同步 sqlite3，checkpoint 写极小、可忽略阻塞。

    生产换 Postgres/Redis 时只换本实现，executor 核心零改动。
    """

    def __init__(self, path: str | Path):
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "run_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, context TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS nodes ("
            "run_id TEXT, node_id TEXT, state TEXT, PRIMARY KEY (run_id, node_id))"
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT workflow, status, context FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        return {"run_id": run_id, "workflow": row[0], "status": row[1],
                "context": json.loads(row[2]) if row[2] else {}}

    def put_run(self, run_id: str, data: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, workflow, status, context) VALUES (?,?,?,?)",
            (run_id, data.get("workflow"), data.get("status"),
             json.dumps(data.get("context", {}), ensure_ascii=False)),
        )
        self._conn.commit()

    def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT state FROM nodes WHERE run_id = ? AND node_id = ?", (run_id, node_id)
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def put_node(self, run_id: str, node_id: str, data: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO nodes (run_id, node_id, state) VALUES (?,?,?)",
            (run_id, node_id, json.dumps(data, ensure_ascii=False)),
        )
        self._conn.commit()
