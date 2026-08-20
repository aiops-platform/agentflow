# 数据源 MCP 工具层（agentflow/tools/）

按 DESIGN.md §4.5 落地：统一以 **MCP server** 接入数据源，opencode 原生支持 MCP，每个 agent
在 frontmatter 挂载自己的 MCP 工具集。本目录当前已实现两个数据源，其余（K8s / CMDB / 服务拓扑）随
里程碑 M3 补齐。

| 文件 | MCP server | 工具 | 消费 agent |
|---|---|---|---|
| `es_logs.py` | `es-logs` | `query_logs` | log-analyst / trace-analyst |
| `prometheus_metrics.py` | `prometheus-metrics` | `query_metrics` | metrics-analyst |
| `common.py` | — | 配置 / 时间 / evidence 封装 | 所有数据源共用 |

## 一、统一约定

1. **证据返回封装**：所有工具返回带 `query_status`（`ok` 有数据 / `empty` 查不到 / `error` 查询失败）
   + `collected_at` + `ttl_seconds`，让 root-cause 能把「查不到」和「正常」区分开（负证据语义，
   DESIGN.md §六）。
2. **配置走环境变量**：地址/凭据不进代码、不进 YAML（DESIGN.md §4.11.7）。
3. **日志记录归一化**：`query_logs` 把 ES 命中归一化成统一结构（`timestamp` / `service` / `level` /
   `trace_id` / `message` / `stack_trace` / `logger` / `pod`），与 mock 数据源返回一致，保证
   「agent 挂载不变，只换数据源」时逻辑一致。

## 二、本地运行（需先 `pip install -e .` 或使用含 fastmcp 的 venv）

```bash
# 方式 1：模块运行（推荐，正式用法）
python -m agentflow.tools.es_logs
python -m agentflow.tools.prometheus_metrics

# 方式 2：opencode 直接拉起（脚本已加包根目录到 sys.path，两种方式均可）
python agentflow/tools/es_logs.py
python agentflow/tools/prometheus_metrics.py
```

## 三、挂载到 opencode（agent frontmatter）

```bash
# 日志查询（log-analyst / trace-analyst）
opencode mcp add es-logs -- python -m agentflow.tools.es_logs

# 指标查询（metrics-analyst）
opencode mcp add prometheus-metrics -- python -m agentflow.tools.prometheus_metrics
```

## 四、配置项

### ES 日志（`es_logs.py`）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ES_URL` | `http://localhost:19200` | ES 地址；K8s 内用 `http://elasticsearch:9200` |
| `ES_INDEX` | `app-logs` | 日志索引 |
| `ES_USERNAME` / `ES_PASSWORD` | 空 | 测试床关 security；生产配置 |
| `ES_SERVICE_FIELD` | `app.service` | Filebeat `target:"app"` 命名空间 |
| `ES_LEVEL_FIELD` | `app.level` | |
| `ES_TRACE_FIELD` | `app.traceId` | MDC key 为 `traceId`（camelCase） |
| `ES_MESSAGE_FIELD` | `app.message` | |
| `ES_STACK_FIELD` | `app.stack_trace` | |
| `ES_LOGGER_FIELD` | `app.logger_name` | |
| `ES_POD_FIELD` | `app.pod` | |
| `ES_TIMESTAMP_FIELD` | `@timestamp` | 时间过滤/排序用 Filebeat 采集时间 |

### Prometheus 指标（`prometheus_metrics.py`）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `PROMETHEUS_URL` | `http://localhost:19090` | Prometheus 地址；K8s 内用 `http://prometheus:9090` |

## 五、mock 数据源（L1/L2 测试）

真实数据源的 stub 版在 `testbed/mock-datasource/server.py`（工具签名一致，读 fixture JSON）。
L1/L2 测试时把 agent 挂载的 MCP server 从真实换成 mock 即可：

```bash
MOCK_FIXTURE_DIR=testbed/mock-datasource/fixtures/scenario1 \
    python testbed/mock-datasource/server.py
```

详见 `testbed/mock-datasource/`。
