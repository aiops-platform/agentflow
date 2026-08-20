"""M5 REST API + config 双态测试。"""
from __future__ import annotations

import tempfile
from pathlib import Path

import agentflow.config as config
import agentflow.server as server
from agentflow.engine import DAGExecutor
from agentflow.engine.state import InMemoryStore, PostgresStore, RedisStore
from agentflow.opencode import NodeEvent, NodeEventType
from fastapi.testclient import TestClient


class _FakeRuntime:
    async def run_node(self, agent, prompt, tools=None):
        yield NodeEvent(type=NodeEventType.TEXT,
                        text='{"summary": "报价单打印失败", "symptom_type": "crash"}')
        yield NodeEvent(type=NodeEventType.DONE)


class _FakeAdapter:
    async def aclose(self): ...


def _fake_build_executor(runtime):
    return DAGExecutor(_FakeRuntime(), store=InMemoryStore())


def _write_workflow() -> str:
    content = """
name: server-test
inputs:
  bug: { type: object }
nodes:
  triage: { agent: triage, params: { bug: "$.inputs.bug" } }
"""
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return f.name


def test_health_and_run():
    # monkeypatch：免真实 opencode
    server._build_executor = _fake_build_executor
    server.OpenCodeAdapter = _FakeAdapter

    client = TestClient(server.app)
    assert client.get("/health").json() == {"status": "ok"}

    wf_path = _write_workflow()
    resp = client.post("/run", json={"workflow": wf_path, "ticket": {"bug": {"title": "x"}}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["nodes"]["triage"]["status"] == "done"
    print("  ✓ test_health_and_run")


def test_build_store_dual_mode():
    orig = config.settings.state_backend

    config.settings.state_backend = "memory"
    assert isinstance(config.build_store(), InMemoryStore)

    config.settings.state_backend = "postgres"
    assert isinstance(config.build_store(), PostgresStore)

    config.settings.state_backend = "redis"
    assert isinstance(config.build_store(), RedisStore)

    config.settings.state_backend = orig
    print("  ✓ test_build_store_dual_mode")


def main() -> None:
    test_health_and_run()
    test_build_store_dual_mode()
    print("\nALL M5 SERVER TESTS PASS ✅")


if __name__ == "__main__":
    main()
