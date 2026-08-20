# AIOps Bug Fix 智能体工作流平台

把「定位 bug → 修复 bug」拆成一串**可插拔的职能智能体**，workflow 由 **YAML** 手动定义（含流向），每个 agent 的执行过程（做了什么 + 与大模型交互的 prompt/token/cost）全程可监控；底层用 **opencode** 驱动每个 agent，代码执行用 **opensandbox** 隔离。

## 文档导航

| 文档 | 内容 |
|---|---|
| [DESIGN.md](DESIGN.md) | 技术选型与架构设计（含里程碑 M0–M5、领域 schema、评审记录） |
| [SCENARIOS.md](SCENARIOS.md) | 两个故障场景 + 测试床搭建 + 踩坑记录 |
| [SECURITY.md](SECURITY.md) | 沙箱安全基线 |
| [spike/README.md](spike/README.md) | 4 个关键技术风险的 spike 验证记录 |

## 怎么使用（快速开始）

### 0. 前置条件

| 依赖 | 说明 |
|---|---|
| Python 3.11+ | 平台运行环境 |
| opencode | 驱动 agent 的 LLM 运行时，先启动：`opencode serve --hostname 127.0.0.1 --port 4090` |
| ES / Prometheus / K8s（可选） | 数据源；本地测试床见 `testbed/README.md`，或 L1/L2 用 mock 数据源 |

### 1. 安装

```bash
pip install -e .
# 依赖：fastmcp / httpx / pydantic / pyyaml / fastapi / uvicorn
```

### 2. 接线到 opencode（注册 MCP + 生成 agent）

让 opencode 认识平台的数据源工具和职能 agent：

```bash
python -m agentflow opencode-setup --apply
# 作用：
#   1) 注册 6 个 MCP server：es-logs / prometheus-metrics / k8s / cmdb / service-topology / opensandbox
#   2) 把 agents/*/agent.md 生成到 ~/.config/opencode/agents/<name>.md（成为 opencode subagent）
```

### 3. 校验 workflow

```bash
python -m agentflow validate examples/bug-fix-pipeline.yaml
# 输出：节点数/边数/拓扑序；检测环、悬空节点、非法 JSONPath 引用
```

### 4. 运行 workflow

```bash
python -m agentflow run examples/order-service-quotation-print-fail.yaml \
    --trigger testbed/mock-datasource/fixtures/scenario1/ticket.json
# 从 ticket 一路跑到 postmortem，每个节点 = 一个 opencode session，输出结构化结果
# 运行过程实时打印每节点的输入 prompt + 输出 output + 状态（ConsoleSink）
# trigger 文件格式：{"repo": "...", "bug_report": {...工单字段...}}，匹配 workflow 的 inputs 定义
```

断点续跑（已 done 节点幂等跳过、failed 节点重跑）：

```bash
python -m agentflow run examples/order-service-quotation-print-fail.yaml --resume <run_id>
```

### 5. 列出可插拔 agent

```bash
python -m agentflow list
```

### 6. REST API（生产入口 / Alertmanager webhook）

```bash
uvicorn agentflow.server:app --host 0.0.0.0 --port 8000
curl -X POST localhost:8000/run -H 'Content-Type: application/json' \
     -d '{"workflow": "examples/bug-fix-pipeline.yaml", "ticket": {...}}'
```

## 命令总览

| 命令 | 作用 |
|---|---|
| `agentflow validate <wf.yaml>` | 静态校验（环 / 悬空节点 / JSONPath 合法性 + 拓扑序） |
| `agentflow run <wf.yaml> [--trigger bug.json] [--resume run_id] [--input '{...}'] [--invalidate-from node_id]` | 运行 workflow（resume 时 `--input` 补料、`--invalidate-from` 作废该节点及下游重跑） |
| `agentflow list` | 列出可插拔 agent |
| `agentflow inspect <run_id>` | 查看某次 run 各节点的输入 prompt + 结构化输出 + 原始 stdout |
| `agentflow pause <run_id>` | 优雅停止某次 run（当前节点跑完即停，resume 可续跑） |
| `agentflow opencode-setup [--apply]` | 接线到 opencode（注册 MCP + 生成 agent） |

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `OPENCODE_URL` | `http://127.0.0.1:4090` | opencode serve 地址 |
| `AGENTFLOW_WORKDIR` | `./workdir` | run 工作区 + SQLite 落盘位置 |
| `AGENTFLOW_STATE_BACKEND` | `sqlite` | `sqlite` / `postgres` / `redis` / `memory` |
| `AGENTFLOW_STATE_DSN` | `sqlite:///...` | Postgres DSN（`backend=postgres` 时） |
| `AGENTFLOW_REDIS_URL` | `redis://localhost:6379/0` | Redis（`backend=redis` 时） |
| `AGENTFLOW_APPROVAL_MODE` | `auto` | `auto` 自动过审 / `manual` 人工（超时 auto-deny） |
| `AGENTFLOW_MAX_COST` / `AGENTFLOW_MAX_TOKENS` | `0`（不限） | run 级成本预算，超限自动停 |
| `SANDBOX_DOMAIN` / `SANDBOX_IMAGE` / `SANDBOX_ENTRYPOINT` | `127.0.0.1:8080` / `opensandbox/code-interpreter:v1.0.2` / `/opt/opensandbox/code-interpreter.sh` | opensandbox 沙箱 |
| `LANGFUSE_URL` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | 空 | Langfuse 自托管（LLM 层观测） |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek API key，放 `.env`（已被 gitignore）；opencode 用它驱动 agent，默认模型见 `~/.config/opencode/opencode.json` 的 `model` 字段 |

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
├── cli.py                  # python -m agentflow validate|run|list|pause|opencode-setup
├── config.py               # 本地/生产配置（环境变量驱动）+ build_store
├── server.py               # REST API（POST /run + GET /health）
├── workflow/               # workflow schema + parser + dag（拓扑/环检测）
├── domain/                 # 15 个领域 output schema（边界校验）
├── tools/                  # 数据源 MCP server（ES/指标/K8s/CMDB/拓扑 + 脱敏）
├── engine/                 # executor + context + state(4 实现) + approval
├── agents/                 # AgentRegistry（扫描 agents/ 目录）
├── opencode/               # HTTP+SSE 适配层（server_adapter）
├── sandbox/                # opensandbox 适配器 + MCP server
└── observability/          # event_bus + Langfuse/OTel sink + ConsoleSink（CLI 实时打印进度）
agents/                     # 15 个职能 agent 定义（agent.md）
examples/                   # 场景 workflow YAML
docker/                     # Dockerfile + .dockerignore
deploy/k8s/                 # 生产 K8s 清单（Deployment + Service）
testbed/                    # 测试床：manifests / fault-inject / mock-datasource / scripts
spike/                      # 关键技术风险验证
tests/                      # 单测 + 集成测试（10 个文件）
```

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 定义层 | 脚手架 + Pydantic schema + parser/dag + `validate` | ✅ 完成 |
| M1 opencode 单节点 | HTTP+SSE 封装 `server_adapter`（含 SSE 工具调用捕获） | ✅ 完成 |
| M2 编排执行器 | 并发调度 + WorkflowContext + AgentRegistry + SQLite 断点续跑 | ✅ 完成 |
| M3 数据源 MCP + 沙箱 | 数据源 MCP 全 + 出站脱敏 + opensandbox 适配器 | ✅ 完成 |
| M4 审批 + 观测 | 写操作审批门禁 + event_bus 扇出 Langfuse/OTel sink | ✅ 完成 |
| M5 部署 | Dockerfile + K8s 清单 + REST API + config 双态 | ✅ 完成 |

## 测试

```bash
# 单测 / 集成（10 个文件，独立脚本，无需真实 opencode）
for t in tests/*.py; do PYTHONPATH=. python "$t"; done
# 覆盖：adapter / executor / registry+resume / 数据源工具 / 沙箱 / 审批+观测 /
#       server / statestore / opencode-setup / L2 集成（场景1/2）
# 注：test_sandbox.py 额外需要 opensandbox SDK（可选依赖，未列入 pyproject）：
#     pip install opensandbox opensandbox-code-interpreter
```

## 测试床（端到端验收）

两个故障场景的端到端验证（minikube + 3 个微服务 + ES/Prometheus/Grafana/Kibana）见 [testbed/README.md](testbed/README.md)：

```bash
cd testbed && ./build-and-deploy.sh          # 一键打包→镜像→发布
testbed/scripts/port-forward-all.sh          # 端口转发（自动重连）
```
