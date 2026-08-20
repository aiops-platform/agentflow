"""StateStore：checkpoint / 断点续跑 / 审批等待的持久化接口。DESIGN.md §4.3 / §4.12。

接口是 async（executor 是 asyncio）。实现：
- ``InMemoryStore``：单测。
- ``SqliteStore``：本地持久化（同步 sqlite3 走 asyncio.to_thread）。
- ``PostgresStore``：生产（asyncpg，可选依赖，未装时优雅降级报错）。
- ``RedisStore``：跨进程断点续跑 / 锁（redis.asyncio，可选依赖）。

接口契约：节点级 upsert，各节点只写自己的 slot，并行分支互不覆盖。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol


class StateStore(Protocol):
    async def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    async def put_run(self, run_id: str, data: dict[str, Any]) -> None: ...

    async def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None: ...

    async def put_node(self, run_id: str, node_id: str, data: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


class InMemoryStore:
    """内存实现（单测 / MVP；跨进程不持久）。"""

    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._runs.get(run_id)

    async def put_run(self, run_id: str, data: dict[str, Any]) -> None:
        self._runs[run_id] = data

    async def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        run = self._runs.get(run_id)
        return (run.get("nodes") or {}).get(node_id) if run else None

    async def put_node(self, run_id: str, node_id: str, data: dict[str, Any]) -> None:
        run = self._runs.setdefault(run_id, {})
        nodes = run.setdefault("nodes", {})
        nodes[node_id] = data

    async def close(self) -> None:
        pass


class SqliteStore:
    """SQLite 本地持久化（同步 sqlite3 经 asyncio.to_thread，避免阻塞事件循环）。"""

    def __init__(self, path: str | Path):
        # check_same_thread=False：连接经 asyncio.to_thread 跨线程使用；用锁串行化避免竞态
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "run_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, context TEXT)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS nodes ("
                "run_id TEXT, node_id TEXT, state TEXT, PRIMARY KEY (run_id, node_id))"
            )
            self._conn.commit()

    # ── 同步底层（跑在线程里，锁串行化）──

    def _get_run_sync(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT workflow, status, context FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        return {"run_id": run_id, "workflow": row[0], "status": row[1],
                "context": json.loads(row[2]) if row[2] else {}}

    def _put_run_sync(self, run_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, workflow, status, context) VALUES (?,?,?,?)",
                (run_id, data.get("workflow"), data.get("status"),
                 json.dumps(data.get("context", {}), ensure_ascii=False)),
            )
            self._conn.commit()

    def _get_node_sync(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM nodes WHERE run_id = ? AND node_id = ?", (run_id, node_id)
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def _put_node_sync(self, run_id: str, node_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO nodes (run_id, node_id, state) VALUES (?,?,?)",
                (run_id, node_id, json.dumps(data, ensure_ascii=False)),
            )
            self._conn.commit()

    def _close_sync(self) -> None:
        with self._lock:
            self._conn.close()

    # ── async 接口 ──

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_run_sync, run_id)

    async def put_run(self, run_id: str, data: dict[str, Any]) -> None:
        await asyncio.to_thread(self._put_run_sync, run_id, data)

    async def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_node_sync, run_id, node_id)

    async def put_node(self, run_id: str, node_id: str, data: dict[str, Any]) -> None:
        await asyncio.to_thread(self._put_node_sync, run_id, node_id, data)

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)


class PostgresStore:
    """Postgres 持久化（生产）。asyncpg 可选依赖；未装时报清晰错误。"""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5, pool=None):
        self.dsn = dsn
        self._min = min_size
        self._max = max_size
        self._pool = pool              # 可注入（测试）

    async def _ensure_pool(self):
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as e:
                raise ImportError("PostgresStore 需 asyncpg：pip install asyncpg") from e
            self._pool = await asyncpg.create_pool(self.dsn, min_size=self._min, max_size=self._max)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS runs ("
                    "run_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, context TEXT)"
                )
                await conn.execute(
                    "CREATE TABLE IF NOT EXISTS nodes ("
                    "run_id TEXT, node_id TEXT, state TEXT, PRIMARY KEY (run_id, node_id))"
                )
        return self._pool

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT workflow, status, context FROM runs WHERE run_id = $1", run_id)
        if not row:
            return None
        return {"run_id": run_id, "workflow": row["workflow"], "status": row["status"],
                "context": json.loads(row["context"]) if row["context"] else {}}

    async def put_run(self, run_id: str, data: dict[str, Any]) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO runs (run_id, workflow, status, context) VALUES ($1,$2,$3,$4) "
                "ON CONFLICT (run_id) DO UPDATE SET workflow=$2, status=$3, context=$4",
                run_id, data.get("workflow"), data.get("status"),
                json.dumps(data.get("context", {}), ensure_ascii=False),
            )

    async def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state FROM nodes WHERE run_id = $1 AND node_id = $2", run_id, node_id)
        return json.loads(row["state"]) if row and row["state"] else None

    async def put_node(self, run_id: str, node_id: str, data: dict[str, Any]) -> None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO nodes (run_id, node_id, state) VALUES ($1,$2,$3) "
                "ON CONFLICT (run_id, node_id) DO UPDATE SET state=$3",
                run_id, node_id, json.dumps(data, ensure_ascii=False),
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()


class RedisStore:
    """Redis 持久化（跨进程断点续跑 / 锁）。redis.asyncio 可选依赖。"""

    def __init__(self, url: str = "redis://localhost:6379/0", *, prefix: str = "agentflow",
                 ttl_seconds: int | None = 7 * 86400, client=None):
        self.url = url
        self.prefix = prefix
        self.ttl = ttl_seconds
        self._client = client          # 可注入（测试）

    async def _ensure(self):
        if self._client is None:
            try:
                import redis.asyncio as aioredis
            except ImportError as e:
                raise ImportError("RedisStore 需 redis：pip install redis") from e
            self._client = aioredis.from_url(self.url, decode_responses=True)
        return self._client

    def _run_key(self, run_id: str) -> str:
        return f"{self.prefix}:run:{run_id}"

    def _node_key(self, run_id: str, node_id: str) -> str:
        return f"{self.prefix}:node:{run_id}:{node_id}"

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        c = await self._ensure()
        raw = await c.get(self._run_key(run_id))
        return json.loads(raw) if raw else None

    async def put_run(self, run_id: str, data: dict[str, Any]) -> None:
        c = await self._ensure()
        key = self._run_key(run_id)
        await c.set(key, json.dumps(data, ensure_ascii=False))
        if self.ttl:
            await c.expire(key, self.ttl)

    async def get_node(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        c = await self._ensure()
        raw = await c.get(self._node_key(run_id, node_id))
        return json.loads(raw) if raw else None

    async def put_node(self, run_id: str, node_id: str, data: dict[str, Any]) -> None:
        c = await self._ensure()
        key = self._node_key(run_id, node_id)
        await c.set(key, json.dumps(data, ensure_ascii=False))
        if self.ttl:
            await c.expire(key, self.ttl)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
