# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这是什么

AIOps Bug Fix 智能体工作流平台：把「定位 bug → 修复 bug」拆成一串可插拔的职能智能体，workflow 由 **YAML** 手动定义（含流向），底层用 **opencode** 驱动每个 agent，代码执行用 **opensandbox** 隔离。详见 `DESIGN.md`（架构/里程碑）、`SCENARIOS.md`（场景+测试床）、`SECURITY.md`（沙箱安全）。

## 设计意图（从 DESIGN.md / SCENARIOS.md 提炼）

核心思想：opencode 只提供「单智能体 = 一个 session」的原子能力（内置代码工具 + 权限 + 事件流），是 bug fix 的强项；它原生只有「primary → subagent」树形委托、**没有 DAG**，所以 DAG 编排、并行、条件、上下文传递、观测全部自建。分工是：

- **代码类 agent**（定位/修复/测试）复用 opencode 内置工具（read/edit/grep/glob/bash），不重复造工具。
- **数据类 agent**（日志/链路/指标/K8s/CMDB/拓扑）通过 **MCP** 接外部数据源。
- **代码执行**（跑测试/编译/复现）一律走 **opensandbox** 沙箱（MCP 工具 `run_python`/`run_shell`），**禁用 opencode 内置 `bash`**——内置 bash 跑在宿主机，唯一真隔离路径是 MCP 沙箱工具（spike 3 已证明）。
- **静态 DAG 够用**：条件边 `when` + 重试即可，无循环/动态子流程；修复循环靠 fix-implementer 节点内部迭代，不靠 DAG 回环。

### Agent 编队（15 个 = 诊断侧 + 解决侧）

- **诊断侧（全只读，`edit: deny` + `bash: deny`）**：triage → 并行 log-analyst / trace-analyst / metrics-analyst / infra-locator / code-locator / knowledge-lookup → root-cause。
- **解决侧（写操作走审批门禁）**：fix-planner → 并行 fix-implementer（改代码）+ infra-remediator（改 K8s/止血）→ tester → reviewer → committer → postmortem。

权限分级（agent.md frontmatter 的 `permission` 字段）：诊断侧 deny 读写；fix-implementer `edit: allow`（限 run 工作区副本）+ `bash: deny`；tester `bash: deny`；infra-remediator / committer 的写操作 `ask`（human-in-the-loop 审批，超时 auto-deny）。

### 两个故障场景（驱动整个平台，SCENARIOS.md）

| 维度 | 场景 1 | 场景 2 |
|---|---|---|
| 现象 | 报价单打印失败（**报错**） | 结账无响应（**挂起，无报错**） |
| 根因类型 | 基础设施：CPU 100% + 磁盘 100% | 代码 bug：fin 缺参 + 空 catch 吞异常 + Feign 无超时 |
| 根因服务 | order-service（ticket 同服务） | warranty-service（**ticket 下游**） |
| 胜负手 agent | metrics-analyst + infra-locator + infra-remediator | trace-analyst（跨服务）+ code-locator（多仓库） |
| symptom_type | crash | hang |

关键：**同一套 15-agent 编队 + 同一 DAG，靠「节点是否出现」切换场景**（场景 2 无 infra-remediator）。`BugReport.symptom_type`（crash/hang/slow/wrong_output）驱动诊断侧重。场景 2 的核心诊断价值：`first_error: null` + 下游故障 span → 定位「吞异常 + 无超时」。

### 关键设计决策的「为什么」（都在 domain schema / executor 里落地）

- **summary/details 双分层**：AIOps 结构化 output 单字段可达 10–50KB（error_stack / span_tree / diff），不能全局字符截断——summary（≤500 token）传下游，details 只落 StateStore。
- **`query_status: ok/empty/error` 负证据语义**：root-cause 只把 `ok` 的「正常」当负证据，`empty/error` 标「证据不足」，避免把「查不到」误判成「正常」。
- **失败节点固定 `FailedOutput`**（`{status:failed, error_reason}`）：`on_failure: continue` 时下游读到确定结构，避免把「空证据」当「无异常」。
- **证据时效**：证据类 schema 带 `collected_at` + `ttl_seconds`（AIOps 特有：日志轮转/采样 TTL）。
- **隐式依赖边**：`params` 引用 `$.nodes.<upstream>.output` 即产生 DAG 边，无需手动写 edges。

## ⚠️ DESIGN.md 是完整设计蓝图，部分尚未落地

DESIGN.md 是「目标架构」而非「当前实现快照」，不少章节描述的能力**代码里还没做**。接手时以实际代码为准，遇到下列概念先 grep 确认是否已实现：

| DESIGN.md 描述 | 实际实现状态 |
|---|---|
| 每 agent 一个 `spec.py`（input/output schema + pre/post 钩子） | ✅ 已实现（`agents/<name>/spec.py` 声明 input_view/requires_sandbox；pre/post 钩子未做） |
| `input_view` 三种传递策略（Summary/Reference/Full） | ✅ 已实现 summary/full 两态（NodeDef.input_view + spec.py；Reference 态未做） |
| 汇合 two_phase 深挖 + `read_upstream_output` MCP 工具 | ✅ 已实现 read_upstream_output 工具 + prompt 注入 run_id（two_phase 自动深挖未做） |
| CLI `pause` / `resume --input` / `--invalidate-from` | ✅ 已实现 |
| 断点续跑 resume 原 opencode session（存 `session_id`） | ✅ 已实现（checkpoint 存 session_id，resume 用原 session） |
| run 级成本预算（token/cost 上限自动停） | ✅ 已实现（AGENTFLOW_MAX_COST/AGENTFLOW_MAX_TOKENS） |
| ServiceTopology 离线聚合 job（trace→拓扑 + 同步 CMDB） | ⚠️ 半成品：MCP 查询工具 4 个已完整（读静态 `data/topology.json`）；离线聚合 job 未做，拓扑是手写 JSON |
| OTel `MetricsSink` 导出到 OTel | ⚠️ 只内存聚合 metrics（node/token/cost），未接 OTel SDK/collector，不导出 |
| Langfuse `LlmTraceSink` 推送到 Langfuse | ⚠️ 只建本地 `self.traces` 结构；`_langfuse` client 建了但从没调 trace/generation API，`flush()` 是 no-op |

补充两个「半接线」点（也是 DESIGN 描述了但没完全打通）：

- **观测实际能用的路径** = StateStore 记录每节点 `prompt`/`output` + `inspect` 命令（本地回看）+ `ConsoleSink`（CLI 实时打印每节点 prompt/output）。远程 Langfuse/OTel 仍是半成品（两个 sink 都不真推送）。
- **审批**：`ApprovalManager` 逻辑完整（approve/reject 两态、按 node 并发、超时 auto-deny），但 `server.py` 只有 `POST /run` + `GET /health`，**无 approve/reject 端点**——manual 模式的 `decide()` 只能进程内调用。另：opencode 侧的 `permission.asked`（`external_directory` 等工具级权限）已由 `server_adapter` 自动 `reply=once`，不再卡住 fix/tester 这类写操作 agent。

milestone M0–M5 在 README 标 ✅，指的是「该里程碑的主干已通」，不等于 DESIGN 里每个细节都落地了。此外 `tools/README.md` 里「当前只实现两个数据源」已过时——5 个数据源 MCP（es-logs/prometheus-metrics/k8s/cmdb/topology）实际都已实现。

## TODO / 后续迭代（剩余半成品）

6 个核心功能（input_view / two_phase / 成本预算 / spec.py / pause+resume / session resume）已实现，详见上表 ✅ 行。剩余待办：

**半成品（上表 ⚠️ 行）**
- [ ] ServiceTopology 离线聚合 job（现为手写静态 `data/topology.json`）
- [ ] OTel `MetricsSink` 真导出 + Langfuse `LlmTraceSink` 真推送
- [ ] 审批 approve/reject HTTP 端点（`server.py` 只有 `/run` + `/health`）

**已实现的 6 项的遗留小尾巴**
- [ ] `input_view` 的 Reference 态（传引用 + 按需拉，当前只有 summary/full 两态）
- [ ] two_phase 自动深挖（当前 read_upstream_output 由 agent 主动调，未做「先 summary 再自动深挖」的 prompt 模板）
- [ ] `spec.py` 的 pre/post 钩子（当前只声明 input_view/requires_sandbox）

## 常用命令

```bash
# 安装（也可不装，直接用 `python -m agentflow`，agentflow/ 在仓库根）
pip install -e .

# 静态校验 workflow.yaml（环 / 悬空节点 / 非法 JSONPath + 拓扑序）
python -m agentflow validate examples/bug-fix-pipeline.yaml

# 列出可插拔 agent
python -m agentflow list

# 运行 workflow（需先启动 opencode serve + opencode-setup，见下）
python -m agentflow run examples/order-service-quotation-print-fail.yaml \
    --trigger tests/mock-datasource/fixtures/scenario1/ticket.json
python -m agentflow run <wf.yaml> --resume <run_id>   # 断点续跑（跳过 done、重跑 failed）
python -m agentflow run <wf.yaml> --resume <run_id> --input '{"x":1}'          # 补料续跑
python -m agentflow run <wf.yaml> --resume <run_id> --invalidate-from rca      # 作废 rca 及下游重跑
python -m agentflow pause <run_id>                    # 优雅停止（当前节点跑完即停）

# 查看某次 run 各节点的输入 prompt + 结构化输出 + 原始 stdout
python -m agentflow inspect <run_id>

# 接线到 opencode（注册 7 个 MCP server + 生成 agent 到 ~/.config/opencode/agents/）
python -m agentflow opencode-setup --apply

# 测试：tests/*.py 是独立脚本（assert 风格，非 pytest），无需真实 opencode
for t in tests/*.py; do PYTHONPATH=. python "$t"; done
PYTHONPATH=. python tests/test_executor.py   # 单跑一个
```

### 依赖说明（哪些测试需要额外装包）

- `pip install -e .` 装的是核心依赖（fastmcp/httpx/pydantic/pyyaml/fastapi/uvicorn），够跑 L2 编排测试（`test_integration.py`）和大部分测试。
- **沙箱测试 `test_sandbox.py` 额外需要 opensandbox SDK，要装两个包**（`pyproject.toml` 故意没声明，属可选依赖）：
  ```bash
  pip install opensandbox opensandbox-code-interpreter
  # opensandbox → Sandbox 类；opensandbox-code-interpreter → code_interpreter 模块
  # 注意 import 名是 `from code_interpreter import ...`，与包名不同，别只装 opensandbox 一个
  ```
  装这两个只够跑 fake 沙箱测试；真实沙箱链路还需 opensandbox-server 服务 + podman（见 `spike/README.md`）。

跑 `run` 前的真实链路前置：`opencode serve --hostname 127.0.0.1 --port 4090` 已启动，且 `opencode-setup --apply` 已执行过（否则 agent 未注册成 opencode subagent）。

## 架构

数据流：`workflow.yaml` → Pydantic parse + `validate`（`workflow/dag.py`）→ `DAGExecutor`（`engine/executor.py`，asyncio 编排）→ 每个节点 = 一个 opencode session（`OpenCodeAdapter`）→ agent 最终文本解析成 JSON → 按 agent 的 output schema 校验 → 写回 `WorkflowContext` → 下游节点用 JSONPath 读。

关键模块（`agentflow/` 包）：

- `workflow/`：`schema.py`（Pydantic：nodes/edges/inputs）、`parser.py`、`dag.py`（拓扑排序/环检测/悬空节点/JSONPath 校验）。
- `engine/`：`executor.py`（编排核心）、`context.py`（`WorkflowContext` + JSONPath 求值 + `eval_when`）、`state.py`（StateStore 4 实现）、`approval.py`（写操作审批门禁）、`artifact.py`。
- `agents/`：`AgentRegistry` 扫描 `agents/*/agent.md`；每个 agent = frontmatter（`permission`/`model`/`tools`）+ 正文（system prompt 角色方法论）。
- `domain/schemas.py`：`AGENT_OUTPUT_SCHEMAS` —— 每个 agent 的结构化输出 Pydantic 模型，用于输出校验。
- `opencode/`：`adapter.py`（`AgentRuntime` Protocol，防腐层）+ `server_adapter.py`（HTTP+SSE 真实实现）。
- `tools/`：数据源 MCP server（es-logs / prometheus-metrics / k8s / cmdb / topology），含 `masking.py` 脱敏。
- `sandbox/`：opensandbox 适配器 + MCP server。
- `observability/`：`event_bus` 扇出 Langfuse / OTel sink + `console.py`（`ConsoleSink`，CLI 实时打印每节点 prompt/output）。
- `opencode_setup.py`：注册 MCP + 生成 opencode agent；`MCP_SERVERS` / `MCP_TOOLS` / `OPENCODE_TOOLS` 三张映射表在此。
- 顶层 `agents/`（仓库根，非包）＝15 个职能 agent 定义；`examples/`＝场景 workflow YAML。
- `server.py`：REST API——workflow CRUD（`/workflows`，SQLite workflows 表存 YAML）+ run 异步触发（`POST /run` → 后台 task，返回 run_id）+ 查询（`GET /runs/:id` 聚合 nodes 表节点级 checkpoint）+ 停止（`POST /runs/:id/stop` 落 cancelled）。

**前端**（独立 repo [github.com/xqfgbc/agentflow-ui](https://github.com/xqfgbc/agentflow-ui)，Vue3 + Vite + VueFlow，**不在本仓库**）：流程配置页（YAML→节点图→保存/复用）+ Bug 解决页（流程图实时三态 + 节点 prompt/output/token 格式化 + 总 token）。dev 代理 `/workflows /run /runs /health` 到后端 8000；nginx 容器代理到 `agentflow:8000`。K8s 前后端同在 `aiops` namespace。

### 必须理解的几个核心机制

- **DAG 边 = 显式 `edges` + 隐式边**：节点 `params` 里写 `$.nodes.<upstream>.output` 即自动产生依赖边（`workflow/dag.py:build_edges`）。YAML 里 `params` 是 JSONPath 字符串引用，运行时由 `WorkflowContext.resolve_params` 解析成具体值。
- **上下文命名空间分区**：`WorkflowContext.data` = `{meta, inputs, nodes}`，每个节点只写自己的 `nodes.<id>`，天然避免并行分支写冲突。引用路径：`$.inputs.*` / `$.meta.*` / `$.nodes.<id>.output.*`。
- **trigger 文件格式**：`--trigger` 文件必须匹配 workflow 的 `inputs` 定义，即 `{"repo": "...", "bug_report": {...}}`（不是 ServiceNow 扁平工单格式），否则 `$.inputs.bug_report` 会是 null、run 假成功。
- **条件边 `when`**：`eval_when` 只支持 `$.path`（真值）/ `$.path == literal` / `!= literal` 三种形式；False → 节点 skipped 并向下游传播。
- **失败分类**：infra 失败（runtime 异常/ERROR 事件）走 `retry`；logic 失败（output schema 校验不过）走 `on_failure`（`abort`/`continue`）+ `on_schema_error`（`fail`/`retry`/`coerce`）。
- **输出提取**：agent 最终文本 → 从文本里挖 JSON（`_parse_json`，支持非纯 JSON 兜底）→ 用 `spec.output_schema`（`domain` 里对应模型）`model_validate` → 落 context。失败节点写固定 `FailedOutput` 兜底结构，供 `on_failure: continue` 的下游读。
- **断点续跑**：节点级 checkpoint（`store.put_node`），resume 时已 `done` 节点幂等跳过、复用缓存 output。
- **opencode 直连的关键坑**（`server_adapter.py` 头注释）：`POST /session/:id/message` 的同步返回**不含工具调用 part**，要观测工具调用必须消费 `GET /event` 的 SSE 流（`message.part.updated`）并按 `properties.sessionID` 过滤；`run_node` 因此先挂 SSE 再发 prompt。每个节点通过 `POST /session` 的 `agent` 字段切到对应 opencode subagent。
- **StateStore 可选后端**：`AGENTFLOW_STATE_BACKEND` = `sqlite`(默认) / `postgres` / `redis` / `memory`，由 `config.py:build_store()` 分发。接口 async，节点级 upsert 契约保证并行分支互不覆盖。
- **测试三层**（`SCENARIOS.md` §5.1）：L1 agent 单测、L2 workflow 集成（`tests/test_integration.py` 用 `MockLlm` 按 agent 返回脚本化 schema 输出 + 真实 registry/executor，断言根因/症状与 golden 一致，免真实 LLM）、L3 端到端测试床（`testbed/`，minikube + 微服务 + ES/Prometheus）。`testbed/mock-datasource/` 提供与真实工具签名一致的 mock MCP server（读 fixture JSON），供 L1/L2 使用。

## 配置（环境变量）

全部经 `config.py` 的 `Settings` 收敛（凭据不进代码/YAML）。常用：`OPENCODE_URL`（默认 `http://127.0.0.1:4090`）、`AGENTFLOW_WORKDIR`（默认 `./workdir`，含 `state.db`，已被 gitignore）、`AGENTFLOW_STATE_BACKEND`/`AGENTFLOW_STATE_DSN`/`AGENTFLOW_REDIS_URL`、`AGENTFLOW_APPROVAL_MODE`（`auto`/`manual`）、`AGENTFLOW_MAX_COST`/`AGENTFLOW_MAX_TOKENS`（成本预算，0=不限）、`SANDBOX_*`、`LANGFUSE_*`。opencode 的模型/key 在 opencode 侧：默认模型在 `~/.config/opencode/opencode.json` 的 `model` 字段（当前 `deepseek/deepseek-v4-flash`），API key 放项目根 `.env`（`DEEPSEEK_API_KEY`，已 gitignore；opencode 不自动读 .env，启动 serve 时需 `set -a; source .env; set +a`）。

**沙箱（opensandbox）两个坑**：① `Sandbox.create` 的 `entrypoint` 必须是 **list**（`["/opt/opensandbox/code-interpreter.sh"]`），不是字符串——否则 `POST /v1/sandboxes` 返回 422（沙箱创建失败，agent 会静默降级宿主机）。② 沙箱链路需 opensandbox-server 跑在 `127.0.0.1:8080`（`OPENSANDBOX_INSECURE_SERVER=YES opensandbox-server --config infra/opensandbox/sandbox.toml`），否则 MCP 工具 `run_python`/`run_shell` 不可达。
