# 基于 opencode 的 AI 运维 Bug Fix 智能体 Workflow — 技术选型与架构设计

## Context（背景与目标）

构建一个 **AI 运维 Bug Fix 智能体工作流平台**：把「定位 bug → 修复 bug」拆成一串职能智能体，节点可插拔，workflow 由 **YAML 手动定义**（含流向），每个 agent 的执行过程（做了什么 + 与大模型交互的 prompt/token/cost）全程可监控；底层用 **opencode**（Python SDK）；agent 内跑测试/编译等代码执行用 **opensandbox** 隔离。

已确认的决策：
- **场景**：AI 运维 Bug Fix（诊断 bug 的 agent + 修复 bug 的 agent）。
- **编排层**：自研轻量编排器，YAML 为唯一事实来源；**静态 DAG 够用**（条件边 `when` + 重试，无需循环/动态子流程）。
- **数据源**：日志平台 + 链路追踪（另默认纳入 CMDB + 你已有的运维知识图谱）。
- **执行**：混合分级——低危修复自动执行，高危修复走工单/审批。
- **审批**：写操作审批（human-in-the-loop）。
- **监控**：Langfuse 自托管。
- **部署**：生产 K8s + Docker 容器；本地用 Python 直接调试。
- **沙箱**：opensandbox（本地用 podman / 生产 K8s，已 spike 验证 podman 兼容）。

关键前提：
- opencode 原生只有「primary → subagent」树形委托，没有 DAG；DAG 编排由我们自建，**每个节点 = 一个 opencode session**。
- opencode 由 **HTTP+SSE 直连**驱动（已 spike 验证）：`POST /session` 建会话、`POST /session/:id/message` 发 prompt、`GET /event` 收 SSE 事件流。**不需要官方 Python SDK**（SDK 未 GA，且只是薄封装）。
- bug fix 是 opencode 原生强项：代码定位/改代码/跑测试直接复用其内置工具（read/edit/grep/glob/bash），**无需为代码类 agent 造工具**，只需为日志/链路接 MCP。

---

## 零、Spike 验证结论（已完成，详见 spike/README.md）

进入正式开发前，已用约 2 天完成 spike，验证了 3 个最大技术风险 + 1 个后续补充，**全部通过**：

| 验证项 | 结论 |
|---|---|
| opencode 能被 Python 驱动 | ✅ HTTP+SSE 直连跑通：建 session → 发 prompt → 取输出 + token/cost |
| opensandbox 能用 podman 跑 | ✅ podman 的 docker 兼容 socket 直接可用，沙箱跑 Python 返回正确结果 |
| agent 能经 MCP 调沙箱 | ✅ agent 真调用了 `run_python` 工具，沙箱执行 SHA256 结果与预期一致 |
| session 跨崩溃重启恢复 | ✅ SIGKILL 后重启，同一 session 上下文仍在（断点续跑可 resume 原 session） |

下文标记 ✅ 处为 spike 实测验证过的结论，已按实测结果修正设计。

---

## 一、技术选型总览

| 维度 | 选型 | 说明 |
|---|---|---|
| 编排引擎 | 自研 Python（asyncio） | YAML→DAG 解析 + 拓扑调度执行器 |
| 智能体运行时 | opencode（HTTP+SSE 直连 ✅） | 每节点 = 一个 opencode session；SDK 仅作可选封装 |
| 代码类工具 | opencode 内置（read/edit/grep/glob/bash） | 定位/修复/测试 agent 直接用 |
| 数据源工具 | MCP server 集合 | 日志、链路、CMDB 标准化接入 |
| 沙箱 | opensandbox | 跑测试/编译/复现用例，隔离不可信执行 |
| 监控观测 | Langfuse（LLM 层）+ OTel（基础设施层） | 两层互补，run_id/node_id 关联 |
| 状态/上下文 | 可插拔 StateStore（SQLite / Postgres+Redis） | 本地 SQLite，生产 Postgres；跨退出持久化以支持断点续跑 |
| 配置/模型 | opencode 原生 provider-agnostic | 模型在 agent.md frontmatter 配 |
| 语言/框架 | Python 3.11+、Pydantic v2、asyncio、httpx | — |

---

## 二、Agent 编队（bug fix 场景）

每个 agent = **三元组**：`agent.md`（system prompt 角色方法论 + frontmatter 配 model/permission/tools）+ Python spec（输入/输出 schema）+ **工具集**（opencode 内置 或 MCP）。按「读代码/跑测试」还是「接外部数据」分两类，权限分级如下表。

### 诊断侧（Diagnosis，全部只读）

| Agent | 职责 | 工具 | 输出（schema） |
|---|---|---|---|
| **triage 问题接入** | 接收 bug 报告/报错，规范化、评估严重级与影响面 | 工单系统（MCP，可选） | `BugReport`（标题/复现步骤/影响/严重级） |
| **log-analyst 日志分析** | 查日志平台，提取错误日志/堆栈/报错上下文 | 日志查询（MCP） | `LogEvidence`（错误类型/堆栈/实例/时间窗） |
| **trace-analyst 链路分析** | 按 traceId 关联 ES 日志，重建跨服务调用链、定位失败服务 | 日志按 traceId 关联查询（MCP，ES） | `TraceEvidence`（调用序列/失败服务/异常） |
| **metrics-analyst 指标分析** | 查 Prometheus 指标（CPU/内存/磁盘/GC），定位资源打满类根因 | metrics 查询（MCP，Prometheus） | `MetricsEvidence`（资源时序/阈值越界/时间窗） |
| **infra-locator 基础设施定位** | 查 K8s 对象状态（pod/PVC/events/重启/驱逐），确认资源载体 | K8s 只读查询（MCP，kubectl describe/get） | `InfraEvidence`（pod 状态/资源占用/事件） |
| **code-locator 代码定位** | 定位出错代码位置与调用链 | opencode 内置 grep/glob/符号搜索 | `CodeLocation`（文件:行号/调用链/可疑点） |
| **knowledge-lookup 知识检索** | 从知识图谱+历史 bug 库召回相似缺陷/已知根因/修复模式 | KG 检索（MCP，复用你已有 kg-qa） | `KnowledgeEvidence`（相似案例+根因候选+来源） |
| **root-cause 根因定位** | 综合日志+链路+代码，产出根因假设 | 只读编排（读上游证据） | `RootCause`（假设列表/置信度/证据链） |

### 解决侧（Resolution）

| Agent | 职责 | 工具 | 输出 | 审批 |
|---|---|---|---|---|
| **fix-planner 修复方案** | 根因→修复方案/改动点，评估风险等级 | 只读推理 + 知识检索 | `FixPlan`（改动点/风险等级/影响面/测试需求） | — |
| **fix-implementer 修复实现** | 改代码，**内部迭代直到测试通过** | opencode 内置 edit/write（限 run 工作区）+ MCP 沙箱工具 `run_python`/`run_shell`（edit → 自测 → 失败 → 再 edit，循环至通过或达上限） | `FixResult`（diff + env_changes） | 高危改动 `ask` |
| **infra-remediator 基础设施修复** | 改 K8s 对象（resources.limits/PVC/HPA）+ 执行止血（scale/清盘/重启） | K8s 写操作（MCP，kubectl scale/apply/exec） | `RemediationResult`（actions/diff/回滚方案/风险） | **写操作 `ask`** |
| **tester 测试验证** | 跑单测/集成/复现用例，验证无回归 | MCP 沙箱工具 `run_python`/`run_shell`（**禁用内置 bash**） | `TestResult`（通过率/失败项/回归） | — |
| **reviewer 代码审查** | 审查 diff（正确性/安全/边界） | opencode read + diff | `ReviewResult`（问题清单/是否通过） | — |
| **committer 提交/PR** | 生成 commit + PR 描述并提交 | git 操作 | `CommitResult`（commit/PR 链接） | **写操作审批** |
| **postmortem 复盘沉淀** | 生成复盘报告 | 文档生成（只读） | `Postmortem`（复盘报告） | — |

**权限分级映射（opencode permission）**：
- 诊断侧 agent：`edit: deny`、`bash: deny`（只读），仅允许各自的 MCP 查询工具。
- fix-implementer：`edit: allow`（仅限 run 工作区）、`bash: deny`（代码执行走 MCP 沙箱工具），高危文件/核心模块配 `ask`。
- tester：`bash: deny`，测试执行一律走 MCP 沙箱工具 `run_python`/`run_shell`。**⚠️ opencode 内置 `bash` 跑在宿主机，禁止用于代码执行**（spike 3 已证明 MCP 沙箱工具是唯一真隔离路径）。
- infra-remediator：`kubectl`/`helm` 写操作 `ask`（基础设施写操作审批门禁），只读查询（describe/get）放行。
- committer：`git` 写操作 `ask`（写操作审批门禁）。

> 完整场景（基础设施故障 / 跨服务代码故障）及测试床搭建详见 `SCENARIOS.md`。静态 DAG 通过「无对应节点」自然跳过不适用的 agent（如纯代码 bug 场景省略 metrics/infra/remediate）。

### 典型 workflow（YAML）

```yaml
name: bug-fix-pipeline
inputs:
  repo: { type: string, required: true }   # 多仓库场景可用映射 { service: repo }
  bug_report: { type: object, required: true }   # 触发时的原始 bug 报告
nodes:
  triage:    { agent: triage,           params: { bug: "$.inputs.bug_report" } }
  logs:      { agent: log-analyst,      params: { bug: "$.nodes.triage.output.summary" } }
  trace:     { agent: trace-analyst,    params: { bug: "$.nodes.triage.output.summary" } }
  metrics:   { agent: metrics-analyst,  params: { bug: "$.nodes.triage.output.summary" } }
  infra:     { agent: infra-locator,    params: { bug: "$.nodes.triage.output.summary" } }
  locate:    { agent: code-locator,     params: { bug: "$.nodes.triage.output.summary", repo: "$.inputs.repo" } }
  know:      { agent: knowledge-lookup, params: { bug: "$.nodes.triage.output.summary" } }
  rca:       { agent: root-cause,       params: { logs: "$.nodes.logs.output", trace: "$.nodes.trace.output",
                                                   metrics: "$.nodes.metrics.output", infra: "$.nodes.infra.output",
                                                   code: "$.nodes.locate.output", know: "$.nodes.know.output" } }
  plan:      { agent: fix-planner,      params: { rca: "$.nodes.rca.output" } }
  fix:       { agent: fix-implementer,  params: { plan: "$.nodes.plan.output" }, approve: high-risk }
  remediate: { agent: infra-remediator, params: { plan: "$.nodes.plan.output" }, approve: write }
  test:      { agent: tester,           params: { fix: "$.nodes.fix.output", remediate: "$.nodes.remediate.output" } }
  review:    { agent: reviewer,         params: { diff: "$.nodes.fix.output.diff" } }
  commit:    { agent: committer,        params: { diff: "$.nodes.fix.output.diff", test: "$.nodes.test.output" },
              approve: write }
  recap:     { agent: postmortem,       params: { rca: "$.nodes.rca.output", fix: "$.nodes.fix.output", remediate: "$.nodes.remediate.output" } }
edges:
  - { from: triage, to: logs }
  - { from: triage, to: trace }
  - { from: triage, to: metrics }
  - { from: triage, to: infra }
  - { from: triage, to: locate }
  - { from: triage, to: know }
  - { from: logs,    to: rca }
  - { from: trace,   to: rca }
  - { from: metrics, to: rca }
  - { from: infra,   to: rca }
  - { from: locate,  to: rca }
  - { from: know,    to: rca }
  - { from: rca, to: plan }
  - { from: plan, to: fix }
  - { from: plan, to: remediate }
  - { from: fix, to: test }
  - { from: remediate, to: test }
  - { from: test, to: review, when: "$.nodes.test.output.passed == true" }   # 条件边
  - { from: review, to: commit }
  - { from: commit, to: recap }
```

> 路径统一为 `$.nodes.<id>.output.<field>` 完整路径，`$.inputs.*` 为全局入参。修复循环不靠 DAG 回环，而靠 `fix-implementer` 节点内部迭代（见下）。

---

## 三、总体架构

```
                    ┌──────────────────────────────────────────┐
                    │          workflow.yaml（手动编写）           │
                    │  nodes + edges(流向) + when(条件) + approve  │
                    └──────────────────┬───────────────────────┘
                                       │ parse/validate (Pydantic)
                    ┌──────────────────▼───────────────────────┐
                    │       编排引擎（DAGExecutor, asyncio）       │
                    │  拓扑排序 → ready 集 → 并发调度 → 条件边/重试  │
                    └──────┬──────────────────────┬────────────┘
                           │ per-node               │ read/write
                ┌──────────▼───────────┐    ┌───────▼─────────┐
                │  AgentRegistry（插件）  │    │  WorkflowContext │
                │  发现/加载职能智能体      │    │  JSONPath 取值/回写│
                └──────────┬───────────┘    └───────┬─────────┘
                           │ agent 名                │ StateStore
                ┌──────────▼─────────────────────────▼─────────┐
                │          opencode 适配层（OpenCodeAdapter）      │
                │  每节点：建 session → 发 prompt → 流式收事件 → 取输出│
                └──────┬───────────────┬───────────────┬────────┘
                       │ 内置工具       │ MCP 工具        │ events
            ┌──────────▼──────┐  ┌─────▼──────────┐  ┌───▼─────────┐
            │ opencode 内置     │  │ 数据源 MCP 层    │  │ 事件总线→    │
            │ edit/grep/bash  │  │ 日志/链路/    │  │ Langfuse    │
            └──────────┬──────┘  │ CMDB           │  │ trace/gen    │
                       │         └────────────────┘  └─────────────┘
            ┌──────────▼──────────────────┐
            │  沙箱 opensandbox（MCP 工具）  │
            │  跑测试/编译/复现，Docker/K8s   │
            └─────────────────────────────┘
```

**核心思想**：opencode 提供「单智能体 = 一个 session」的原子能力（内置代码工具 + 权限 + 事件流）；我们自建 DAG、并行、条件、上下文、观测；代码类 agent 复用 opencode 内置工具，数据类 agent 通过 MCP 接外部数据源，测试执行通过 opensandbox 沙箱隔离。

---

## 四、核心组件设计

### 4.1 Workflow 定义（YAML schema）
- `nodes`（agent、params、approve、retry、timeout、`on_schema_error`、`on_failure`、`idempotency_key`）+ `edges`（流向，支持 `when` 条件边）。~~`context_limit`~~ 已由字段语义分级 + summary/details 取代，**废弃**。
- 每个节点回写结构化 `output`（按领域 schema）+ 原始 `stdout` + `events`，供下游 `$.nodes.<id>.output.xxx` 引用；上下文管理详见 [4.10](#410-上下文管理workflowcontext)。
- Pydantic v2 校验；`python -m agentflow validate workflow.yaml` 静态检查（环/悬空节点/agent 存在性/JSONPath 合法性/**禁止 params 引用 `$.nodes.*.stdout`**，强制走 summary/details 而非原始输出）。

### 4.2 可插拔节点（AgentRegistry）
- 每职能 agent = `agents/<name>/agent.md`（opencode agent 定义）+ `agents/<name>/spec.py`（输入/输出 schema、沙箱需求、工具声明、pre/post 钩子）。
- **`spec.py` 声明 `input_view`**：告诉 executor「我只要上游的哪些字段」，executor 按视图裁剪后再注入 prompt——把字段挑选从 prompt 模板挪到声明里，可静态校验、可监控。
- **`input_view` 三种传递策略**：
  - **Summary（默认）**：只传上游 `summary`（核心结论）——绝大多数情况。
  - **Reference（按需引用）**：只注入引用 + MCP 工具 `read_upstream_output`，agent 需要时主动拉取——大数据量/低概率用到。
  - **Full（全量）**：注入完整 `details`——强依赖/转换型（如 `reviewer` 要完整 diff、`fix-implementer` 要完整 CodeLocation）。
- 约定：诊断链用 Summary/Reference（传假设 + 证据指针，不传日志全文）；修复链的 diff/test 用 Full；全局事实（repo/branch/ticket）放 `$.inputs`，所有节点可见。
- `AgentRegistry` 目录扫描 + entry_points 发现；YAML `agent: triage` 即插拔，新增 agent = 新增目录。

### 4.3 编排引擎（DAGExecutor）
- `parser.py`（YAML→DAG）、`dag.py`（拓扑排序/环检测/ready 集）、`executor.py`（asyncio 并发调度：ready 节点并行、`when` 条件过滤、infra 失败 retry / logic 失败 on_failure、超时 abort、并发配额）。
- `StateStore` 接口：`InMemoryStore`（单测）/ `SqliteStore`（本地）/ `SqlStore`（Postgres，生产）/ `RedisStore`（锁、断点续跑）。**checkpoint 节点级**：持久化节点状态 + output + 重试计数 + 审批等待状态，重启后幂等跳过已 done 节点（见 4.11）。本地也用 SQLite 而非纯内存——否则「流程退出后重新运行」无法接上 checkpoint。
- **并发写语义**：每个节点只写自己的 `nodes.<id>` 命名空间（无跨节点同 key 争用），StateStore 对单 slot 提供原子写（UPSERT）；并行分支互不覆盖。

### 4.4 opencode 适配层（OpenCodeAdapter）
- 接口 `run_node(agent_name, prompt, tools) -> AsyncIterator[Event]`，内部建 session、发 prompt、收 SSE、取输出。
- **默认实现 `server_adapter`（HTTP+SSE 直连，已 spike 验证 ✅）**，用 `httpx` 调 `opencode serve`：
  - `POST /session` 建会话；`POST /session/:id/message`（body `{"parts":[{"type":"text","text":...}]}`）发 prompt；`GET /event` 收 SSE 事件流。
- `sdk_adapter`（官方 Python SDK）仅作可选封装，**非必需**。
- 捕获关键事件：`message.part.updated`（文本/工具调用增量）、`session.status`/`session.idle`（完成信号）、`step-finish`（token/cost）、`permission.asked`（审批门禁）。
- ⚠️ **关键发现（spike 实测）**：同步 `POST /message` 返回里**没有工具调用 part**，要观测工具调用必须消费 `GET /event` 的 SSE 流。**adapter 必须挂 SSE 监听器，不能只看同步返回**。

### 4.5 数据源 MCP 工具层（新增）
- 统一以 **MCP server** 接入数据源；opencode 原生支持 MCP，每个 agent 在 frontmatter 挂载自己的 MCP 工具集。
- 数据源清单（按诊断/解决侧 agent 需求）：

  | 数据源 | MCP 工具 | 消费 agent |
  |---|---|---|
  | 日志平台（ES） | `query_logs` | log-analyst |
  | 链路追踪（gateway MDC traceId → ES 日志关联） | `query_logs(trace_id)` | trace-analyst |
  | 指标平台（Prometheus） | `query_metrics` | metrics-analyst |
  | K8s 对象（只读） | `describe_pod`/`get_events` | infra-locator |
  | K8s 对象（写） | `scale`/`apply`/`exec` | infra-remediator |
  | CMDB | `get_ci` | triage |
  | 服务拓扑（预计算） | `service_topology` | code-locator / root-cause / fix-planner |

- **服务拓扑（ServiceTopology）**：离线聚合 trace（动态调用关系 caller→callee）+ CMDB（service→repo/owner 元数据）+ 代码扫描（`@FeignClient` 声明依赖）→ 预计算成拓扑图，暴露 `get_service`/`get_dependencies`/`get_dependents`/`get_path` 四类查询。支撑两件事：**多仓库定位**（code-locator 由故障 service 查 repo）与**影响面评估**（root-cause/fix-planner 用 `get_dependents` 圈 blast radius）。生命周期：离线预计算 → 诊断/修复时 MCP 查询，详见 `SCENARIOS.md` 第四节。
- 知识库检索与「诊断→修复→沉淀」闭环**暂不实现**（kg-qa/kg-update 属咨询业务，与 AIOps 无关），列为后续待办。

### 4.6 沙箱（opensandbox）
- `SandboxAdapter` 接口 + `opensandbox.py` 实现（同一套 `Sandbox` API，本地 podman / 生产 K8s 由配置切换）。
- **podman 兼容（已 spike 验证 ✅）**：podman 自动在 `/var/run/docker.sock` 转发 Docker 兼容 socket，opensandbox-server 直接当 docker 用，无需改造。需给 podman 配 docker.io 镜像源（国内网络访问不了 docker.io）。
- 以 MCP 工具暴露 `run_python` / `run_shell` / `read_file` / `write_file`（fastmcp 写 stdio MCP server，已 spike 验证 ✅）；tester 与 fix-implementer 的编译/测试在沙箱内执行。
- ⚠️ code-interpreter 镜像必须传 `entrypoint=["/opt/opensandbox/code-interpreter.sh"]`，否则沙箱服务起不来（spike 踩坑）。
- **沙箱生命周期 = per-node**：节点开始创建、节点结束（成功/失败/超时）销毁，节点内多次代码执行复用同一沙箱；**绝不跨节点/跨请求共享单例**。
- **沙箱环境一致性（防上下文漂移）**：fix-implementer 在沙箱里做的环境变更（`pip install`、配置修改）必须在 tester 的沙箱里重现，否则测试假阴性。**首选 snapshot/resume**（opensandbox 支持沙箱状态快照，tester 直接从 fix 的快照恢复，最可靠）；`FixResult.env_changes` 作为兜底（LLM 自报环境变更会漏记 apt 包/系统配置等隐性依赖）。MVP 先用 env_changes，M3 起评估 snapshot 为主。
- **安全基线**（详见 `SECURITY.md`）：① `allowed_host_paths` 必须显式白名单（禁止空值=允许全部）；② ingress 关闭、egress 白名单；③ 防泄漏三层（finally + TTL 兜底 + reaper）。
- **沙箱 egress 放通（bug fix 场景必需）**：fix-implementer/tester 沙箱要访问 GitHub（clone）、数据源 MCP（日志/指标/K8s）、LLM API、镜像源。egress 白名单必须显式包含这些端点（本地测试可用 `--network=host`），否则 agent 在沙箱里 clone/查数据全失败。见 `SCENARIOS.md` 补充 gap 分析。
- **Java 沙箱镜像**：fix-implementer/tester 跑 Java 单测需专用镜像——基础 `eclipse-temurin:21-jdk` + `git` + `ca-certificates`（HTTPS 访问 GitHub），并预灌 `~/.gradle` 缓存（或用 `./gradlew` 自下载 Gradle 8.10 distribution）。镜像文件 `testbed/docker/sandbox-java.Dockerfile`。

### 4.7 审批门禁 + 分级执行（新增）
- 写操作（commit/push、高危文件改动、KG 写入）走 opencode `permission: ask`，审批请求经 `permission.asked` 事件流转到人工确认；审批记录进审计。
- 分级：低危修复自动执行；高危修复（核心模块/安全/db schema）走工单/人工审批再落地。
- **审批驳回路径**：审批必须支持「通过 / 驳回」两态。驳回 → 节点标记 `approval_rejected`，走 `on_failure`（回 fix-planner 重新规划或转人工），不能 panic/卡死；审批超时默认 auto-deny。
- **并发审批**：同一 run 可能多个写操作并行（如场景1 的 fix-implementer 高危改动 + infra-remediator 写操作同时 `ask`）。审批器须支持并发（按 `node_id` 分 slot），串行会阻塞止血操作。

### 4.8 可观测（两层：Langfuse + OTel）

观测分两层，职责不重叠，通过 `run_id`/`node_id` 关联：

| 层 | 工具 | 回答什么 | 粒度 |
|---|---|---|---|
| **LLM/Agent 层** | Langfuse（自托管） | agent 做了什么、和模型交互了什么、token/cost、工具调用、推理过程 | 业务级（run→node→generation→tool） |
| **基础设施层** | OpenTelemetry | 服务健康、延迟、错误率、资源、沙箱数/泄漏、队列、跨进程链路 | 系统级（metrics/traces/logs） |

**数据来源（单一事件源）**：`event_bus.py` 消费 opencode `GET /event` 的 SSE 流（已 spike 验证 ✅：`step-finish` 自带 token/cost、工具调用在 `message.part.updated`）+ executor 编排事件 + 沙箱事件，统一打上 `run_id`/`node_id`，再**扇出**到两个 sink：

```
event_bus（单一事件源，统一 run_id/node_id）
  ├── LlmTraceSink → Langfuse：run=trace、node=generation、工具/沙箱=span
  └── MetricsSink → OTel：metrics + 跨进程分布式 traces
```

**关联机制**：`run_id` + `node_id` 同时写进 Langfuse trace attribute 和 OTel span attribute；OTel `trace_id` 由 executor 生成并作为 attribute 写进 Langfuse——从 Grafana 的慢 span 能带着 `node_id` 跳回 Langfuse 看该节点 LLM 推理详情，反之亦然。

**OTel 侧关键 metrics**：

| 类别 | 指标 |
|---|---|
| 平台健康 | 服务存活、HTTP 错误率、请求延迟 |
| 成本 | token 速率（input/output）、cost 速率、单 run 累计 cost |
| 沙箱 | 活跃沙箱数、沙箱生命周期、**泄漏计数**（孤儿容器数）、创建耗时 |
| 队列 | 待运行节点数、并发占用、排队时长 |
| 节点 | 节点时长、成功率、重试次数 |

**OTel 接入优势**：opencode（`opencode-plugin-otel`）与 opensandbox-server（内置 OTel metrics，spike 启动日志可见 `OpenTelemetry metrics export disabled`）**都原生支持 OTel**——只需开启 + 接 collector + 补 executor 自身的 span，一半已就绪。

**为何不只用 OTel 替代 Langfuse**：OTel 的 `gen_ai.*` 语义仍在演进，且缺 LLM 专属生产力（prompt 版本、评测、人工标注、会话回放）——这正是「agent 到底干没干对」调试场景最需要的，留给 Langfuse。

### 4.9 部署
- **本地开发**：`python -m agentflow run workflow.yaml`，单进程 + SQLite StateStore（跨退出持久化，支持断点续跑）+ opensandbox（podman）+ 本地 Langfuse（可选 docker compose），可直接 pdb 调试。
- **生产**：Dockerfile 容器化，K8s Deployment 无状态横向扩容，状态走 Postgres/Redis，opensandbox 切 K8s 运行时，Langfuse+Postgres 集群内部署。
- 本地/生产共用一套代码，差异只收敛在 `config.py`。

### 4.10 上下文管理（WorkflowContext）

> 前提：每个节点 = 一个独立 opencode session，**agent 之间天然看不到彼此**。所以「上下文管理 + 节点间传上下文」是编排层的骨架职责——不传，DAG 里的 agent 都是瞎子。机制已定（`WorkflowContext` + JSONPath + output schema），本节补齐四个细节。

#### 上下文结构（命名空间，天然解决并行冲突）

一次 workflow 运行对应一份 JSON，按 `node_id` 命名空间分区：

```json
{
  "meta":   { "run_id": "run_123", "workflow": "bug-fix-pipeline" },
  "inputs": { "repo": "...", "bug_report": { "title": "...", "severity": "P1" } },
  "nodes": {
    "logs":  { "status": "done", "output": { "error_type": "NullPointer", "..." }, "stdout": "..." },
    "trace": { "status": "done", "output": { "failing_span": "..." } },
    "rca":   { "status": "done", "output": { "hypotheses": [...], "confidence": 0.9 } }
  }
}
```

JSONPath 取值约定：

| 引用 | 含义 | 作用域 |
|---|---|---|
| `$.inputs.repo` | workflow 级入参 | 全局 |
| `$.meta.run_id` | 运行元数据 | 全局 |
| `$.nodes.logs.output.xxx` | 上游节点结构化输出 | 边级 |
| `$.nodes.logs.stdout` | 上游原始输出（仅溯源，不进 prompt） | 边级 |

#### 4.10.1 并行分支汇合

- **各分支写各自的命名空间**（`nodes.<id>`），互不覆盖，**不存在「合并冲突」**。
- 下游节点需要多个分支时，在 `params` 里声明多个引用，executor 逐个解析；DAG 拓扑保证「所有上游完成」后节点才 ready（`rca` 要等 `logs/trace/locate` 三支全 done 才跑）。
- 「汇合」= 下游节点的 params 同时引用多个 `$.nodes.<id>.output`，由 prompt 模板拼装，**不是**把四个 output 合并成一个大对象。
- ⚠️ **汇合节点有独立的 token 预算问题**：`rca` 同时承载 3 份证据，全量拼装易破模型 context window。采用 **two_phase 模式**：phase1 只给 `rca` 三份 `summary`，让它决定 `deep_dive` 哪个分支；phase2 用 MCP 工具按需拉取该分支 `details`。**这同时在「静态 DAG」内获得了「动态证据」能力**——下游想看更多细节，无需打破 DAG。
- **two_phase 是单个 session，不是两个**：phase1 = agent 收到四份 summary 并推理「该深挖哪个」；phase2 = agent 在**同一个 session 内**调用 `read_upstream_output` 工具拉 details（一次普通的工具调用，不是新开 session）。session 生命周期与普通节点一致（done 后 DELETE）。

#### 4.10.2 上下文大小 / token 预算（summary + details 双分层）

⚠️ **AIOps 场景下「结构化 output 天然很小」不成立**——`LogEvidence.error_stack`、`TraceEvidence.span_tree`、`FixResult.diff` 单字段可达 10–50KB。因此用「双分层 + 字段分级 + 按需深挖」替代全局字符截断：

1. **每个 agent 的 output 强制双分层**：`summary`（≤500 token 核心结论）+ `details`（完整证据，只落 StateStore）。下游默认只拿 `summary`，需要细节时通过 MCP 工具 `read_upstream_output(node_id, field)` 按需拉取。
2. **字段语义分级**（替代全局 `context_limit`）：`critical` 字段（root_cause、key_stack_frame）永不截断；`supporting` 字段（context_lines）可截断；`raw` 字段（stdout、完整 diff）默认不进 prompt。分级在 schema 里声明，确定性、可校验。
3. **用 token 计数而非字符数**：中文/堆栈 token 密度差异大，字符估算误差可达 30%。
4. **截断决策权下放给上游**：schema 强制 `must_pass`（必传核心结论）+ `optional`（可省略），下游按节点级 token 预算组装，而非 executor 暴力截断。
5. **`read_upstream_output` 的 run_id 由 executor 绑定，不由 LLM 传**：工具创建时（per-node）由 executor 把当前 `run_id`/`node_id` 捕获进工具闭包/环境变量，LLM 只传 `(node_id, field)`。**不依赖 LLM 把 run_id 带进工具调用**（不可靠）。

#### 4.10.3 全局上下文 vs 边级上下文

- **全局（workflow 级）**：`$.inputs.*` + `$.meta.*`，所有节点无需声明即可引用，**不产生边**。放「关于本次运行的事实」——repo 路径、原始 bug 报告、运行 ID、配置。
- **边级（节点间）**：`$.nodes.<upstream>.output.*`，必须在 `params` 里显式声明，**声明即产生 DAG 依赖边**。放「推导出来的数据」——证据、方案、结果。
- 约定：全局放「事实」，边级放「派生物」，两者不混用。
- **全局 inputs 也走字段分级**：`$.inputs.*` 会被注入所有节点的 prompt，若含大字段（bug 报告原始堆栈/截图描述）会浪费 token 并挤压末端节点 context。约定：`inputs` 里 `critical` 字段（repo/branch/ticket_id）全量注入，大字段（原始 bug 全文）走 reference（按需拉取）——**不默认「全量注入所有 inputs」**。

#### 4.10.4 schema 不匹配 / 边界行为

节点完成后，executor 用其 `output_schema` 校验输出。YAML 里可配 `on_schema_error`：

| 取值 | 行为 |
|---|---|
| `fail`（默认） | 节点标记失败，走该节点的 `on_failure`（abort/continue） |
| `retry` | 把校验错误反馈给 agent，重新生成，最多 `retry.max` 次 |
| `coerce` | 尝试清洗/兜底（仅少数宽松 schema 用） |

若节点未声明 `output_schema`，则视为「非结构化输出」：`output` 置空、全文进 `stdout`，下游仍可读 `$.nodes.<id>.stdout`（降级路径）。

#### 4.10.5 失败节点的 fallback 结构

`on_failure: continue` 时，下游仍会读失败节点的 `output`。**必须**把失败节点的 output 定义为固定结构：

```json
{ "status": "failed", "error_reason": "...", "output": null }
```

下游 prompt 模板据此显式声明「上游 X 不可用，结论置信度受限」，避免把「空证据」误判成「无异常」。executor 保证失败节点的 `$.nodes.<id>.output` 永远是上述结构（而非 null/空对象）。
- **executor 硬门禁（不靠 LLM 自律）**：当 `critical` 上游失败时（如 rca 缺 logs/trace），executor 强制转人工介入，而非让 agent 在 null 证据上继续推理——LLM 面对缺失证据会幻觉或过度自信。规则：`critical` 证据缺失 → 节点标记 `degraded` → 走审批门禁，人工确认后才继续。

#### 4.10.6 证据时效（AIOps 特有）

运维证据有时效：日志可能轮转、trace 采样有 TTL、审批等待小时级后证据可能已失效。

- 证据类 schema 强制带 `collected_at` + `ttl_seconds`。
- 节点 ready 检查时，executor 校验上游证据是否过期：过期标记为 `stale`。**处置路径**：默认 `warn`（带 stale 声明继续），`critical` 证据过期则 `abort`（转人工或触发上游重查）。
- 审批等待超长时告警「证据可能失效」。
- （进阶，M5+）过期触发上游重跑——需轻量动态，暂不做。

### 4.11 运行生命周期（失败/断点/文件/异步/资源/幂等/入口）

> 从「真实 agent 使用」视角补齐的运行时语义。**P0** 项已给首期决策（M2 实现地基），**P1** 给建议值，唯一待 spike 的是 session resume。

#### 4.11.1 失败分类（P0）

两种性质不同的失败，处理策略完全不同：

| 类别 | 例子 | 处理 |
|---|---|---|
| **infra 失败**（可重试） | LLM 超时、网络抖动、沙箱起不来 | 走 `retry`（指数退避 + jitter） |
| **logic 失败**（重试无用） | 测试没过、schema 校验不过、置信度太低 | 走 `on_failure`（abort / continue） |

executor 依据 opencode 事件里的 error 类型 + 节点状态 + schema 校验结果分类。**`retry` 只对 infra 失败生效，logic 失败直接传播。**

#### 4.11.2 断点续跑（P0）

- **checkpoint 粒度 = 节点级**：一个节点跑完（成功或失败）即落盘一次。
- StateStore 持久化：节点状态（pending/running/done/failed）+ 结构化 output + 重试计数 + **session_id**（用于 resume）。
- **恢复语义**：重启后已 `done` 的节点**幂等跳过**（复用缓存 output），只重跑 `failed`/`pending` 节点。
- **session resume 可行（已 spike 验证 ✅）**：opencode 把 session 持久化在 SQLite（`~/.local/share/opencode/opencode.db`），server 崩溃重启后 session 及对话上下文仍在（spike 4 实测 SIGKILL 后重启，同一 session 仍能记住之前的口令）。因此**中断的节点直接 resume 原 session 继续**，而非重开。
- **session 生命周期**：节点 `done` 后主动 `DELETE /session/:id`（除非该节点配置了允许 resume），避免 session 上下文长期累积、DB 膨胀；仅在「中断 resume」场景保留 session。
- **resume 追加 input（用户中途补充）**：用户跑一半想起要补信息 → `pause <run_id>` 优雅停止（当前节点 abort、checkpoint 落盘）→ 补 input → `resume <run_id> --input '{...}'` 继续。新增 input 合并进 `$.inputs`，**只作用于重跑的节点（failed/pending），已 done 节点不受影响**——符合「继续刚才的流程」的直觉。
- **作废重跑（补的 input 影响已跑节点）**：`resume <run_id> --invalidate-from <node_id>`，把该节点及其下游全部作废重跑，其余 done 保留（MVP 后做）。
- **CLI 三入口**：`run`（全新，新 run_id，从头跑）/ `pause <run_id>`（优雅停止）/ `resume <run_id> [--input ...]`（续跑，跳过 done）。

#### 4.11.3 输出与文件（P0）

- **大文本**：见 4.10.2（截断 + StateStore 指针）。
- **文件/二进制 artifact**：**不落 StateStore（Postgres）**，落 **run 级工作区** `workdir/<run_id>/`（本地开发）或对象存储（生产 MinIO/S3）。结构化 output 只存**相对路径 / URI 引用**。
- **共享工作区 + per-attempt 隔离**：同一 run 共享 `workdir/<run_id>/`，但每个节点每次尝试用子目录 `workdir/<run_id>/<node_id>/<attempt>/`，避免 retry 时旧 diff/临时文件/编译产物污染新一次执行。
- **repo 来源**：`$.inputs.repo` 是远程 Git URL；executor 在 run 启动时 clone 到 `workdir/<run_id>/repo/`（每个 run 一个隔离副本），agent 的 code 工具只在这个副本上操作，不碰真实仓库。clone 失败按 infra 失败处理。

#### 4.11.4 异步审批 + 超时（P0）

- **审批状态持久化**：`permission.asked` 等人审批时，把「等待审批」状态写入 StateStore，**重启后能继续等**。
- **审批超时默认 auto-deny**（安全优先），超时未响应即拒绝。
- **节点超时语义**：超时 → `POST /session/:id/abort` 中止 → 取已生成部分 → 标记 `failed(timeout)`；长任务用**心跳**区分「在跑」和「卡死」。

#### 4.11.5 资源与成本（P1）

- **并发上限**：executor 内置并发配额（默认 4 并行），受沙箱容器数 + LLM 限流约束，超出排队。
- **成本预算**：run 级 token/cost 上限，累加 `step-finish` 的 cost，超限自动停 + 告警（数据源现成）。

#### 4.11.6 幂等与副作用（P1）

- 有副作用的节点（`committer` push、`postmortem` 写 KG）声明 `idempotency_key` + 执行前检查是否已提交，支持 dry-run。
- 与断点续跑绑定：否则「断点重跑」会把 push 再推一遍。

#### 4.11.7 入口与凭据（P2）

- **触发方式（首期）**：CLI `python -m agentflow run workflow.yaml --trigger bug.json`；后续挂 REST API / 事件消费。
- **暂停/续跑/重跑入口**：`pause <run_id>` / `resume <run_id> [--input ...]` / `run`，语义见 [4.11.2](#4112-断点续跑p0)。
- **凭据注入**：secret 走环境变量 / secret manager，由 adapter 注入 MCP 工具，**不进 YAML**，agent 不直接拿 secret。

### 4.12 依赖边界（防腐层）

把「外部依赖」隔离在接口后面，核心代码只依赖接口、不依赖库。**接口按我们的需求定义，不按库的 API 定义**——如 `AgentRuntime.run_node(agent, prompt, ctx) -> events`，`events` 是我们自己的事件类型，不是 opencode 的原始事件。

| 接口 | 职责 | 当前实现 | 可替换为 |
|---|---|---|---|
| `AgentRuntime` | `run_node(agent, prompt, ctx) -> AsyncIterator[NodeEvent]` | `OpenCodeAdapter`（HTTP+SSE） | 自研 / AgentScope ReActAgent |
| `Sandbox` | `run_code` / `run_shell` / 文件读写 | `OpenSandboxAdapter` | E2B / Docker / Daytona |
| `StateStore` | checkpoint / 断点续跑 / 审批等待 | InMemory / Postgres | Redis / 其他 |
| `LlmTraceSink` | LLM 级 trace（prompt/token/cost/工具调用） | Langfuse | 自研 / 其他 |
| `MetricsSink` | 系统级 metrics + 分布式 trace | OpenTelemetry | 自研 |
| `ArtifactStore` | 大文件/二进制落盘与读取 | 本地 FS | S3 / MinIO |

**不抽象**（YAGNI，避免过度设计）：
- **编排引擎 DAGExecutor**——产品核心，不是外部依赖；想换 LangGraph 就直接用，不会去包接口。
- **LLM provider**——opencode 已 provider-agnostic，我们这层无需再包。

**接口契约约定**：每个接口用 Python `Protocol`/`ABC` 定义，配「输入输出类型 + 错误语义 + 生命周期（谁创建谁销毁）」注释。换实现只改 adapter，核心零改动。

**边界能力**：防腐层能「换单个外部依赖」（换沙箱/换观测干净）；**换不了整套框架**（AgentScope 这种全栈方案不会干净落进某个边界，只能整体替换）。

---

## 五、项目结构

```
my-agent-cc/
├── pyproject.toml
├── docker/Dockerfile
├── deploy/k8s/
├── agentflow/
│   ├── cli.py                  # python -m agentflow validate|run|list
│   ├── config.py               # opencode/sandbox/langfuse/state 配置
│   ├── workflow/               # schema.py / parser.py / dag.py
│   ├── engine/                 # executor.py / context.py / state.py / artifact.py
│   ├── agents/                 # registry.py / base.py
│   ├── opencode/               # adapter.py / server_adapter.py(主,HTTP+SSE) / sdk_adapter.py(可选)
│   ├── sandbox/                # adapter.py / opensandbox.py / mcp_server.py
│   ├── tools/                  # MCP 工具注册（日志/链路/KG/CMDB 的 MCP server 装配）
│   └── observability/          # event_bus.py / langfuse.py(LlmTraceSink) / otel.py(MetricsSink)
├── agents/                     # 可插拔职能智能体（示例）
│   ├── triage/{agent.md,spec.py}
│   ├── log-analyst/{agent.md,spec.py}
│   ├── trace-analyst/{agent.md,spec.py}
│   ├── metrics-analyst/{agent.md,spec.py}
│   ├── infra-locator/{agent.md,spec.py}
│   ├── code-locator/{agent.md,spec.py}
│   ├── knowledge-lookup/{agent.md,spec.py}
│   ├── root-cause/{agent.md,spec.py}
│   ├── fix-planner/{agent.md,spec.py}
│   ├── fix-implementer/{agent.md,spec.py}
│   ├── infra-remediator/{agent.md,spec.py}
│   ├── tester/{agent.md,spec.py}
│   ├── reviewer/{agent.md,spec.py}
│   ├── committer/{agent.md,spec.py}
│   └── postmortem/{agent.md,spec.py}
├── examples/                   # 场景 workflow（bug-fix-pipeline.yaml / order-service-*.yaml）
├── testbed/                    # 测试场景搭建（manifests/fault-inject/mock-datasource/fixtures/assertions，见 SCENARIOS.md）
├── workdir/                    # run 级工作区（artifact 落盘，gitignore）
└── tests/
```

---

## 六、领域中间产物 schema（bug fix 专属，边界校验）

所有 output schema 强制遵循统一约定：
- **`schema_version` 字段**：StateStore 读取时做版本兼容。
- **`summary` + `details` 双分层**：`summary`（≤500 token）默认传下游，`details`（完整证据）只落 StateStore、按需拉取。
- **证据类 schema 带 `collected_at` + `ttl_seconds`**（时效性）；失败节点带 `status: failed` + `error_reason`。

领域 schema：`BugReport`（含 **symptom_type: crash/hang/slow/wrong_output** 症状分类）/ `LogEvidence` / `TraceEvidence` / `MetricsEvidence`（资源时序）/ `InfraEvidence`（K8s 状态）/ `CodeLocation` / `RootCause`（假设+置信度+证据链+**ruled_out 已排除假设**）/ `FixPlan`（改动点+风险等级）/ `FixResult`（diff + **env_changes 环境变更**）/ `RemediationResult`（基础设施改动+回滚方案）/ `TestResult`（通过率+回归）/ `ReviewResult` / `CommitResult` / `Postmortem`；另有预计算数据 `ServiceTopology`（服务拓扑，见 4.5）。证据均带来源追溯（指标/日志行/文档名/幻灯片编号/文件行号）。证据类 schema 额外带 **`query_status: ok/empty/error`**：root-cause 只把 `ok` 的「正常」当负证据，`empty/error` 标为「证据不足」，避免把「查不到」误判成「正常」。

---

## 七、实现里程碑（每阶段可验证）

1. **M0 定义层**：脚手架 + Pydantic schema（含 `schema_version`、`summary`+`details` 约定、失败 fallback 结构）+ parser/dag + `validate` 命令。
2. **M1 opencode 单节点**：把 spike 已验证的 HTTP+SSE 链路封装成 `server_adapter`（建 session→prompt→事件→输出 已跑通），补上 SSE 工具调用捕获；代码定位用内置 grep/glob 验证。
3. **M2 编排执行器**：串行/并行/条件边/重试 + `WorkflowContext`（JSONPath + summary/details + two-phase 汇合 + 字段分级 + 失败 fallback）+ `input_view` 裁剪 + `InMemoryStore` + per-attempt 工作区。
4. **M3 数据源 MCP + 沙箱**：日志/链路 MCP server（出站脱敏）、KG 检索（复用 kg-qa）、opensandbox 适配器 + 沙箱跑测试 + 证据时效 `collected_at`/`ttl`。
5. **M4 审批 + 观测**：写操作审批门禁、`event_bus` 扇出到 Langfuse（`LlmTraceSink`：trace/generation、`step-finish` token/cost 映射）+ OTel（`MetricsSink`：关键 metrics + 分布式 trace）。
6. **M5 部署**：Dockerfile、K8s 清单、`config.py` 本地/生产双态。

---

## 八、关键风险与缓解

| 风险 | 缓解 |
|---|---|
| ~~opencode Python SDK 未 GA~~ | ✅ **已解除**：HTTP+SSE 直连已 spike 验证，不需要 SDK |
| 工具调用观测缺失 | ✅ **已发现并规避**：同步返回无工具调用，adapter 必须挂 SSE 监听消费 `GET /event` |
| 模型跳过工具直接作答 | agent system prompt 强制「涉及代码执行必须用沙箱工具」+ 任务设计需真实执行 |
| opencode 无 DAG，仅树形 subagent | 自建 DAG；节点=独立 session，上下文显式传递 |
| 日志/链路数据源接入 | 统一 MCP 工具层；日志/链路按各自平台 API 封装 |
| opensandbox 需自建 server/镜像 | 本地 podman（docker 兼容 socket，需配镜像源）；生产 K8s；镜像需预拉 + 指定 entrypoint |
| 高危修复的误操作风险 | 写操作审批门禁 + 分级执行 + dry-run（沙箱） + 变更审计 |
| 诊断证据不可追溯 | 证据快照落库 + schema 强制来源字段 |
| 静态 DAG 无法「动态重跑上游拿新数据」 | MVP 接受；two-phase deep-dive（MCP 按需拉取）覆盖「看更多细节」类需求；真正的动态重跑列为后续迭代 |

---

## 九、验证方式（端到端）

1. **静态校验**：`python -m agentflow validate examples/bug-fix-pipeline.yaml` → 输出 DAG、检测环/悬空节点。
2. **本地全链路**：给一个真实 bug（配日志+链路数据），`python -m agentflow run examples/bug-fix-pipeline.yaml`，观察 triage→(并行 logs/trace/locate/know)→rca→plan→fix→test→review→commit→recap 依次执行、条件边生效、上下文正确传递。
3. **沙箱核对**：确认 tester/fix-implementer 的编译测试发生在 opensandbox 容器内（非本地 shell）。
4. **审批核对**：committer 的 git 写操作触发审批门禁，人工确认后才落地。
5. **监控核对**：Langfuse UI 能看到本次 run 的 trace，逐节点查看 prompt/completion/token/cost/工具调用/审批状态。
6. **生产核对**：`docker build` 部署 K8s，切换 Postgres/Redis + K8s 沙箱运行时，重复跑同一 workflow 结果一致。

---

## 附录：设计评审记录

> 记录评审中发现的问题、采纳/缓做的决策及原因，供后续接手的人理解「为什么这么设计」。

### A. 框架选型结论

- **不引入 AgentScope 2.0 / LangGraph**：AgentScope 的「agent 运行时（ReActAgent）」与我们的 opencode（编码专用）是**竞争关系**，引入它等于换掉 opencode、丢掉 bug fix 的核心优势；LangGraph 是编排引擎，与自研 DAGExecutor 定位重叠且我们更简单。自研 harness + opencode 是正确组合。
- **从 AgentScope 借思想（不引框架）**：多租户隔离（唯一明显 gap，SaaS 化时补）、可恢复中断、上下文自动压缩、沙箱预热池。

### B. 第一轮评审（AI 运维专家）—— 采纳清单

| 问题 | 采纳结果 |
|---|---|
| output 大小假设不成立 + 截断粗暴 | ✅ summary+details 双分层 + 字段分级 + token 计数 |
| 汇合节点 token 爆炸 | ✅ two-phase 深挖 |
| 失败节点 fallback 未定义 | ✅ `{status:failed, error_reason}` 固定结构 |
| 敏感数据脱敏 | ✅ MCP 出站脱敏 + StateStore 边界 middleware |
| schema 版本 | ✅ `schema_version` |
| 证据时效 | ✅ `collected_at` + `ttl_seconds` |
| 工作区并发污染 | ✅ per-attempt 目录 |
| session 生命周期 | ✅ done 后 DELETE session |
| 字段内筛选 | ✅ `input_view` 字段级裁剪 |
| 证据索引中间件（倒排索引） | ❌ 不做（过度设计） |
| YAML transform DSL | ❌ 不做（有变 DSL 风险） |
| Layer4 相关性评分 agent | ⏸ 进阶可选 |
| 动态重跑上游 / 跨 run 指纹 | ⏸ M5+ 后续 |

### C. 第二轮评审（运维专家）—— 采纳清单

| 问题 | 采纳结果 |
|---|---|
| 沙箱环境漂移（fix 装依赖、tester 丢环境） | ✅ `FixResult` 加 `env_changes`，tester 重放 |
| 三种传递策略（Summary/Reference/Full） | ✅ 形式化进 `input_view` |
| 引用式上下文 | ⚠️ 已覆盖（`read_upstream_output` MCP 工具） |
| 循环依赖/滑动窗口 | ⚠️ 不成立（静态 DAG + 节点级 retry，不累积） |

### D. 遗留待办（明确标注为后续）

1. **跨 run 指纹**（复发 bug 对比）—— M5+。
2. **动态重跑上游**（证据过期触发重查）—— M5+，需轻量动态。
3. **多租户隔离**—— SaaS 化时补。
4. **DeepSeek 接入**—— 需 `DEEPSEEK_API_KEY`（spike 用的是 opencode 内置 Zen 模型）。
5. **知识库检索 agent（knowledge-lookup）与「诊断→修复→沉淀」闭环**—— 暂不实现（kg-qa/kg-update 属咨询业务，与 AIOps 无关）；如将来需要，接独立的运维知识库。
