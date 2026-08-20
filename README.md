# AIOps Bug Fix 智能体工作流平台

把「定位 bug → 修复 bug」拆成一串**可插拔的职能智能体**，workflow 由 **YAML** 手动定义（含流向），每个 agent 的执行过程（做了什么 + 与大模型交互的 prompt/token/cost）全程可监控；底层用 **opencode** 驱动每个 agent，代码执行用 **opensandbox** 隔离。

## 文档导航

| 文档 | 内容 |
|---|---|
| [DESIGN.md](DESIGN.md) | 技术选型与架构设计（含里程碑 M0–M5、领域 schema、评审记录） |
| [SCENARIOS.md](SCENARIOS.md) | 两个故障场景 + 测试床搭建 + 踩坑记录 |
| [SECURITY.md](SECURITY.md) | 沙箱安全基线 |
| [spike/README.md](spike/README.md) | 4 个关键技术风险的 spike 验证记录 |

## 架构一览

```
workflow.yaml（手动编写）
   │ parse/validate (Pydantic)
   ▼
DAGExecutor（asyncio 编排：拓扑排序 → 并发调度 → 条件边/重试）
   │ 每节点 = 一个 opencode session
   ├── opencode 内置工具（代码定位/修复/测试用 read/edit/grep/bash）
   ├── 数据源 MCP（日志 ES / 指标 Prometheus / K8s / CMDB / 拓扑）
   └── opensandbox 沙箱（MCP 工具 run_python/run_shell，隔离代码执行）
```

## 项目结构

```
agentflow/
├── cli.py                  # python -m agentflow validate|run|list
├── config.py               # 本地/生产配置（环境变量驱动）
├── workflow/               # schema.py / parser.py / dag.py        ✅ M0
├── domain/                 # 15 个领域 output schema（边界校验）      ✅ M0
├── tools/                  # 数据源 MCP server（ES/指标）            ✅
├── engine/                 # executor/context/state                （M2）
├── agents/                 # 可插拔职能智能体注册                     （M2）
├── opencode/               # HTTP+SSE 适配层                        （M1）
├── sandbox/                # opensandbox 适配                       （M3）
└── observability/          # Langfuse + OTel                        （M4）
examples/                   # 场景 workflow YAML（bug-fix-pipeline / 场景1 / 场景2）
testbed/                    # 测试床：manifests / fault-inject / mock-datasource / scripts
spike/                      # 关键技术风险验证
```

## 快速开始

```bash
# 安装（Python 3.11+）
pip install -e .

# 静态校验 workflow（环/悬空节点/JSONPath 合法性 + 拓扑序）
python -m agentflow validate examples/bug-fix-pipeline.yaml

# 运行数据源 MCP server（供 opencode agent 挂载）
python -m agentflow.tools.es_logs            # ES 日志查询 query_logs
python -m agentflow.tools.prometheus_metrics # Prometheus 指标查询 query_metrics
```

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 定义层 | 脚手架 + Pydantic schema + parser/dag + `validate` | ✅ 完成 |
| M1 opencode 单节点 | HTTP+SSE 封装 `server_adapter`（建 session→prompt→事件→输出，含 SSE 工具调用捕获） | ✅ 完成 |
| M2 编排执行器 | 并发调度 + WorkflowContext + StateStore | ⏳ 待做 |
| M3 数据源 MCP + 沙箱 | 数据源 MCP 已全（ES/指标/K8s/CMDB/拓扑 + 出站脱敏）；opensandbox 适配待做 | ⏳ 部分完成 |
| M4 审批 + 观测 | 写操作审批门禁 + Langfuse + OTel | ⏳ 待做 |
| M5 部署 | Dockerfile + K8s 清单 | ⏳ 待做 |

## 测试床

端到端验收的测试床（minikube + 3 个微服务 + ES/Prometheus/Grafana/Kibana）见 [testbed/README.md](testbed/README.md)。启动命令：

```bash
cd testbed && ./build-and-deploy.sh          # 一键打包→镜像→发布
testbed/scripts/port-forward-all.sh          # 端口转发（自动重连）
```
