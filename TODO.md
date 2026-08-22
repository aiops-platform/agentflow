# 实现待办清单（按优先级排序）

> 基于 DESIGN.md 与当前实现的差距评估，按「对整体实现场景（bug 定位 → 修复端到端）的影响 + 功能重要性」排序。
> - **P0**：影响核心场景的「能不能跑通 / 结果对不对」。
> - **P1**：影响「跑得好不好 / 准不准 / 省不省」。
> - **P2**：生产化 / 观测 / 进阶能力。
> 每一项标注对应 DESIGN.md 章节与实现位置。

---

## P0 —— 核心场景正确性 / 可用性

### 1. 节点 timeout + 超时错误透传（§4.11.4）
- **影响**：`NodeDef.timeout` 字段已定义但 executor 完全没用；节点卡住（如 DeepSeek 偶发卡死）要干等 httpx 300s 才失败，且报错是空的 `agent runtime error`。
- **现状**：locate 节点实测卡 5 分钟。
- **实现**：executor 用 `node.timeout`（超时 → abort session → 标 `failed(timeout)`）；超时错误透传出来替代空错误。配合 `retry` 可自动重试一次。
- **位置**：`agentflow/engine/executor.py`、`agentflow/opencode/server_adapter.py`。

### 2. knowledge-lookup 工具（know 节点空转）（§4.5）
- **影响**：knowledge-lookup 没有 kg 检索工具，agent 空转 120s 超时，拖慢每个 run（除非从 workflow 去掉 know 节点）。
- **实现**：给 knowledge-lookup 加一个 mock/空的知识库工具（返回空 `similar_cases`），或接真实 kg-qa；至少让它快速产出合法 `KnowledgeEvidence`。
- **位置**：`agents/knowledge-lookup/`、`agentflow/tools/`。

### 3. repo 隔离副本（run 启动时 clone）（§4.11.3）
- **影响**：设计里 executor 把 `$.inputs.repo` clone 到 `workdir/<run_id>/repo/`，agent 只碰副本；当前没实现，agent 的 code 工具（edit/bash）直接在宿主机操作，违反安全隔离，且 fix/tester 的代码修复场景依赖隔离副本。
- **实现**：run 启动时 clone repo 到 run 工作区，agent 的 code 工具限定在该副本。
- **位置**：`agentflow/engine/executor.py`、`agentflow/engine/artifact.py`。

### 4. 失败分类 + 指数退避 jitter（§4.11.1）
- **影响**：infra 失败（LLM 超时、网络抖动）应走 `retry`（指数退避 + jitter）；当前 retry 只是固定 max 次，无退避，偶发失败无法有效自愈。
- **实现**：retry 加指数退避 + jitter（`retry.max` + `backoff_seconds` 字段已在 schema，补 executor 逻辑）。
- **位置**：`agentflow/engine/executor.py`、`agentflow/workflow/schema.py`。

### 5. 审批状态持久化（§4.11.4）
- **影响**：manual 审批模式下，「等待审批」状态只在内存（`ApprovalManager._requests`），后端重启后审批丢失、节点永久卡住。
- **实现**：审批等待状态写入 StateStore，重启后恢复可继续等（配合 `pending()` 查询）。
- **位置**：`agentflow/engine/approval.py`、`agentflow/engine/state.py`。

### 6. executor 硬门禁（critical 证据缺失 → 人工）（§4.10.5）
- **影响**：`critical` 上游失败时（如 rca 缺 logs/trace），当前下游在 null 证据上继续推理，LLM 会幻觉或过度自信。
- **实现**：`critical` 证据缺失 → 节点标 `degraded` → 走审批门禁，人工确认后才继续。
- **位置**：`agentflow/engine/executor.py`、`agentflow/domain/schemas.py`。

---

## P1 —— 健壮性 / 成本 / 准确性

### 7. 证据时效 stale 检查（§4.10.6）
- **影响**：AIOps 证据有时效（日志轮转、trace 采样 TTL），当前 schema 有 `collected_at`/`ttl_seconds` 字段，但 executor 无 stale 检查。
- **实现**：节点 ready 时校验上游证据是否过期，过期标 `stale`（默认 warn，critical 则 abort）。
- **位置**：`agentflow/engine/executor.py`。

### 8. two_phase 自动深挖（§4.10.1）
- **影响**：rca 等汇合节点 token 放大（input_view summary 已缓解，但无「先 summary 再自动深挖」的引导）。
- **实现**：给汇合节点生成 two_phase prompt（phase1 给 summary 让其决定 deep_dive，phase2 用 `read_upstream_output` 拉 details）。
- **位置**：`agentflow/engine/executor.py`（`_build_prompt`）。

### 9. 字段语义分级（critical/supporting/raw + must_pass/optional）（§4.10.2）
- **影响**：当前只有 summary/details 双分层，无字段级分级（critical 永不截断、supporting 可截断、raw 不进 prompt）。
- **实现**：schema 里声明字段分级，executor 按分级裁剪。
- **位置**：`agentflow/domain/schemas.py`、`agentflow/engine/executor.py`。

### 10. idempotency_key + dry-run（§4.11.6）
- **影响**：有副作用节点（committer push、postmortem 写 KG）断点重跑会重复 push/写。
- **实现**：`idempotency_key` 字段已定义，补 executor 执行前检查 + dry-run。
- **位置**：`agentflow/engine/executor.py`。

### 11. 沙箱环境一致性（env_changes 重放 / snapshot）（§4.6）
- **影响**：fix 在沙箱里的环境变更（pip install 等）若不在 tester 沙箱重现，测试假阴性。
- **实现**：`FixResult.env_changes` 字段已定义，补 tester 重放逻辑；M3 起评估 snapshot 为主。
- **位置**：`agentflow/sandbox/`、`agentflow/engine/executor.py`。

### 12. 动态分级审批（risk_level → 决定审批）（§4.7）
- **影响**：当前审批是静态声明（approve 字段/审批节点），不做「低危自动、高危审批」的动态判断。
- **实现**：fix 节点执行后读 `FixPlan.risk_level`，`high` 才触发审批。
- **位置**：`agentflow/engine/executor.py`、`agentflow/domain/schemas.py`。

### 13. token 计数（而非字符数）（§4.10.2）
- **影响**：裁剪用字符数，中文/堆栈 token 密度差异大，误差可达 30%。
- **实现**：裁剪时用 token 计数（需 tokenizer 或近似估算）。
- **位置**：`agentflow/engine/executor.py`。

### 14. read_upstream_output 的 run_id 绑定（§4.10.2）
- **影响**：当前 run_id 是 prompt 注入、LLM 再传回，不可靠（设计是 executor 绑定进工具闭包）。
- **实现**：run_id 由 executor 绑定（per-node 工具闭包/环境变量），LLM 只传 `(node_id, field)`。
- **位置**：`agentflow/tools/read_upstream.py`、`agentflow/engine/executor.py`。

### 15. 全局 inputs 字段分级（§4.10.3）
- **影响**：`$.inputs.*` 全量注入所有节点 prompt，大字段（bug 报告全文）浪费 token。
- **实现**：inputs 的 critical 字段全量、大字段走 reference。
- **位置**：`agentflow/engine/executor.py`。

---

## P2 —— 生产化 / 观测 / 进阶

### 16. Langfuse 真推送（§4.8）
- **影响**：`LlmTraceSink` 只建本地 traces dict，`flush()` 是 no-op，没真发到 Langfuse。
- **实现**：把本地 trace 结构真正调 `_langfuse.trace()/generation()/span()` 推送到 Langfuse（需自托管 Langfuse 服务）。
- **位置**：`agentflow/observability/langfuse.py`。

### 17. OTel MetricsSink 导出（§4.8）
- **影响**：`MetricsSink` 只内存聚合，未接 OTel SDK/collector。
- **实现**：接 OTel SDK + collector + exporter。
- **位置**：`agentflow/observability/otel.py`。

### 18. ServiceTopology 离线预计算（§4.5）
- **影响**：当前读手写静态 `data/topology.json`，无「离线聚合 trace + CMDB + 代码扫描」的预计算 job。
- **实现**：离线 job 聚合生成拓扑，MCP 查询工具读生成结果。
- **位置**：`agentflow/tools/topology.py`、新增离线 job。

---

## 备注

- 本清单与 `CLAUDE.md` 的 TODO 章节一致，是那份的「按优先级排序版」。完成一项后同步更新两处。
- 「实现超出设计」的增强（审批节点、ConsoleSink、json5、permission 自动 reply、成本预算、input_view 裁剪）已在 `DESIGN-v2.md` 回写，不在本清单。
