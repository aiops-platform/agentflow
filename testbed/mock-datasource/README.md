# Mock 数据源（L1/L2 测试）

与真实数据源（`agentflow/tools/es_logs.py`、`agentflow/tools/prometheus_metrics.py`）**工具签名一致**，
区别仅在数据来源：真实版查 ES/Prometheus，mock 版读 fixture JSON。agent 的 frontmatter 挂载的 MCP
工具名不变，只把 server 从真实换成 mock——逻辑一致，只有数据源被 stub（SCENARIOS.md §5.2）。

## 目录

```
mock-datasource/
├── server.py
└── fixtures/
    ├── scenario1/{ticket,logs,metrics,k8s,topology,cmdb,expected}.json   # 报价单打印失败（infra）
    └── scenario2/{ticket,logs,metrics,k8s,topology,cmdb,expected}.json   # 结账无响应（code）
```

- `logs.json` / `metrics.json` 已被 `query_logs` / `query_metrics` 消费；
- `k8s.json` / `topology.json` / `cmdb.json` 预留给后续 K8s / CMDB / 服务拓扑 MCP server；
- `ticket.json` 是 workflow 触发入参；`expected.json` 是断言 golden（SCENARIOS.md §5.3）。

## 运行

```bash
# 单场景：MOCK_FIXTURE_DIR 直接指向场景目录
MOCK_FIXTURE_DIR=testbed/mock-datasource/fixtures/scenario1 \
    python testbed/mock-datasource/server.py

# 指向 fixtures 父目录时，用 MOCK_FIXTURE_SCENARIO 选场景（或工具参数 scenario）
MOCK_FIXTURE_DIR=testbed/mock-datasource/fixtures MOCK_FIXTURE_SCENARIO=scenario2 \
    python testbed/mock-datasource/server.py
```

## 工具

| 工具 | 签名（与真实一致） | 行为 |
|---|---|---|
| `query_logs` | `service / level / trace_id / query / time_range / start_time / end_time / size` + mock 独有 `scenario` | 内存过滤 fixture 的 logs.json |
| `query_metrics` | `promql / time / start / end / step` + mock 独有 `scenario` | 读 metrics.json（Prometheus 向量格式），按 promql 里的指标名 best-effort 过滤 |
