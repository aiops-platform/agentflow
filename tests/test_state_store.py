"""StateStore 真实实现测试：Postgres（降级）+ Redis（fake client）。"""
from __future__ import annotations

import asyncio

from agentflow.engine.state import PostgresStore, RedisStore


async def test_postgres_requires_asyncpg():
    # asyncpg 未装 → 清晰 ImportError（优雅降级）
    store = PostgresStore("postgresql://localhost/test")
    try:
        await store.get_run("x")
        assert False, "应抛 ImportError"
    except ImportError as e:
        assert "asyncpg" in str(e)
    print("  ✓ test_postgres_requires_asyncpg")


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}

    async def get(self, key): return self.data.get(key)

    async def set(self, key, value): self.data[key] = value

    async def expire(self, key, ttl): pass

    async def aclose(self): pass


async def test_redis_store_roundtrip():
    store = RedisStore(client=FakeRedis())
    await store.put_run("r1", {"run_id": "r1", "workflow": "w", "status": "success",
                               "context": {"nodes": {}}})
    await store.put_node("r1", "a", {"status": "done", "output": {"x": 1}})

    assert (await store.get_run("r1"))["status"] == "success"
    assert (await store.get_node("r1", "a"))["output"] == {"x": 1}
    assert await store.get_run("nope") is None
    assert await store.get_node("r1", "ghost") is None
    await store.close()
    print("  ✓ test_redis_store_roundtrip")


async def main() -> None:
    await test_postgres_requires_asyncpg()
    await test_redis_store_roundtrip()
    print("\nALL STATESTORE TESTS PASS ✅")


if __name__ == "__main__":
    asyncio.run(main())
