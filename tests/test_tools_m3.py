"""M3 数据源 MCP 补全测试：CMDB / 拓扑 / K8s（mock kubectl）/ 脱敏 / mock 数据源新工具。"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os

from agentflow.tools import masking


def test_masking():
    assert masking.mask_text("password=abc123def456") == "password=***"
    assert masking.mask_text("contact foo@bar.com") == "contact ***@***"
    assert masking.mask_text("card 1234567890123456") == "card ***"
    masked = masking.mask_data({"msg": "token=abcdef", "nested": [{"email": "a@b.com"}]})
    assert masked["msg"] == "token=***"
    assert masked["nested"][0]["email"] == "***@***"
    print("  ✓ test_masking")


async def test_cmdb_and_topology():
    import agentflow.tools.cmdb as cmdb
    import agentflow.tools.topology as topo

    ci = await cmdb.get_ci("order-service")
    assert ci["query_status"] == "ok" and ci["ci"]["owner"] == "order-team"
    assert (await cmdb.get_ci("nope"))["query_status"] == "empty"

    assert (await topo.get_service("warranty-service"))["meta"]["repo"].endswith("aiops-test-warranty-service")
    assert (await topo.get_dependencies("order-service"))["dependencies"] == ["warranty-service"]
    assert (await topo.get_dependents("warranty-service"))["dependents"] == ["order-service"]
    assert (await topo.get_dependents("warranty-data-service"))["dependents"] == ["warranty-service"]
    assert (await topo.get_path("order-service", "warranty-data-service"))["path"] == \
        ["order-service", "warranty-service", "warranty-data-service"]
    print("  ✓ test_cmdb_and_topology")


async def test_k8s_readonly_and_mock():
    import agentflow.tools.k8s as k8s

    async def fake_kubectl(*args):
        if args[0] == "get" and args[1] == "pod":
            return 0, json.dumps({"status": {"phase": "Running"}}), ""
        if args[0] == "get" and args[1] == "events":
            return 0, json.dumps({"items": [{"type": "Normal"}]}), ""
        return 1, "", "boom"

    k8s._runner = fake_kubectl
    r = await k8s.describe_pod("order-service")
    assert r["query_status"] == "ok" and r["pod_json"]["status"]["phase"] == "Running"
    assert (await k8s.get_events())["events"]["items"][0]["type"] == "Normal"

    # 只读模式：写操作被拒绝
    k8s.K8S_READONLY = True
    assert (await k8s.scale("order-service", 3))["query_status"] == "error"
    k8s.K8S_READONLY = False
    print("  ✓ test_k8s_readonly_and_mock")


def test_mock_datasource_new_tools():
    os.environ["MOCK_FIXTURE_DIR"] = "testbed/mock-datasource/fixtures"
    spec = importlib.util.spec_from_file_location("ms3", "testbed/mock-datasource/server.py")
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)

    assert ms.get_ci(scenario="scenario2", service="warranty-service")["ci"]["owner"] == "warranty-team"
    assert ms.get_dependencies(scenario="scenario2", service="order-service")["dependencies"] == ["warranty-service"]
    assert ms.get_dependents(scenario="scenario2", service="warranty-service")["dependents"] == ["order-service"]
    assert ms.get_path(scenario="scenario2", a="order-service", b="warranty-data-service")["path"] == \
        ["order-service", "warranty-service", "warranty-data-service"]
    pods = ms.describe_pod(scenario="scenario1", pod="order-service-7b9c8d5f6-abcde")
    assert pods["query_status"] == "ok" and pods["pods"][0]["cpu_throttled"] is True
    print("  ✓ test_mock_datasource_new_tools")


async def test_read_upstream():
    import sqlite3
    import tempfile
    from pathlib import Path
    import agentflow.tools.read_upstream as ru

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "state.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE nodes (run_id TEXT, node_id TEXT, state TEXT)")
        conn.execute("INSERT INTO nodes VALUES (?, ?, ?)",
                     ("r1", "a", json.dumps({"output": {"summary": "磁盘满", "details": {"error_stack": "AAAA"}}})))
        conn.commit(); conn.close()
        ru.STATE_DB = db  # 覆盖全局 STATE_DB 指向临时库

        r = await ru.read_upstream_output("r1", "a")
        assert r["status"] == "ok" and r["output"]["summary"] == "磁盘满"
        r2 = await ru.read_upstream_output("r1", "a", "details.error_stack")
        assert r2["value"] == "AAAA"
        assert (await ru.read_upstream_output("r1", "ghost"))["status"] == "not_found"
    print("  ✓ test_read_upstream")


async def main() -> None:
    test_masking()
    await test_cmdb_and_topology()
    await test_k8s_readonly_and_mock()
    test_mock_datasource_new_tools()
    await test_read_upstream()
    print("\nALL M3 TOOLS TESTS PASS ✅")


if __name__ == "__main__":
    asyncio.run(main())
