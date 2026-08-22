# AIOps Bug Fix 智能体工作流 — 设计 v2（实现阶段新增/增强）

> 本文档是 [DESIGN.md](DESIGN.md) 的 **v2 补充**：记录实现阶段新增或增强、但 v1 设计未覆盖（或只简略提及）的能力。每个条目标注对应 v1 章节，供对照。

---

## 1. 审批节点（`kind: approval`）—— 对应 DESIGN.md §4.7

**v1 设计**：审批是「节点属性」（`approve` 字段），节点跑完顺手审一下，不是流程图的一部分。

**v2 增强**：审批可做成**显式流程节点**，插在任意两个节点之间。

- `NodeDef` 新增 `kind: Literal["agent", "approval"] = "agent"`。
- `kind=approval` 的节点不调 LLM，走审批分支（`executor._execute_approval_node`）。
- `params` 声明「展示/透传哪些直接上游」的 JSONPath 引用（单上游透传单个 output，多上游透传整个 dict）。
- 通过 → 透传上游 output（下游引用 `$.nodes.<approve>.output` 拿到原样内容）；驳回 → 节点标 `rejected-canceled` + `on_failure: abort` 停下游；run 状态归 `failed`。

**用法**（示例见 `examples/bug-fix-with-approval.yaml`）：

```yaml
  fix:         { agent: fix-implementer, params: { plan: "$.nodes.plan.output" } }
  approve-fix: { agent: approval, kind: approval,
                 params: { output: "$.nodes.fix.output" }, approve: high-risk }
  test:        { agent: tester, params: { fix: "$.nodes.approve-fix.output" } }
```

**配套 API**：`POST /runs/:id/approve` / `/reject`（body `node_id`）；`GET /runs/:id` 返回 `pending_approvals`（含 `{node_id, trigger, upstream}`）；前端展示审批按钮 + 直接上游 output。

**实现位置**：`agentflow/workflow/schema.py`、`agentflow/engine/executor.py`、`agentflow/server.py`。

---

## 2. ConsoleSink（CLI 实时进度）—— 对应 DESIGN.md §4.8

**v1 设计**：观测分两层 Langfuse（LLM 层）+ OTel（基础设施层），没有本地实时调试手段。

**v2 增强**：新增第三个 sink `ConsoleSink`，CLI `run` 时实时打印每节点的 `▶ 开始` → `输入 prompt` + `输出 output` → `✅/❌ 状态(tokens/cost/耗时)`。面向本地调试，与 Langfuse/OTel 的「机器观测」互补。

- 只在 `cli.py` 订阅（`server.py` 不订阅，避免污染服务日志）。
- 打印的 prompt/output 来自 `GENERATION` 事件（executor 发布）。

**实现位置**：`agentflow/observability/console.py`、`agentflow/cli.py`。

---

## 3. json5 宽松解析 —— 对应 DESIGN.md §4.10.4

**v1 设计**：output 提取用标准 JSON 解析（`json.loads`）。

**v2 增强**：`executor._parse_json` 在标准 JSON 解析失败后，用 **json5** 兜底，容忍 LLM 输出的常见瑕疵——尾逗号、注释、单引号 key 等，降低「模型输出不是合法 JSON」导致的节点失败。

- 配合 `on_schema_error: coerce`（复盘类节点用）进一步降级。
- json5 是 fastmcp 的传递依赖，`try import` 惰性加载，不可用时跳过。

**实现位置**：`agentflow/engine/executor.py`（`_parse_json`）。

---

## 4. permission.asked 自动 reply —— 对应 DESIGN.md §4.4

**v1 设计**：只提到 adapter 捕获 `permission.asked` 事件，未说明如何响应。

**v2 增强**：`server_adapter` 收到 opencode 的工具级权限请求（如 `external_directory`，fix/tester 访问 `/tmp` 时触发）时，自动 `POST /permission/:id/reply` 回复 `once`（批准单次），避免 session 卡死在审批等待超时。

- 根因：不响应 `permission.asked` 会导致 opencode 一直等待，写操作 agent 卡满 httpx 300s 超时。
- 这与 agentflow 自己的 `ApprovalManager`（业务级审批）是两层：opencode 权限是工具级安全，agentflow 审批是「高危写操作是否继续」的业务决策。

**实现位置**：`agentflow/opencode/server_adapter.py`（`reply_permission` + `PERMISSION_ASKED` 处理）。

---

## 5. 成本预算 —— 对应 DESIGN.md §4.11.5

**v1 设计**：成本预算是 P1（「run 级 token/cost 上限，超限自动停」），未展开。

**v2 增强**：实现 run 级成本预算：

- 环境变量 `AGENTFLOW_MAX_COST` / `AGENTFLOW_MAX_TOKENS`（默认 `0` = 不限）。
- `DAGExecutor` 累计每节点 `cost`/`tokens`，超限触发 `abort`（后续节点 cancelled）。
- `cli.py` / `server.py` 均传入 `settings.max_cost` / `max_tokens`。

**实现位置**：`agentflow/config.py`、`agentflow/engine/executor.py`。

---

## 6. input_view 的 None 字段裁剪 —— 对应 DESIGN.md §4.10.2

**v1 设计**：`input_view` 三种策略 Summary（只传 summary）/ Reference（按需拉）/ Full（全量）。

**v2 增强**：Summary 视图的裁剪比 v1 更细——不仅去掉 `details` 字段，还去掉**值为 `None` 的顶层字段**。

- 根因：schema 校验后 evidence 类有很多顶层 `None` 字段（如 `error_stack: null`），`json.dumps` 会输出 `"error_stack": null`，导致 prompt 仍含大量空字段。去掉 None 字段后，下游只拿到有实际内容的 summary。
- 当前实现 summary/full 两态（`NodeDef.input_view` + agent 的 `spec.py` 声明，节点级可覆盖）；Reference 态未实现。

**实现位置**：`agentflow/engine/executor.py`（`_crop_summary`）、`agentflow/workflow/schema.py`、`agentflow/agents/base.py`。

---

## 附：与 v1 的关系

- 本文档 6 项均为「实现超出 v1 设计」的增强，已在生产代码中落地并有测试覆盖。
- v1 中「设计合理但未实现」的差距（字段语义分级、token 计数、two-phase 引导、证据时效 stale、节点 timeout、repo 隔离、审批持久化、idempotency 等）**不在本文档范围**，仍是待办，见 CLAUDE.md 的 TODO。
