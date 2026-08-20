# AIOps Bug Fix 场景设计（场景 1 + 场景 2）

> 配套 `DESIGN.md`。两个场景覆盖两类性质完全不同的故障，用来验证 AIOps 平台的诊断与修复闭环：
> - **场景 1**：基础设施故障（资源打满）—— 胜负手在「指标 + K8s + 基础设施修复」。
> - **场景 2**：跨服务代码故障（参数缺失 + 异常被吞）—— 胜负手在「链路追踪 + 多仓库定位 + 负证据排除」。

---

## 一、场景总览（对比）

| 维度 | 场景 1 | 场景 2 |
|---|---|---|
| 业务入口 | order-service 打印报价单 | order-service 结账 |
| 故障现象 | 报价单无法生成/下载（报错） | 结账无响应/挂起（**无报错**） |
| 根因类型 | 基础设施（CPU 100% + 磁盘 100%） | 代码 bug（`fin` 缺参 + 空 catch 吞异常） |
| 根因所在服务 | order-service（ticket 同服务） | warranty-service（**ticket 下游服务**） |
| 核心诊断 agent | metrics-analyst + infra-locator | trace-analyst + code-locator（多仓库） |
| 核心修复 agent | infra-remediator（止血+根治） | fix-implementer（跨仓库改代码/配置） |
| 关键诊断信号 | 日志 IOException（果）→ 指标 100%（因） | 无 first_error + trace 故障 span 在下游 |

---

## 二、场景 1：订单服务报价单打印失败（基础设施故障）

### 2.1 场景定义

| 维度 | 内容 |
|---|---|
| 业务场景 | order-service「打印报价单」：生成报价单文件（PDF/Excel）→ 下载 → 打印 |
| 故障现象 | 报价单打印/下载突然无法进行，业务人员批量反馈 |
| 服务形态 | Spring Cloud Java 微服务，容器化部署在 K8s |
| 日志链路 | 应用日志 → JSON 化 → Logstash → Elasticsearch（每 5s 同步） |
| 代码管理 | GitHub |
| 已知根因 | pod **CPU 100% + 硬盘 100%** → 文件无法生成 → 无法下载 |

> 日志是「延迟 5s 的准实时」，指标（Prometheus）和 K8s 状态是「实时」。CPU/磁盘 100% 这类问题日志里只有「果」（`IOException`），「因」（资源打满）必须在指标和 K8s 侧定位——这正是需要基础设施类 agent 的原因。

### 2.2 ServiceNow Ticket（模拟数据）

```json
{
  "sys_id": "8f3a2c1d4e5f...",
  "number": "INC0012345",
  "short_description": "订单服务报价单打印失败",
  "description": "业务人员点击「打印报价单」后长时间无响应，最终提示下载失败、无法生成报价单文件。影响全部业务人员，已持续 10 分钟。",
  "category": "Application",
  "subcategory": "order-service",
  "impact": "1 - High",
  "urgency": "1 - High",
  "priority": "P1",
  "state": "New",
  "opened_at": "2026-08-19T14:30:00+08:00",
  "opened_by": "zhang.san@company.com",
  "assignment_group": "AIOps",
  "cmdb_ci": { "name": "order-service", "service": "订单服务", "environment": "production", "namespace": "order", "pod": "order-service-7b9c8d5f6-abcde" },
  "symptom": "报价单文件生成失败 / 下载失败",
  "correlation_hint": { "trace_id": "trace-20260819-143000-abc123", "first_error": "java.io.IOException: No space left on device" }
}
```

### 2.3 根因链路

```
现象       报价单打印/下载失败
  ↓
直接错误   java.io.IOException: No space left on device（文件写入失败）
  ↓
直接原因   order-service pod 磁盘 100%（emptyDir tmpfs 写满）
  ↓
并发因素   ├─ 磁盘 100% → 报价单临时文件无法落盘
  └─ CPU 100%  → 线程池耗尽 / 请求超时 / 重试风暴
  ↓
深层根因（磁盘） └─【代码 bug】QuotationService.generateFile() 临时文件异常路径 finally 未清理（日志走 stdout，不在故障盘上）
深层根因（CPU）  ├─ 文件写入失败 → 重试风暴 → CPU 打满
  └─ 磁盘+内存压力 → GC 频繁
```

**结论**：双症状（CPU + 磁盘）+ 双层修复——① 止血（infra 扩容/清盘）先恢复业务，② 根治（修代码 temp file 泄漏 + K8s 资源限制）防复发。

### 2.4 Agent 分工

诊断侧（只读）：`triage` → 并行 `log-analyst` / `trace-analyst` / **`metrics-analyst`** / **`infra-locator`** / `code-locator` / `knowledge-lookup` → `root-cause`。
解决侧（写）：`fix-planner` → 并行 `fix-implementer`（改代码/配置）+ **`infra-remediator`**（改 K8s + 止血）→ `tester` → `reviewer` → `committer` → `postmortem`。

本场景新增的 3 个基础设施 agent（详见 `DESIGN.md` 增量）：

| Agent | 职责 | 工具 | 输出 schema |
|---|---|---|---|
| 🆕 metrics-analyst | 查 Prometheus，提取 CPU/内存/磁盘/GC 时序（**得「因」**） | metrics 查询 MCP | `MetricsEvidence` |
| 🆕 infra-locator | 查 K8s 对象状态（pod/PVC/events/restart/驱逐） | K8s 只读 MCP | `InfraEvidence` |
| 🆕 infra-remediator | 改 K8s 对象 + 止血（scale/清盘/重启），写操作审批 | K8s 写 MCP | `RemediationResult` |

### 2.5 workflow YAML

```yaml
name: order-service-quotation-print-fail
inputs:
  repo: { type: string, required: true }
  bug_report: { type: object, required: true }
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
  commit:    { agent: committer,        params: { diff: "$.nodes.fix.output.diff", test: "$.nodes.test.output" }, approve: write }
  recap:     { agent: postmortem,       params: { rca: "$.nodes.rca.output", fix: "$.nodes.fix.output", remediate: "$.nodes.remediate.output" } }
edges:
  - { from: triage, to: logs }      - { from: triage, to: trace }     - { from: triage, to: metrics }
  - { from: triage, to: infra }     - { from: triage, to: locate }    - { from: triage, to: know }
  - { from: logs, to: rca }    - { from: trace, to: rca }    - { from: metrics, to: rca }
  - { from: infra, to: rca }   - { from: locate, to: rca }   - { from: know, to: rca }
  - { from: rca, to: plan }
  - { from: plan, to: fix }        - { from: plan, to: remediate }
  - { from: fix, to: test }        - { from: remediate, to: test }
  - { from: test, to: review, when: "$.nodes.test.output.passed == true" }
  - { from: review, to: commit }
  - { from: commit, to: recap }
```

### 2.6 测试数据

**日志（ES，Logstash 已转 JSON）**：

```json
{ "@timestamp": "2026-08-19T14:30:00.123+08:00", "level": "ERROR", "service": "order-service",
  "pod": "order-service-7b9c8d5f6-abcde", "logger": "com.company.order.service.QuotationService",
  "message": "生成报价单失败: java.io.IOException: No space left on device",
  "stack_trace": "java.io.IOException: No space left on device\n  at java.io.FileOutputStream.write0(Native Method)\n  at com.company.order.service.QuotationService.generateFile(QuotationService.java:128)\n  ...",
  "trace_id": "trace-20260819-143000-abc123" }
```

**指标（Prometheus）**：

```
rate(container_cpu_usage_seconds_total{pod="order-service-..."}[5m]) → 100%
container_fs_usage_bytes / container_fs_limit_bytes → 100%（14:28 触顶）
rate(jvm_gc_pause_seconds_sum[5m]) 显著上升（CPU 100% 佐证）
```

**代码（GitHub，bug 注入点）**：

```java
// QuotationService.java —— 临时文件泄漏 bug
public File generateQuotation(Order order) {
    File tmp = File.createTempFile("quotation_", ".pdf", new File("/data/tmp"));
    try {
        renderQuotation(order, tmp);
        return tmp;
    } catch (Exception e) {
        // ⚠️ bug：异常路径未清理 tmp → 磁盘被临时文件逐渐写满
        throw new QuotationException("生成报价单失败", e);
    }
}
```

```xml
<!-- logback-spring.xml —— 日志走 stdout（JSON），不与故障盘同盘 -->
<appender name="CONSOLE" class="ch.qos.logback.core.ConsoleAppender">
    <encoder class="net.logstash.logback.encoder.LogstashEncoder"/>  <!-- JSON 输出，供 Logstash 采集 -->
</appender>
```

**K8s 清单（配置缺陷）**：

```yaml
spec:
  replicas: 1
  template:
    spec:
      containers:
      - name: order-service
        image: company/order-service:latest
        env:
        - name: QUOTATION_TEMP_LEAK   # 场景1 bug 开关
          value: "false"
        resources:
          limits:
            cpu: "500m"              # ⚠️ 小 CPU limit：轻载即 100% throttled（秒级触发）
        volumeMounts: [ { mountPath: /data, name: data } ]
  volumes:
  - name: data
    emptyDir:
      medium: Memory                # tmpfs，有真实大小限制
      sizeLimit: 512Mi              # ⚠️ 小盘：秒级写满
```

### 2.7 解决过程（证据链）

| 步骤 | Agent | 关键发现 |
|---|---|---|
| Step 0 | triage | 归一化 BugReport（服务/pod/时间窗/P1） |
| Step 1 | log-analyst | `IOException: No space left on device` @ `QuotationService.java:128`（果） |
| Step 1 | trace-analyst | `generateQuotation` span 延迟 200ms→30s 超时（果） |
| Step 1 | metrics-analyst | **CPU 100% + 磁盘 100%**，14:28 触顶，GC 频繁（因） |
| Step 1 | infra-locator | pod Running 但 CPU throttled，**PVC 512Mi/512Mi 满**，无 OOM/驱逐（因） |
| Step 1 | code-locator | `generateFile()` 临时文件异常路径未清理（深层因） |
| Step 1 | knowledge-lookup | 召回「磁盘满→文件生成失败」修复模式 |
| Step 2 | root-cause | 磁盘 100%→文件写入失败（0.95）+ CPU 100%→重试风暴（0.85）+ temp file 泄漏（0.90） |
| Step 3 | fix-planner | 止血（PVC 扩容/清盘/扩容副本）+ 根治（修 temp leak + resources.limits + HPA） |
| Step 4 | fix-implementer + infra-remediator | 改代码/配置（沙箱自测）+ 改 K8s（scale/apply，**写审批**） |
| Step 5 | tester | 报价单生成/下载恢复，CPU/磁盘回落，回归通过 |
| Step 6-8 | reviewer → committer → postmortem | 审查 diff → 提交 PR（审批）→ 复盘 |

---

## 三、场景 2：订单结账 → warranty 三包期查询 `fin` 缺参（跨服务代码故障）

### 3.1 场景定义

| 维度 | 内容 |
|---|---|
| 业务场景 | order-service「结账」时同步调用 warranty-service 查询该保养车辆是否仍在三包期内 |
| 故障现象 | 点击「结账」后**无响应**（不是报错，一直转圈），结账无法完成 |
| 服务形态 | order-service（结账）+ warranty-service（三包期查询）+（可选）warranty-data-service（下游数据） |
| 已知根因 | warranty-service 查询三包期内部必填参数 `fin` 未传 → 抛异常 → 结账无响应 |
| 关键差异 | **根因在 ticket 报告服务的下游（warranty-service）**；症状是「无响应」而非「报错」 |

> `fin` = Fahrzeug-Identifizierungs-Nummer（德文）＝ VIN 车辆识别码/车架号，三包期查询用它定位车辆。

### 3.2 ServiceNow Ticket（模拟数据）

关键特征：**无明确错误信息**（`first_error: null`），因为「无响应」而非「报错」：

```json
{
  "sys_id": "9c2b4e6f...",
  "number": "INC0012456",
  "short_description": "订单服务结账操作无响应",
  "description": "业务人员点击「结账」后，页面一直转圈，无任何错误提示，结账无法完成。涉及保养车辆订单的结账，已持续 15 分钟。",
  "category": "Application",
  "subcategory": "order-service / checkout",
  "impact": "1 - High",
  "urgency": "1 - High",
  "priority": "P1",
  "state": "New",
  "opened_at": "2026-08-19T15:20:00+08:00",
  "opened_by": "li.si@company.com",
  "assignment_group": "AIOps",
  "cmdb_ci": { "name": "order-service", "service": "订单服务", "environment": "production", "namespace": "order" },
  "symptom": "结账无响应 / 挂起",
  "correlation_hint": { "trace_id": "trace-20260819-152000-def456", "first_error": null }
}
```

### 3.3 根因链路

```
现象       结账操作无响应（无报错）
  ↓
阻塞点     order-service 同步调用 warranty-service.checkWarranty() 一直等待
  ↓
直接原因   order-service Feign/HTTP 客户端未配超时 → 无限阻塞
  ↓
下游异常   warranty-service 内部查询三包期时，必填参数 fin 未传给下游
  ↓
异常抛出   warranty-data-service / DAO 抛 MissingServletRequestParameterException
            或 MyBatis BindingException: Parameter 'fin' not found
  ↓
异常被吞   warranty-service 空 catch 吞异常（只 log 不返回响应体）→ HTTP 响应未完成
  ↓
结论       「异常被吞 + 无超时」=「无响应」而非「报错」的根因组合
```

**两个 bug + 一个配置缺陷**：

| # | 类型 | 位置 | 内容 |
|---|---|---|---|
| 1 | 代码 bug（主） | warranty-service | 调下游查询时**漏传 `fin`** |
| 2 | 代码 bug（次） | warranty-service | 空 catch 吞异常，不返回响应体 → 请求挂起 |
| 3 | 配置缺陷 | order-service | Feign 未配 readTimeout → 下游挂起时自身无限等待 |

> 诊断价值点：若只有 bug#1 而异常被正确传播，用户看到的是「结账报错 500」而非「无响应」。**「无响应」这个症状本身就指向「吞异常 + 无超时」**——log-analyst 和 trace-analyst 都要重点确认。

### 3.4 Agent 分工（与场景 1 对比）

同一套 15-agent 编队，但各 agent 权重完全不同：

| Agent | 场景 1 | 场景 2 | 说明 |
|---|---|---|---|
| triage | 高 | **高** | 识别「无响应=挂起类」 |
| log-analyst | 高（IOException） | **高** | 查 warranty 的 fin 缺参异常 + 「有请求日志无完成日志」 |
| trace-analyst | 中 | **极高（核心）** | 跨服务定位故障 span 在 warranty-service |
| metrics-analyst | **极高** | 低（**负证据**） | 排除基础设施 |
| infra-locator | **极高** | 低（**负证据**） | 排除基础设施 |
| code-locator | 中（单仓库） | **高（多仓库）** | 跨 order→warranty 两仓库定位 |
| knowledge-lookup | 中 | 中 | 召回「参数缺失」「吞异常」模式 |
| root-cause | 高 | 高 | 综合正证据 + 负证据 |
| fix-planner | 高 | 高 | 分层方案 |
| fix-implementer | 中 | **高（改两服务）** | 改 warranty 代码 + order 超时配置 |
| infra-remediator | **极高** | **不激活** | 无 infra 变更 |
| tester/reviewer/committer/postmortem | 常 | 常 | committer 提交 warranty-service PR |

### 3.5 workflow YAML（复用同一 DAG，无 infra-remediator）

```yaml
name: order-service-checkout-no-response
inputs:
  repo: { type: string }                      # ⚠️ 可能涉及多仓库（order + warranty）
  bug_report: { type: object, required: true }
nodes:
  triage:    { agent: triage,           params: { bug: "$.inputs.bug_report" } }
  logs:      { agent: log-analyst,      params: { bug: "$.nodes.triage.output.summary" } }
  trace:     { agent: trace-analyst,    params: { bug: "$.nodes.triage.output.summary" } }
  metrics:   { agent: metrics-analyst,  params: { bug: "$.nodes.triage.output.summary" } }   # 负证据
  infra:     { agent: infra-locator,    params: { bug: "$.nodes.triage.output.summary" } }   # 负证据
  locate:    { agent: code-locator,     params: { bug: "$.nodes.triage.output.summary", repo: "$.inputs.repo" } }
  know:      { agent: knowledge-lookup, params: { bug: "$.nodes.triage.output.summary" } }
  rca:       { agent: root-cause,       params: { logs: "$.nodes.logs.output", trace: "$.nodes.trace.output",
                                                   metrics: "$.nodes.metrics.output", infra: "$.nodes.infra.output",
                                                   code: "$.nodes.locate.output", know: "$.nodes.know.output" } }
  plan:      { agent: fix-planner,      params: { rca: "$.nodes.rca.output" } }
  fix:       { agent: fix-implementer,  params: { plan: "$.nodes.plan.output" }, approve: high-risk }
  test:      { agent: tester,           params: { fix: "$.nodes.fix.output" } }
  review:    { agent: reviewer,         params: { diff: "$.nodes.fix.output.diff" } }
  commit:    { agent: committer,        params: { diff: "$.nodes.fix.output.diff", test: "$.nodes.test.output" }, approve: write }
  recap:     { agent: postmortem,       params: { rca: "$.nodes.rca.output", fix: "$.nodes.fix.output" } }
edges:
  - { from: triage, to: logs }  - { from: triage, to: trace } - { from: triage, to: metrics }
  - { from: triage, to: infra } - { from: triage, to: locate } - { from: triage, to: know }
  - { from: logs, to: rca } - { from: trace, to: rca } - { from: metrics, to: rca }
  - { from: infra, to: rca } - { from: locate, to: rca } - { from: know, to: rca }
  - { from: rca, to: plan } - { from: plan, to: fix } - { from: fix, to: test }
  - { from: test, to: review, when: "$.nodes.test.output.passed == true" }
  - { from: review, to: commit } - { from: commit, to: recap }
```

### 3.6 测试数据

**order-service 日志（有请求进入，无请求完成）**：

```json
{ "@timestamp": "2026-08-19T15:20:01.000+08:00", "level": "INFO", "service": "order-service",
  "message": "结账请求进入: orderId=ORD20260819001, 开始查询三包期...", "trace_id": "trace-...-def456" }
// ⚠️ 之后 15 分钟无「结账完成」日志 → 请求卡在等待 warranty 响应
```

**warranty-service 日志（异常被吞，只 log 不返回）**：

```json
{ "@timestamp": "2026-08-19T15:20:01.200+08:00", "level": "ERROR", "service": "warranty-service",
  "logger": "com.company.warranty.service.WarrantyServiceImpl",
  "message": "查询三包期失败: org.apache.ibatis.binding.BindingException: Parameter 'fin' not found. Available parameters are [orderId, param2]",
  "stack_trace": "...WarrantyServiceImpl.checkWarranty(WarrantyServiceImpl.java:88)...",
  "trace_id": "trace-...-def456" }
// ⚠️ 关键：有 ERROR 日志，但无对应「响应返回」日志 → 异常被吞、响应未完成
```

**链路追踪（本场景最关键证据）**：

```
checkout span (order-service)                    ── running，15 分钟未结束  ← 挂起
   └─ checkWarranty span (warranty-service)      ── running，未结束        ← 挂起
        └─ queryWarrantyPeriod span (warranty-data-service) ── error       ← 真正故障点
             error_tag: MissingServletRequestParameterException: fin is required
```

**warranty-service 代码（两个 bug）**：

```java
public WarrantyResult checkWarranty(String orderId) {
    Order order = orderDao.getById(orderId);
    String fin = order.getVehicleFin();          // 已取出车辆识别码
    try {
        // ⚠️ bug#1：漏传 fin 给下游
        return warrantyDataClient.queryWarrantyPeriod(orderId);
    } catch (Exception e) {
        // ⚠️ bug#2：空 catch 吞异常，不抛、不返回 → 请求挂起
        log.error("查询三包期失败", e);
    }
    return null;
}
```

**order-service 代码（配置缺陷）**：

```java
@FeignClient(name = "warranty-service")
public interface WarrantyClient {
    @PostMapping("/checkWarranty")
    WarrantyResult checkWarranty(@RequestParam String orderId);
}
// ⚠️ 缺陷：Feign 未配 readTimeout → warranty 挂起时自身无限等待
// 修复时用 Spring Cloud 2023.0 新配置 key（kebab-case）：
//   spring.cloud.openfeign.client.config.default.read-timeout=3000
```

**指标/K8s（负证据，用于排除）**：

```
rate(container_cpu_usage_seconds_total{service=~"order-service|warranty-service"}[5m]) → 正常 (<20%)
container_fs_usage_bytes / limit → 正常 (<30%)
kubectl get pods → 全部 Running，无重启，无 OOM，无驱逐
```

### 3.7 解决过程（证据链）

| 步骤 | Agent | 关键发现 |
|---|---|---|
| Step 0 | triage | 识别「无 first_error + 无响应」= 挂起类，重点「找阻塞点 + 跨服务调用链」 |
| Step 1 | log-analyst | order 有请求进入无完成；warranty 有 `BindingException: fin not found` + 有 ERROR 无响应返回 → 异常被吞 |
| Step 1 | trace-analyst | **故障 span 在 warranty→warranty-data（fin 缺参 error）**，checkout span 挂起 15 分钟 |
| Step 1 | metrics-analyst | **负证据**：CPU/磁盘/内存正常 → 排除基础设施 |
| Step 1 | infra-locator | **负证据**：pod 全部 Running，无重启/OOM → 排除基础设施 |
| Step 1 | code-locator | 定位 warranty `checkWarranty()`：漏传 fin + 空 catch 吞异常 |
| Step 1 | knowledge-lookup | 召回「跨服务参数缺失」「空 catch 吞异常」模式 |
| Step 2 | root-cause | fin 漏传（0.95）+ 空 catch 吞异常（0.90）+ Feign 无超时（0.85）+ **排除基础设施（0.05）** |
| Step 3 | fix-planner | 传 fin + 修空 catch + Feign 配超时（均中低危，无需 infra-remediator） |
| Step 4 | fix-implementer | **跨两个仓库**改代码/配置 → 沙箱自测 |
| Step 5-8 | tester → reviewer → committer（warranty PR，审批）→ postmortem | 结账恢复 + 超时快速失败 |

---

## 四、服务间关联关系：获取方式 + 使用时机

### 4.1 先厘清两个层次（避免混淆）

| 层次 | 内容 | 来源 | 作用 | 生命周期 |
|---|---|---|---|---|
| **服务级拓扑（全局）** | order→warranty→warranty-data 的调用关系 + 各服务 repo/owner/env | **预计算聚合** | 作为**输入**被各 agent 查询 | 离线持续刷新 |
| **请求级调用链（本次）** | 某一次请求的 span 树 | **本次 trace** | trace-analyst 的**产出** | 随 ticket 实时查询 |

关键：**服务级拓扑是「先验知识」，请求级调用链是「本次证据」**。trace-analyst 用「本次 trace」产出请求级调用链；而「服务级拓扑」是另一个预计算的 MCP 输入，供需要「全局视角」的 agent 用。

### 4.2 获取方式（来源 + 合并策略）

按权威度排序：

| 来源 | 类型 | 得到什么 | 权威度 |
|---|---|---|---|
| **链路追踪聚合**（gateway MDC traceId → ES 日志，聚合 caller→callee） | 动态 | **真实调用关系** caller→callee（谁实际调用谁） | 最高 |
| **CMDB** | 静态 | service→repo / owner / environment / namespace 元数据 | 高（元数据） |
| **代码/配置静态扫描** | 静态 | `@FeignClient`、`RestTemplate`、`application.yml` 声明的依赖 | 中（可能死代码） |
| **服务注册中心**（Eureka/Nacos/Consul） | 静态 | 服务实例注册关系（非调用关系） | 低（补充） |
| **Service Mesh / API Gateway**（Istio/Kiali） | 动态 | L7 service graph | 高（有 mesh 时最方便） |

**合并策略**：
- **拓扑层（谁调用谁）**：以 trace 聚合为主（动态真实），代码扫描/服务注册补充 trace 覆盖不到的冷链路。
- **元数据层（repo/owner/env）**：以 CMDB 为主。

### 4.3 数据结构 + MCP 工具

```json
// ServiceTopology（预计算，落 KV/图库）
{
  "services": {
    "order-service":  { "repo": "https://github.com/xqfgbc/aiops-test-order-service",  "owner": "order-team",   "namespace": "order" },
    "warranty-service": { "repo": "https://github.com/xqfgbc/aiops-test-warranty-service", "owner": "warranty-team", "namespace": "order" }
  },
  "edges": [
    { "caller": "order-service",   "callee": "warranty-service",      "endpoint": "POST /checkWarranty",       "source": "trace", "qps": 120 },
    { "caller": "warranty-service","callee": "warranty-data-service", "endpoint": "POST /queryWarrantyPeriod", "source": "trace", "qps": 120 }
  ]
}
```

暴露为 MCP 工具 `service_topology`，方法：

| 方法 | 返回 | 用途 |
|---|---|---|
| `get_service(service)` | repo/owner/env 元数据 | code-locator 找仓库 |
| `get_dependencies(service)` | 下游（X 调用了谁） | trace-analyst 预期拓扑 |
| `get_dependents(service)` | 上游（谁调用了 X） | **blast radius 影响面** |
| `get_path(a, b)` | a→b 调用路径 | root-cause 故障传播 |

### 4.4 谁在什么时候用（agent × 时机 × 用途）

| Agent | 时机 | 用哪种关联 | 具体用途 |
|---|---|---|---|
| **triage** | 接入时 | 静态元数据 `get_service` | 从 ticket 的 service 定位 repo/环境，填 BugReport；初步评估影响面 |
| **trace-analyst** | 诊断时 | 动态拓扑 `get_dependencies`（先验） | 知道预期拓扑，更快定位异常 span（本身也在产出本次调用链） |
| **code-locator** | 诊断时 | 静态 `get_service`（service→repo） | **关键**：故障在 warranty-service 时据此 clone 正确仓库 |
| **root-cause** | 综合时 | 动态 `get_dependents` + `get_path` | 故障传播分析：warranty 挂了影响哪些上游；圈定受影响服务 |
| **fix-planner** | 修复时 | 动态 `get_dependents` + 静态元数据 | 评估改动影响面、列出受影响服务、风险定级 |
| **tester** | 修复后 | 动态拓扑 | 决定跑哪些集成测试（涉及 order+warranty 的调用链） |
| **knowledge-lookup** | 诊断时 | 静态元数据（owner/history） | 按服务/团队召回历史案例 |
| **postmortem** | 复盘时 | 动态拓扑 | 复盘报告画受影响拓扑图 |

### 4.5 生命周期（关键结论）

关联关系**不是某个 agent 在运行时现算的**，而是：

1. **离线预计算**：一个独立 job 周期性（如每小时）聚合 trace → 服务拓扑；同步 CMDB → 元数据。产物落 `ServiceTopology`。
2. **诊断时查询**：`trace-analyst` 查本次 trace（产出请求级链），`code-locator` 查 `get_service`（拿 repo），`root-cause` 查 `get_dependents`（传播分析）。
3. **修复时查询**：`fix-planner`/`tester` 查拓扑评估影响面 + 定集成测试范围。

> 与日志/指标/CMDB 一样，服务拓扑是「数据源 MCP 层」的一员，统一预计算 + MCP 查询，**不是 agent 的运行时工具能力**。

---

## 五、两个场景的模拟与端到端验证方案

> 目标：让**完成后的 AIOps 程序**（agentflow 平台 + 15 个 agent + 数据源 MCP + 沙箱 + 审批 + 观测）能证明「真的能定位并解决这两个场景」。

### 5.1 验证分层（三层递进）

| 层 | 验证什么 | 数据源 | 依赖 | 可重复性 |
|---|---|---|---|---|
| **L1 Agent 单测** | 各 agent 逻辑：喂 fixture → 断言输出 schema + 关键字段 | fixture JSON | 无 | 高 |
| **L2 Workflow 集成** | DAG 编排：拓扑/并行/汇合/条件边/审批事件/上下文传递/断点续跑 | mock MCP server | 无 | 高 |
| **L3 端到端真实环境** | 全闭环：真实采集 + 真实修复 + 服务恢复 | 真实 K8s + 故障注入 | 有（需集群） | 中 |

### 5.2 两种模拟方式

| 方式 | 做法 | 优点 | 缺点 | 适用 |
|---|---|---|---|---|
| **Mock 数据源** | `mock-datasource` MCP server 读 fixture 目录返回预置数据，工具签名与真实一致 | 快、可重复、可断言、不依赖真实服务 | 只验证「查询+推理+修复」，不验证「采集」 | L1/L2 |
| **真实故障注入** | 真实 K8s + chaos（写满磁盘 / CPU 压满 / bug 版本镜像） | 验证真实采集链路 + 真实修复落地 | 重、慢、需真实服务 | L3 |

**Mock 数据源设计**：一个 `mock-datasource` MCP server（fastmcp），提供与真实数据源相同的工具签名 `query_logs`（含 traceId 关联） / `query_metrics` / `describe_pod` / `service_topology`。通过环境变量 `MOCK_FIXTURE_DIR` 指向 fixture 目录，测试时读 fixture 返回。**agent 的 frontmatter 挂载的 MCP 工具名不变，只把 MCP server 从真实换成 mock** —— 逻辑与真实一致，只有数据源被 stub。

### 5.3 通用断言框架

LLM 输出非确定，断言只锚定**关键字段**（不锚定全文）：

```python
# tests/conftest.py —— 通用断言辅助
def assert_root_cause(output, expected: dict):
    """断言 root-cause 的根因类型/服务/置信度阈值，而非全文"""
    assert output["root_cause_type"] == expected["type"]          # infra vs code
    assert expected["service"] in output["affected_services"]     # order-service / warranty-service
    assert output["confidence"] >= expected["min_confidence"]     # 阈值
```

每个场景带一份 `expected.json`（golden 期望），断言 AIOps 输出与期望关键字段一致。

### 5.4 场景 1 模拟（真实故障注入）

**环境准备**：
1. minikube（podman driver）集群；部署 order-service（emptyDir tmpfs 512Mi）+ ES/Kibana + Prometheus/Grafana。
2. 部署「bug 版本」镜像：`QuotationService.generateFile()` 临时文件不清理 + deployment 设 500m CPU limit + 512Mi PVC（缩小阈值、秒级触发，见 §6.4）。
3. 预灌一批报价单临时文件（加速磁盘占满）。

**故障注入**（小资源秒级触发，或二选一）：
```bash
# 磁盘 100%：写满emptyDir tmpfs（512Mi，秒级）
kubectl exec deploy/order-service -- dd if=/dev/zero of=/data/tmp/fill bs=1M count=512
# CPU 100%：压满小 limit（500m，1 核即 throttled 到 100%）
kubectl exec deploy/order-service -- 忙循环（while true，1 核）
```
（或直接用 chaos-mesh 的 `PodChaos` / `StressChaos` 注入）

**触发**：
```bash
python -m agentflow run examples/order-service-quotation-print-fail.yaml --trigger fixtures/scenario1/ticket.json
```

**断言（验收点）**：

| # | 断言 | 期望 |
|---|---|---|
| 1 | triage | BugReport 归一化正确（service=order-service，P1） |
| 2 | log-analyst | 查到 `IOException: No space left on device` |
| 3 | metrics-analyst | `MetricsEvidence` 含 CPU 100% + 磁盘 100% |
| 4 | infra-locator | `InfraEvidence` 含 PVC 满 + CPU throttled |
| 5 | root-cause | 根因含「磁盘 100% 导致文件生成失败」，置信度 ≥ 0.85 |
| 6 | fix-planner | 方案含止血 + 根治两层 |
| 7 | infra-remediator | 触发**写操作审批**（ask）；通过后 scale + 清盘生效 |
| 8 | tester | 报价单生成/下载恢复，回归通过 |
| 9 | 最终 | `curl order-service/.../quotation` 返回 200，文件可生成 |
| 10 | 观测 | Langfuse 可见完整 run 的 trace（每节点 prompt/token/cost/工具调用） |

### 5.5 场景 2 模拟（真实故障注入）

**环境准备**：
1. minikube（podman driver）集群；部署 order-service + warranty-service + warranty-data-service（三服务）。
2. warranty-service 部署「bug 版本」镜像（`checkWarranty` 漏传 fin + 空 catch 吞异常）；order-service Feign 无 readTimeout。
3. CMDB 预置 service→repo 映射（order-service / warranty-service 各指向对应 GitHub 仓库）。

**故障注入**：直接调用结账 API，或投递 ticket 触发。

**触发**：
```bash
python -m agentflow run examples/order-service-checkout-no-response.yaml --trigger fixtures/scenario2/ticket.json
```

**断言（验收点）**：

| # | 断言 | 期望 |
|---|---|---|
| 1 | triage | 识别「无 first_error + 无响应」= 挂起类 |
| 2 | trace-analyst | 定位故障 span 在 **warranty-service**（非 order-service） |
| 3 | code-locator | 经 `service→repo` 映射 clone 到 **warranty 仓库**，定位 `checkWarranty()` 漏传 fin + 空 catch |
| 4 | metrics-analyst / infra-locator | 输出「正常」（负证据） |
| 5 | root-cause | 根因含「fin 缺参 + 吞异常」，且**显式排除基础设施** |
| 6 | fix-planner | 方案含传 fin + 修空 catch + Feign 超时，无 infra-remediator |
| 7 | fix-implementer | **跨两仓库**改代码/配置 |
| 8 | tester | 结账恢复 + 超时能快速失败 |
| 9 | 最终 | 结账 API 返回 200，三包期查询成功 |
| 10 | 观测 | Langfuse trace 完整 |

### 5.6 端到端验收标准清单（通用）

完成后的 AIOps 程序需同时满足：

1. **闭环**：两个场景都能从 ticket 一路跑到 postmortem 完成，中间无人工干预（除审批门禁）。
2. **根因准确**：root-cause 与预设根因一致（场景 1=磁盘/CPU，场景 2=fin 缺参），置信度 ≥ 0.8。
3. **恢复生效**：修复后服务恢复（场景 1 报价单可下载，场景 2 结账成功）。
4. **审批生效**：写操作（infra-remediator / committer）被 `ask` 拦截，人工确认后才落地。
5. **可观测**：Langfuse 能看到完整 run 的 trace（逐节点 prompt/completion/token/cost/工具调用/审批状态）。
6. **断点续跑**：中途 kill 重启，已 done 节点幂等跳过，failed 节点重跑。
7. **沙箱隔离**：tester / fix-implementer 的代码执行发生在 opensandbox 容器内，非宿主机 shell。

---

## 六、测试场景搭建详细设计

> 目标：从零搭建一套能复现两个场景、能接入 AIOps 平台做端到端验收的测试床（testbed）。分「真实故障注入」（验证真实采集+修复落地，对应 L3）和「Mock 数据源」（验证诊断推理逻辑，对应 L1/L2）两条路径，**共享同一套 agent/workflow 定义**，仅数据源层可切换。

### 6.1 测试床总体拓扑

```
┌────────────────────────────────────────────────────────────────┐
│           K8s 集群（minikube --driver=podman，本地）                 │
│                                                                │
│  ┌──────────────┐   ┌───────────────┐   ┌────────────────────┐ │
│  │ gateway-svc  │──▶│ order-service │──▶│ warranty-service   │ │
│  │ (traceId 入口)│   │ (场景1被测+结账)│   │ (场景2 fin bug点)   │ │
│  └──────┬───────┘   └──────┬────────┘   └─────────┬──────────┘ │
│         │ JSON 日志(traceId)│                      │            │
│         ▼                  ▼                      │            │
│  ┌──────────────┐  ┌───────────────┐                │            │
│  │  Logstash    │─▶│ Elasticsearch  │  (日志, 5s)    │            │
│  └──────────────┘  └───────────────┘                │            │
│  ┌──────────────┐  ┌───────────────┐                │            │
│  │  Prometheus  │─▶│   Grafana      │  (指标/CPU/磁盘)│           │
│  └──────────────┘  └───────────────┘                │            │
└───────────────────────────────────────────────────────────────┘
        │ MCP 查询（logs / metrics / k8s / topology）
        ▼
┌────────────────────────────────────────────────────────────────┐
│            AIOps 平台（agentflow，宿主机本地 / 单独容器）            │
│  DAGExecutor + 15 agents + opensandbox(podman) + Langfuse(自托管) │
│  数据源 MCP server 可按 MOCK_FIXTURE_DIR 切换真实/mock              │
└────────────────────────────────────────────────────────────────┘
```

### 6.2 组件清单与版本

| 组件 | 用途 | 场景1 | 场景2 | 选型/版本 |
|---|---|---|---|---|
| K8s 集群 | 跑被测服务 + 数据源 | ✓ | ✓ | minikube（`--driver=podman`，本地）/ 生产测试 ns |
| gateway-service | 入口，traceId 经 MDC 设置 + 传播 | ✓ | ✓ | Spring Cloud Gateway（Boot 3.3.x） |
| order-service | 场景1被测 + 场景2调用方 | ✓ | ✓ | Spring Boot 3.3.x + OpenFeign |
| warranty-service | 场景2 bug 点 | ✗ | ✓ | 同上（纯 Boot，内存 Map） |
| Logstash → Elasticsearch | 日志采集/存储 | ✓ | ✓ | 7.x/8.x，JSON codec，5s 同步 |
| Kibana | 日志可视化（人工比对） | ✓ | ✓ | 同 ES 版本 |
| Prometheus + Grafana | 指标（CPU/磁盘/GC） | ✓ | ✓ | Prometheus + node-exporter + kube-state-metrics + **Micrometer（Actuator）** |
| 链路追踪 | 不引 SkyWalking；traceId 经 gateway MDC 进日志，ES 按 traceId 关联 | ✓ | ✓ | gateway + 各服务 Filter + ES |
| GitHub 仓库 | code-locator/fix 输入 | ✓ | ✓ | gateway + order + warranty 三私有 repo |
| agentflow | AIOps 平台（被测） | ✓ | ✓ | 本地 Python 调试 |
| opensandbox | 沙箱跑测试 | ✓ | ✓ | podman |
| Langfuse | 观测 | ✓ | ✓ | 自托管 |
| chaos 工具 | 故障注入 | ✓ | 可选 | chaos-mesh / 或手写脚本 |

### 6.3 目录结构与一键命令

```
my-agent-cc/
├── examples/
│   ├── order-service-quotation-print-fail.yaml    # 场景1 workflow
│   └── order-service-checkout-no-response.yaml    # 场景2 workflow
├── testbed/                          # 测试床（新增）
│   ├── README.md                     # 一键搭建说明
│   ├── Makefile                      # make up / make scenario1 / make scenario2 / make down
│   ├── minikube/start.sh             # 起 minikube（podman driver）+ 开 addons
│   ├── services/                     # 三个被测服务源码（各为独立 git 仓库）
│   │   ├── gateway-service/
│   │   ├── order-service/
│   │   └── warranty-service/
│   ├── manifests/                    # 被测服务 + 数据源 K8s 清单
│   │   ├── gateway-service/  order-service/  warranty-service/
│   │   ├── elasticsearch/  prometheus/
│   ├── fault-inject/                 # 故障注入脚本（env 开关 bug，不改镜像）
│   │   ├── scenario1_disk_cpu.sh
│   │   └── scenario2_enable_bug.sh
│   ├── mock-datasource/              # Mock 数据源 MCP server（L1/L2 用）
│   │   ├── server.py
│   │   └── fixtures/
│   │       ├── scenario1/{ticket.json, logs.json, metrics.json, k8s.json, topology.json, expected.json}   # 日志含 trace_id，无需单独 trace.json
│   │       └── scenario2/{... 同上 ...}
│   └── assertions/                   # pytest 断言（L1/L2/L3 验收）
│       ├── test_scenario1.py
│       └── test_scenario2.py
```

```bash
make up          # 阶段0-2：起集群 + 数据源 + 正常版服务（基线验证）
make scenario1   # 注入场景1故障 → 跑 workflow → 断言
make scenario2   # 开启 warranty fin 缺参 bug 开关 → 跑 workflow → 断言
make mock        # 起 mock 数据源，跑 L1/L2（不依赖真实集群）
make down        # 销毁
```

### 6.4 被测服务「正常/故障」版本矩阵（核心，受控故障注入）

| 场景 | 服务 | bug 开关（env） | 与正常行为的差异（注入点） |
|---|---|---|---|
| 场景1 | order-service | `QUOTATION_TEMP_LEAK=true` | ① `generateFile()` 临时文件异常路径不清理；② deployment 设小 CPU limit（500m）+ emptyDir tmpfs（512Mi） |
| 场景2 | order-service | （无，配置缺陷常驻） | Feign 未配 readTimeout（修复 key：`spring.cloud.openfeign.client.config.default.read-timeout`） |
| 场景2 | warranty-service | `WARRANTY_MISSING_FIN=true` | ① 漏传 fin；② 空 catch 吞异常 |

> **无 bug 分支**：bug 代码常驻 `main`，由 env 开关控制是否生效。AIOps 的 fix 是改 `main` 上的真实代码（提交到 main），发布也从 `main` 构建镜像。开关只在「复现故障」时打开，修复后该 bug 路径从代码中移除。

**缩小资源 = 缩小触发阈值（场景1 关键技巧）**：CPU 100% 和磁盘 100% 的「触发速度」由两个旋钮决定——CPU limit 和 PVC 容量。缩得越小，100% 触发越快、越确定：

| 模式 | CPU limit | PVC | 触发速度 | 用途 |
|---|---|---|---|---|
| **快速触发（默认）** | `500m`（0.5 核） | `512Mi` | 秒级（`dd` 512Mi + `忙循环（while true，1 核）` 即满） | L1/L2 断言、日常迭代、CI |
| 逼真填满 | 无 limit | `10Gi` | 分钟级（需真实填满） | L3 最终验收（验证真实填满链路） |

缩资源反而更好，原因：
1. **指标信号更干净**：设了 limit 后「CPU 100%」= `usage/limit` 明确的阈值越界（throttled）；无 limit 时只能靠「吃满节点」判断，模糊且依赖节点核数。
2. **磁盘秒级写满**：512Mi 用 `dd` 或直接靠 temp 泄漏几秒填满，不用等 10Gi 写盘。
3. **数值写死、可复现**：适合 CI 断言，且换回 `v1` 秒级回滚。
4. **⚠️ 配额必须「起得来」（勿缩过头）**：500m/512Mi 是「相对多核节点的小」，不是「绝对小到起不来」——Spring Boot 启动是 CPU 密集（类扫描/bean 初始化），太小会 liveness 探针失败→重启死循环；512Mi 要能装下 JVM 堆 + 正常临时文件。**配额分离（启动预算 vs 故障阈值）** 与 **日志走 stdout 与故障盘解耦** 见 §6.8。注入前先跑一次基线确认正常。

### 6.5 分阶段搭建步骤

#### 阶段 0：K8s 集群（minikube + podman driver）

```bash
# 前置：确认 podman 可用（macOS 下先起 podman machine）
podman machine start
podman info                 # 确认 podman 就绪

# 起 minikube（podman driver；单节点够用，测试床无需多节点）
minikube start --driver=podman --container-runtime=cri-o
minikube status             # 确认 Running

# 开 addons：metrics-server（HPA 需用）
minikube addons enable metrics-server
kubectl get nodes           # minikube 已配好 kubeconfig 上下文
```

```bash
# testbed/minikube/start.sh 内容（即上面命令的固化）
#!/usr/bin/env bash
set -euo pipefail
podman machine start || true
minikube start --driver=podman --container-runtime=cri-o
minikube addons enable metrics-server
kubectl get nodes
```

> 说明与一致性：
> - **与沙箱同工具链**：opensandbox 已用 podman（DESIGN.md spike 验证），此处 K8s 集群也用 podman driver，本地只需维护 podman 一套容器运行时。
> - **PVC 免配置**：minikube 自带 hostpath provisioner 的 `standard` StorageClass，场景 1 的 PVC 直接可用。
> - **podman driver 若在 macOS 有兼容问题**，回退方案：`minikube start --driver=docker`，并把 `DOCKER_HOST` 指向 podman 的 docker 兼容 socket（`unix:///var/run/docker.sock`，DESIGN.md spike 已验证 podman 会转发该 socket）。

#### 阶段 1：数据源（日志 / 指标 / 追踪）

```bash
kubectl apply -f testbed/manifests/elasticsearch/
kubectl apply -f testbed/manifests/prometheus/
```

关键配置见 6.6（Logstash 5s JSON 采集、Prometheus Micrometer、traceId MDC）。

#### 阶段 2：被测服务部署（先正常版做基线）

```bash
# 场景1 基线：正常版 order-service
kubectl apply -f testbed/manifests/order-service/          # image: order-service:v1
# 场景2 基线：order-service + warranty-service + warranty-data-service（正常版）
kubectl apply -f testbed/manifests/warranty-service/       # image: warranty-service:v1
```

基线验证：调用报价单/结账 API 均返回 200（确认环境本身无误）。

#### 阶段 3：故障注入

```bash
# 场景1：换 bug 镜像（已带 500m CPU limit + 512Mi PVC）→ 秒级填满磁盘 + 打满 CPU
./testbed/fault-inject/scenario1.sh
# 脚本内容（小资源，秒级触发 100%）：
#   kubectl exec deploy/order-service -- sh -c 'mkdir -p /data/tmp && dd if=/dev/zero of=/data/tmp/fill bs=1M count=512'   # 512Mi tmpfs 秒级写满
#   kubectl exec deploy/order-service -- sh -c 'setsid sh -c "while true; do :; done" &'                                  # 忙循环打满 500m limit → 100% throttled

# 场景2：开 warranty fin 缺参 bug（+ order Feign 无超时配置）
./testbed/fault-inject/scenario2.sh
#   kubectl -n order set env deploy/warranty-service WARRANTY_MISSING_FIN=true
#   恢复：./scenario2-recover.sh（WARRANTY_MISSING_FIN=false）
```

#### 阶段 4：AIOps 平台接入

```bash
# config.py 里配数据源 MCP server 地址（真实）
#   ES:        http://elasticsearch:9200
#   Prometheus: http://prometheus:9090
#   （无 SkyWalking；trace 走 ES 按 traceId 关联）
#   K8s API:    ~/.kube/config（只读；infra-remediator 写操作走审批）
#   topology:   service_topology MCP（离线 job 聚合 trace + CMDB）
python -m agentflow run examples/order-service-quotation-print-fail.yaml \
    --trigger testbed/mock-datasource/fixtures/scenario1/ticket.json
```

#### 阶段 5：Mock 数据源 + fixtures（L1/L2，不依赖真实集群）

```bash
MOCK_FIXTURE_DIR=testbed/mock-datasource/fixtures \
    python testbed/mock-datasource/server.py   # 起 fastmcp stdio server
pytest testbed/assertions/ -v                   # 跑断言
```

Mock server 与真实数据源**工具签名一致**，agent 的 MCP 挂载不变，只把 MCP server 从真实换成 mock：

```python
# testbed/mock-datasource/server.py（fastmcp）
import json, os
from pathlib import Path
from fastmcp import FastMCP

mcp = FastMCP("mock-datasource")
FIXTURES = Path(os.environ.get("MOCK_FIXTURE_DIR", "testbed/mock-datasource/fixtures"))

def _load(scenario, name):
    return json.loads((FIXTURES / scenario / name).read_text())

@mcp.tool()
def query_logs(scenario: str, service: str = None, level: str = None, trace_id: str = None) -> dict:
    logs = _load(scenario, "logs.json")
    if service:  logs = [l for l in logs if l["service"] == service]
    if level:    logs = [l for l in logs if l["level"] == level]
    if trace_id: logs = [l for l in logs if l.get("trace_id") == trace_id]   # 按 traceId 关联调用链
    return {"logs": logs, "count": len(logs)}

@mcp.tool()
def query_metrics(scenario: str, metric: str = None) -> dict:
    return _load(scenario, "metrics.json")

@mcp.tool()
def describe_pod(scenario: str, pod: str = None) -> dict:
    return _load(scenario, "k8s.json")

@mcp.tool()
def service_topology(scenario: str, service: str = None) -> dict:
    return _load(scenario, "topology.json")
```

### 6.6 关键配置片段

**Logstash（5s JSON 采集）**：

```ruby
# logstash.conf
input  { beats { port => 5044 } }
filter { json { source => "message" } }
output {
  elasticsearch {
    hosts => ["http://elasticsearch:9200"]
    index => "%{service}-%{+YYYY.MM.dd}"
  }
}
```

**Prometheus（抓 K8s pod + JVM）**：

```yaml
scrape_configs:
- job_name: 'k8s-pods'
  kubernetes_sd_configs: [ { role: pod } ]
  relabel_configs: [ ... ]
- job_name: 'app-metrics'
  metrics_path: /actuator/prometheus   # Micrometer 端点（Boot 3.3 原生，无需 jmx-exporter）
  kubernetes_sd_configs: [ { role: pod } ]
  relabel_configs: [ { source_labels: [__meta_kubernetes_pod_label_app], target_label: service } ]
```

**traceId 传递（替代 SkyWalking，见 §6.11）**：gateway 入口经 MDC 设置 traceId，加 `X-Trace-Id` header 转发；下游服务 `TraceFilter` 读 header 写 MDC，日志带 `trace_id` 字段。

### 6.7 就绪检查清单（搭建完成自检）

| # | 检查项 | 命令/方式 | 通过标准 |
|---|---|---|---|
| 1 | 集群 ready | `kubectl get nodes` | 所有节点 Ready |
| 2 | 数据源就绪 | `kubectl get pods` | ES/Prometheus 全部 Running |
| 3 | 日志可查 | Kibana 或 `curl ES/_search` | 能查到 order-service 的 JSON 日志 |
| 4 | 指标可查 | `curl prometheus/api/v1/query` | 能查到 `container_cpu_usage` |
| 5 | 追踪可查 | ES 按 traceId 查日志 | 同一 traceId 串起 gateway→order→warranty |
| 6 | 基线业务正常 | `curl order-service/.../quotation` + `/checkout` | 均 200 |
| 7 | 故障注入生效 | 注入后查指标/K8s | CPU/磁盘 100%（场景1）；trace 见 error span（场景2） |
| 8 | AIOps 跑通 | `agentflow run ...` | 从 ticket 跑到 postmortem，Langfuse 有完整 trace |
| 9 | 审批生效 | committer/infra-remediator | 写操作被 `ask`，人工确认后落地 |
| 10 | 修复恢复 | 修复后重跑基线 | 报价单可下载 / 结账成功 |

---

### 6.8 被测服务自身设计（资源配额 / 日志管线 / 触发前端 / 日志生成）

#### 6.8.1 资源配额：启动预算 vs 故障阈值分离

回答「单个 pod 资源太小会不会影响启动和日志收集」——**会**。若把 200m/100Mi 当固定配额，Spring Boot 启动（类扫描/bean 初始化是 CPU 密集）+ JVM 堆 + 日志文件会吃不消：liveness 探针超时→重启死循环，或日志写不进去。所以配额拆成「两个目标、一套清单」：

| 目标 | 设计 |
|---|---|
| **能正常启动/运行** | CPU limit **500m**（0.5 核，够启动）；PVC **512Mi**（够 JVM + 正常临时文件）；配 **startupProbe** 给足启动窗口 |
| **秒级触发 100%** | `忙循环（while true，1 核）`（1 核 > 500m limit）→ 秒级 throttled 100%；`dd`/temp 泄漏写满 512Mi → 秒级磁盘 100% |

```yaml
# 关键：startupProbe 与 liveness 分离，避免启动期被误杀
livenessProbe:
  httpGet: { path: /health, port: 8080 }
  initialDelaySeconds: 30
  periodSeconds: 10
startupProbe:
  httpGet: { path: /health, port: 8080 }
  failureThreshold: 30          # 最多 30×10s = 5min 启动窗口
  periodSeconds: 10
```

结论：**「小」是相对节点多核而言的小（500m 仍小于 1 核），不是绝对小到起不来。**

#### 6.8.2 日志管线：日志走 stdout，与故障盘解耦

「资源太小影响 log 收集」的真正根因是**日志写文件且与临时文件同盘**——盘满后日志写不进、Logstash 也读不到。解法：**日志与数据盘分离**。

```
Spring Boot 应用（logback + logstash-encoder，输出 JSON 到 stdout）
  ├─ stdout ──► K8s 容器 stdout ──► 节点文件
  │                                    └─► Filebeat DaemonSet ──► Logstash(5s flush) ──► ES
  │                                        （日志链路，盘满也不受影响）
  └─ /data（PVC 512Mi，只放报价单临时文件）
       └─► 临时文件泄漏写满 ──► 磁盘 100% ──► 报价单无法生成
          （故障盘，与日志隔离）
```

- **日志永不受磁盘满影响**：盘满的是 PVC（/data 临时文件），日志走 stdout 落在节点盘，AI 定位时仍能读到 `IOException` 日志。
- **「logback 无滚动」从 bug 清单移除**：日志走 stdout，不写本地日志文件，滚动与否无关；磁盘满的唯一根因 = 临时文件泄漏（更聚焦）。
- **JSON 日志**：logback 配 `net.logstash.logback.encoder.LogstashEncoder`，输出与 ES 直接匹配的 JSON（含 service/pod/trace_id）。

#### 6.8.3 触发前端（test-ui）

一个 nginx 托管的静态页，按钮直调后端接口，人工/半自动触发：

```html
<!-- test-ui/index.html -->
<h3>订单服务测试面板</h3>
<button onclick="call('/quotation?orderId=ORD001')">打印报价单（场景1）</button>
<button onclick="call('/checkout?orderId=ORD001')">结账（场景2）</button>
<hr>
<label>速率/s <input id="rate" value="10"/></label>
<label>级别 <select id="level"><option>INFO</option><option>WARN</option><option>ERROR</option></select></label>
<button onclick="call('/test/logs/start?rate='+rate.value+'&level='+level.value)">启动日志生成</button>
<button onclick="call('/test/logs/stop')">停止日志生成</button>
<button onclick="call('/test/leak?count=200&size=1MB')">启动临时文件泄漏（填盘）</button>
<pre id="out"></pre>
<script>async function call(u){out.textContent=await (await fetch(u)).text()}</script>
```

#### 6.8.4 后端接口清单 + 日志生成器

| 服务 | 接口 | 用途 |
|---|---|---|
| order-service | `GET /quotation?orderId=` | 生成报价单（写 temp 文件→下载）【场景1 入口】 |
| order-service | `POST /checkout?orderId=` | 结账，调 warranty【场景2 入口】 |
| order-service | `GET /health` | 健康检查（探针用） |
| order-service | `POST /test/logs/start?rate=&level=` | 启动日志生成器（稳态日志流） |
| order-service | `POST /test/logs/stop` | 停止日志生成器 |
| order-service | `GET /test/logs/stats` | 查询日志生成统计（累计条数） |
| order-service | `POST /test/leak?count=&size=` | 触发临时文件泄漏（写 count×size 填盘） |
| warranty-service | `POST /checkWarranty?orderId=` | 三包期查询【场景2 bug 点】 |
| warranty-service | `POST /test/logs/start\|stop` | 同上日志生成器 |

日志生成器（每个服务内置一个，模拟「日志不断产生」，让 ES 持续有数据供 log-analyst 查询）：

```java
@Component
public class LogGenerator {
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final AtomicLong count = new AtomicLong();
    private ScheduledExecutorService exec;
    private static final Logger log = LoggerFactory.getLogger(LogGenerator.class);

    public void start(int rate, String level) {
        if (!running.compareAndSet(false, true)) return;
        exec = Executors.newSingleThreadScheduledExecutor();
        long interval = 1000 / Math.max(1, rate);          // rate 条/秒
        exec.scheduleAtFixedRate(() -> emit(level), 0, interval, TimeUnit.MILLISECONDS);
    }

    private void emit(String level) {
        long n = count.incrementAndGet();
        switch (level) {
            case "INFO"  -> log.info("收到报价单生成请求 orderId={} fin={}", "ORD" + (1000 + n % 9000), randFin());
            case "WARN"  -> log.warn("三包期查询耗时偏高 {}ms orderId={}", 2000 + n % 3000, "ORD" + n);
            case "ERROR" -> log.error("报价单生成失败: java.io.IOException: No space left on device", new IOException("No space left on device"));
        }
    }

    public void stop() { if (running.getAndSet(false)) exec.shutdownNow(); }
    public long getCount() { return count.get(); }
    private String randFin() { return "VIN" + String.format("%09d", count.get()); }
}
```

> 日志生成器的两层用途：① **稳态日志流**（INFO/WARN）模拟正常业务，让 ES「活」着、log-analyst 有数据可查；② **故障日志**由真实触发 bug 接口产生（`/quotation` 真写盘失败 → 真 `IOException`），比生成器伪造的 ERROR 更可信。

#### 6.8.5 触发→证据的数据流

```
test-ui 按钮 ──► /quotation 或 /checkout
                  │
                  ├─► 应用打日志 ──► stdout ──► Filebeat → Logstash → ES   （log-analyst 查）
                  ├─► 日志带 traceId ──► ES（按 traceId 关联）              （trace-analyst 查）
                  ├─► 资源占用 ──► Prometheus（Actuator/kubelet）          （metrics-analyst 查）
                  └─► 文件写 /data ──► PVC 满 / pod throttled             （infra-locator 查）
```

---

### 6.9 资源监控采集链路（metrics-analyst 数据源）

#### 采集架构（Prometheus pull 模型）

```
被监控对象（各 exporter 暴露 /metrics 端点）
  ├─ Node（节点）           → node-exporter（DaemonSet）      节点 CPU/磁盘/内存
  ├─ 容器 cgroup 指标        → kubelet 内置 cAdvisor            每容器 CPU/内存/磁盘
  ├─ K8s 对象状态           → kube-state-metrics（Deployment） pod/PVC/limit/restart
  └─ JVM 内部指标           → Micrometer（Actuator /actuator/prometheus）  GC/堆/线程
        │  scrape（Prometheus 每 15s 拉取）
        ▼
  Prometheus（TSDB 时间序列存储）
        │  PromQL 查询（HTTP API）
        ├─► Grafana（人工看图）
        └─► MCP 工具 query_metrics ──► metrics-analyst ──► MetricsEvidence
```

> 注意与日志区分：**日志 5s 同步是 Logstash 的**（push/采集）；**指标是 Prometheus 每 15s 主动拉取**（pull）。两条链路独立，5s 日志快、15s 指标稍慢，但「秒级触发 100%」后 15s 内指标就能反映。

#### 四类 exporter 分工

| exporter | 部署方式 | 提供什么 | 对应场景证据 |
|---|---|---|---|
| **kubelet/cAdvisor** | K8s 节点自带 | 每容器 CPU/内存/磁盘（cgroup 层） | **CPU 100%**（usage vs limit）、**磁盘 100%**（fs 用量） |
| **kube-state-metrics** | Deployment | K8s 对象状态（pod phase/PVC/restart/limits） | pod 状态、restart 计数、PVC 容量 |
| **node-exporter** | DaemonSet | 节点级 CPU/磁盘/内存 | 排除「节点级问题」（负证据） |
| **Micrometer（Actuator）** | Boot 原生 `/actuator/prometheus` | JVM 内部（GC 暂停/堆/线程） | **GC 频繁**（CPU 100% 的佐证） |

#### 场景1 关键指标 + PromQL

```promql
# ── CPU（容器）──
# 使用率（核/秒）
rate(container_cpu_usage_seconds_total{container="order-service",namespace="order"}[5m])
# 限制（核；500m 的 limit → quota=50000us / period=100000us = 0.5 核）
container_spec_cpu_quota{container="order-service"} / container_spec_cpu_period{container="order-service"}
# 利用率 %（核心判断：>=100% 即打满）
100 * rate(container_cpu_usage_seconds_total{container="order-service"}[5m])
      / (container_spec_cpu_quota{container="order-service"} / container_spec_cpu_period{container="order-service"})
# 节流（throttled 秒数，limit 打满的直接证据）
rate(container_cpu_cfs_throttled_seconds_total{container="order-service"}[5m])

# ── 磁盘（PVC /data）──
# PVC 用量 / 容量（kubelet 直采，比 container_fs_* 更精确对应 PVC）
kubelet_volume_stats_used_bytes{persistentvolumeclaim="data",namespace="order"}
kubelet_volume_stats_capacity_bytes{persistentvolumeclaim="data",namespace="order"}
# 磁盘利用率 %
100 * kubelet_volume_stats_used_bytes{...} / kubelet_volume_stats_capacity_bytes{...}

# ── 内存 ──
container_memory_working_set_bytes{container="order-service",namespace="order"}

# ── GC（JVM，经 Micrometer）──
rate(jvm_gc_pause_seconds_sum{service="order-service"}[5m])
```

#### 场景2 的负证据

场景2 无资源问题，metrics-analyst 跑同一套 PromQL，结果是「正常」——这**不是浪费，而是排除法证据**：root-cause 能据此明确「排除基础设施因素」，把结论锚定到代码 bug。

#### metrics-analyst 如何消费

1. MCP 工具 `query_metrics(promql)` 调 Prometheus HTTP API（`/api/v1/query`）。
2. metrics-analyst 按 system prompt 里的预设 PromQL 逐项查：CPU 利用率%、磁盘利用率%、内存、GC、throttled。
3. 判断规则：`利用率% >= 阈值（如 95%）` 或 `throttled 速率显著 > 0` → 判定「资源打满」。
4. 产出 `MetricsEvidence`（含资源时序 + 阈值越界时间点 + `collected_at`/`ttl`），供 root-cause 汇总。

#### 采集参数

| 参数 | 值 | 说明 |
|---|---|---|
| scrape_interval | 15s | Prometheus 拉取频率 |
| 指标保留 | 30d（本地测试可缩到 7d） | TSDB retention |
| Micrometer 端点 | `/actuator/prometheus` | Boot 3.3 原生，无需额外 javaagent |
| 与日志的关系 | 独立 | 日志走 Logstash 5s，指标走 Prometheus 15s，互不影响 |

---

#### 6.9.6 统一 service 标签（「按服务看」的前提）

cAdvisor 指标只带 `container/pod/namespace`，**没有 `service` 标签**——这是「按服务看资源」的最大坑。解法：Prometheus 自动发现时用 relabeling 把 pod 的 `app` 标签映射成统一 `service` 标签，让所有指标都能按服务过滤/聚合。

```yaml
# prometheus.yml：应用资源 job，自动发现 pod + 打 service 标签
scrape_configs:
- job_name: 'app-resources'
  kubernetes_sd_configs:
  - role: pod
  relabel_configs:
  - source_labels: [__meta_kubernetes_pod_label_app]   # pod 须带 app=order-service
    target_label: service
  - source_labels: [__meta_kubernetes_namespace]
    target_label: namespace
  - source_labels: [__meta_kubernetes_pod_name]
    target_label: pod
  - source_labels: [__meta_kubernetes_pod_container_name]
    target_label: container
```

> 前提：Deployment 的 pod 规范里带 `app: order-service`（k8s 约定俗成，selector 本来就带）。打完后，`container_cpu_usage_seconds_total{service="order-service"}` 之类查询即可按服务过滤。

#### 6.9.7 Grafana 展示（准实时）

**① 数据源**（Grafana provisioning 自动加载）：

```yaml
# grafana/provisioning/datasources/prometheus.yaml
datasources:
- name: Prometheus
  type: prometheus
  url: http://prometheus:9090
  isDefault: true
  access: proxy
```

**② 模板变量 `$service`**（一个仪表盘覆盖所有服务，切换下拉即看某服务）：

```promql
label_values(container_cpu_usage_seconds_total, service)
```

**③ 仪表盘面板**（均按 `$service` 过滤；注意排除 `container="POD"` 这个 pause 沙箱容器）：

| 面板 | PromQL | 类型 |
|---|---|---|
| CPU 使用（核） | `rate(container_cpu_usage_seconds_total{service=~"$service",container!="POD"}[5m])` | Graph |
| CPU 利用率 % | `100 * rate(container_cpu_usage_seconds_total{service=~"$service"}[5m]) / on(container,pod) (container_spec_cpu_quota/container_spec_cpu_period)` | Gauge |
| 内存使用 | `container_memory_working_set_bytes{service=~"$service",container!="POD"}` | Graph |
| 内存利用率 % | `100 * container_memory_working_set_bytes / on(container,pod) container_spec_memory_limit_bytes` | Gauge |
| 磁盘（PVC）用量 | `kubelet_volume_stats_used_bytes{persistentvolumeclaim="data"}` | Graph |
| 磁盘利用率 % | `100 * kubelet_volume_stats_used_bytes{pvc="data"} / kubelet_volume_stats_capacity_bytes{pvc="data"}` | Gauge |
| 重启次数 | `kube_pod_container_status_restarts_total{service=~"$service"}` | Stat |
| GC 暂停 | `rate(jvm_gc_pause_seconds_sum{service=~"$service"}[5m])` | Graph |

**④ 准实时调参**：

| 参数 | 默认 | 准实时 | 说明 |
|---|---|---|---|
| Prometheus `scrape_interval` | 15s | **5s** | 测试床小，5s 足够且负载可忽略 |
| Grafana 面板 `refresh` | 关 | **5s** | 仪表盘自动刷新 |
| 查询模式 | range | **instant** | 看当前值用 instant，看趋势用 range |

> 注意：磁盘利用率 % 用 `kubelet_volume_stats_*`（按 PVC 精确）比 `container_fs_*`（按文件系统）更贴近「/data 这块盘」，二者在只有一个 PVC 时等价，多 PVC 时要靠 `persistentvolumeclaim` 标签区分。

---

### 6.10 代码仓库（GitHub 私有仓库，全 main + env 开关）

**三个私有仓库**（gateway/order/warranty；order/warranty 已建，gateway 待建）：

| 仓库 | 服务 | 场景 |
|---|---|---|
| `xqfgbc/aiops-test-gateway-service` | 网关（traceId 入口） | 统一入口，路由到 order |
| `xqfgbc/aiops-test-order-service` | 订单服务 | 场景1 被测 + 场景2 结账调用方 |
| `xqfgbc/aiops-test-warranty-service` | 保养服务 | 场景2 bug 点（fin 缺参） |

**分支策略（无 bug 分支，全部在 main）**：

- 所有开发、bug、修复、发布都在 `main`，**不建 `bug/*` 分支**。
- bug 代码常驻 `main`，由 **env 开关**控制是否生效（见 §6.4 版本矩阵）。
- AIOps 的 fix 直接提交到 `main`；发布（镜像）也从 `main` 构建。

**技术栈（本地 Java 21 决定）**：

| 项 | 选择 | 理由 |
|---|---|---|
| Java | **21**（LTS） | 本地默认（Homebrew 21.0.11），Boot 3.x 原生支持 |
| Spring Boot | **3.3.x** | 支持 Java 21，成熟稳定（Jakarta EE） |
| Spring Cloud | **2023.0.x（Leyton）** | 对应 Boot 3.2/3.3 的 release train |
| 构建工具 | **Gradle + gradlew wrapper（8.10+）** | 本地未装 Gradle，wrapper 免全局安装；Spring Boot 3.3.x 要求 Gradle ≥ 8.4 |
| 跨服务调用 | **OpenFeign**（order→warranty） | 场景2 需要；两服务中唯一真正用到的 Spring Cloud 组件 |
| 服务发现 | **K8s DNS**（不引 Eureka/Nacos） | Feign 用 `http://warranty-service:8080`，测试床简化 |
| 镜像基础 | `eclipse-temurin:21-jre`（运行） | OpenJDK 21 发行版（官方 openjdk 镜像已下架） |

> ⚠️ 注：Docker Hub 官方 `openjdk` 镜像已**下架**（manifest unknown，实测无法拉取），改用同为 OpenJDK 21 发行版、持续维护的 `eclipse-temurin:21-jre`。

**committer 认证**：AIOps 的 committer 用 **fine-grained PAT**（只授权这三个仓库，权限 read/write contents + pull requests），走环境变量 `GITHUB_TOKEN` 注入，不进 YAML。

**CMDB service→repo 映射**（供 code-locator 用，即 §七 P0-②）：

```json
{
  "gateway-service":  { "repo": "https://github.com/xqfgbc/aiops-test-gateway-service",  "owner": "platform-team", "env": "test" },
  "order-service":    { "repo": "https://github.com/xqfgbc/aiops-test-order-service",    "owner": "order-team",    "env": "test" },
  "warranty-service": { "repo": "https://github.com/xqfgbc/aiops-test-warranty-service", "owner": "warranty-team", "env": "test" }
}
```

---

### 6.11 链路追踪：gateway + MDC traceId（替代 SkyWalking）

**为什么不用 SkyWalking**：gateway 已在入口统一设置 traceId，下游服务一路携带，日志里都带 traceId 进 ES——trace-analyst 直接按 traceId 关联 ES 日志即可重建调用链，无需专门的 APM（少一个 OAP/javaagent/UI 组件，少一个故障点）。

**traceId 传递链路**：

```
请求 → gateway-service（GlobalFilter：无 X-Trace-Id 则生成 → 写 MDC → 加 header 转发）
        → order-service（TraceFilter：读 X-Trace-Id → 写 MDC；Feign RequestInterceptor 把 header 传给下游）
          → warranty-service（TraceFilter：读 X-Trace-Id → 写 MDC）
        （各服务日志均带 trace_id 字段 → Logstash → ES）
```

**各服务需实现的 3 个小件**：

| 服务 | 组件 | 作用 |
|---|---|---|
| gateway | `GlobalFilter`（WebFlux） | 读/生成 traceId → 写 MDC（reactor context）→ 加 `X-Trace-Id` header 转发 |
| order/warranty | `TraceFilter`（Servlet） | 读 `X-Trace-Id` → 写 MDC，日志自动带 traceId |
| order | Feign `RequestInterceptor` | 把当前 traceId 加到出站 header，跨服务保持同一 traceId |

**trace-analyst 数据源变化**：从「SkyWalking trace 查询」改为「**ES 日志按 traceId 关联查询**」（`query_logs(trace_id=...)`），产出 `TraceEvidence`（按 traceId 串起的服务调用序列 + 哪个服务报了 ERROR）。

**logback 输出带 traceId**：JSON encoder 加 `trace_id` 字段（`%X{traceId}`），与 ES 索引直接对应。

---

## 七、为支持端到端自动排错，测试床还需补齐的能力（gap 分析）

> 逐条对照 AIOps 闭环的每个「外部触点」，检查测试床是否已有对应组件。✅ = 已有，⚠️ = 有部分/可 mock 顶着，❌ = 缺失。

| # | AIOps 触点 | 测试床现状 | 缺口 | 优先级 |
|---|---|---|---|---|
| 1 | 触发源（ticket/告警）→ triage | ✅ 手动 `--trigger bug.json`；⚠️ 已加 Prometheus 告警规则 | ❌ **告警→Alertmanager→自动投递 ticket** 未打通 | **P0** |
| 2 | 代码定位 → code-locator | ✅ GitHub 仓库 | ❌ **CMDB（service→repo 映射）** 未建，跨服务定位不到仓库 | **P0** |
| 3 | 提交 → committer（push/PR） | ✅ 真实 GitHub 私有仓库（§6.10） | ⚠️ 需注入 committer 的 fine-grained PAT | **P0** |
| 4 | 审批 → human-in-the-loop | ❌ 无 | ❌ **审批模拟器**（自动过审/驳回），否则「自动排错」卡在人工 | **P0** |
| 5 | 端到端编排 | ⚠️ make 脚本半自动 | ❌ **一键 runner**（注入→等告警→投 ticket→跑 AIOps→断言恢复） | **P0** |
| 6 | 知识检索 → knowledge-lookup | ❌ 无数据源 | ❌ **历史 bug 知识库**（kg-qa 暂不实现，需轻量替代） | P1 |
| 7 | 拓扑/影响面 → root-cause/fix-planner | ⚠️ mock fixture 的 topology.json | ⚠️ **服务拓扑服务**（get_dependents），可先用 mock 顶着 | P1 |
| 8 | 修复/验证 → fix-implementer/tester | ⚠️ opensandbox(podman) 已就绪 | ❌ **Java+Maven/Gradle 沙箱镜像**，否则跑不了 Java 单测 | P1 |
| 9 | 复盘 → postmortem | ❌ 无写入目标 | ⚠️ KG 写入目标（可先写文件） | P2 |

### P0 缺口（不补则「自动排错」跑不通）

**① 自动触发链路（告警 → AIOps）**：现在要人工投 `bug.json`，不是「自动」。补齐：
```
Prometheus 告警规则（已加 rules/order-service.yml）
  → Alertmanager（需部署）
    → webhook → agentflow REST API（POST /run {ticket}）
```
建议落地：`testbed/manifests/alertmanager/alertmanager.yml`（webhook receiver）+ agentflow 加一个 `POST /run` 入口（替代 CLI `--trigger`）。

**② CMDB（service→repo 映射）**：code-locator 靠它跨服务找仓库。最简实现：一个 `cmdb.json` + 只读 MCP 工具 `get_ci(service) -> {repo, owner, env}`。
建议落地：`testbed/mock-datasource/fixtures/cmdb.json` + MCP 工具 `get_ci`。

**③ Git 仓库（✅ 已建，用真实 GitHub 私有仓库）**：committer 要真实 push/PR，不能只 dry-run。已建两个私有仓库（见 §6.10），committer 直接对真实 GitHub push/PR——比自建 Gitea 省事，且 PR 流程真实。
剩余待办：给 committer 注入 **fine-grained PAT**（只授权这两个仓库，走环境变量，见 DESIGN.md 凭据约定）。

**④ 审批模拟器**：`permission.asked` 事件需要响应。测试床提供 `AUTO_APPROVE=1` 开关（自动通过写操作审批），或一个 mock 审批端点。
建议落地：agentflow config 加 `approval.mode: auto|manual`。

**⑤ 一键 runner**：把「注入故障→等告警→（自动）投 ticket→跑 AIOps→断言服务恢复→生成报告」串成一个脚本。
建议落地：`testbed/Makefile` 的 `make scenario1`/`make scenario2` 目标 + `testbed/run_scenario.sh`。

### P1 缺口（有 mock 可顶，但补了更完整）

- **历史 bug 知识库**：knowledge-lookup 需要可召回的相似案例。最简：`fixtures/knowledge.json`（几条已知根因→修复模式），MCP 工具 `search_knowledge(q)` 做简单关键词匹配。真正的向量检索留到接 kg-qa 时。
- **服务拓扑服务**：root-cause/fix-planner 用 `get_dependents` 圈影响面。可先用 `fixtures/topology.json` 顶，M3 再上离线聚合 trace 的真拓扑。
- **Java 沙箱镜像**：opensandbox 的 code-interpreter 镜像要带 JDK + Maven/Gradle，否则 fix-implementer/tester 跑不了 order-service 的单测。需自建 `testbed/docker/sandbox-java.Dockerfile`。

### 结论

**五件 P0 补齐后，「故障注入 → 自动告警 → 自动投 ticket → AIOps 诊断 → 修复 → 审批(模拟) → 提交(真实 PR) → 验证恢复」这条自动排错闭环就能在测试床里端到端跑通。**

---

## 八、端到端验证视角的补充 gap 分析（评审采纳）

> 从「端到端跑通闭环」视角补充。标注：✅ 采纳（已回写正文/落地待办）、❌ 因 SkyWalking 移除已失效、⚠️ 已定 eclipse-temurin:21（openjdk 镜像已下架）。对应正文修正已回写（Micrometer §6.9、Feign key §6.4/§3.6）。

### P0（不加则闭环跑不稳/跑不通）

| # | 补充项 | 状态 | 落地 |
|---|---|---|---|
| 1 | 故障 teardown（清 PVC + 杀 stress + 重启 pod） | ✅ | `fault-inject/scenarioN_teardown.sh` |
| 2 | 证据可用性等待（投 ticket 前 sleep 15s / 轮询 ES） | ✅ | `run_scenario.sh` |
| 3 | Git read/write PAT 分层（fix 只读、commit 写） | ✅ | 环境变量双 PAT |
| 4 | 沙箱 egress 放通（GitHub/K8s/MCP/LLM） | ✅ | 回写 DESIGN.md §4.6 |
| 5 | K8s RBAC serviceaccount（deployments/scale+pvc+pods/exec） | ✅ | `manifests/rbac.yaml` |
| 6 | Micrometer 替代 jmx-exporter | ✅ | 回写 §6.9 + prometheus.yml |
| 7 | OpenFeign 新配置 key（`read-timeout` kebab-case） | ✅ | 回写 §6.4/§3.6 |
| 8 | traceId 跨服务传播校验（gateway→order→warranty→ES 同一 traceId） | ✅ 改 | 替代原「SkyWalking traceId 关联校验」 |
| 9 | SkyWalking × Boot3 × Java21 兼容性预验证 | ❌ 失效 | 已移除 SkyWalking |

### P1

| # | 补充项 | 状态 |
|---|---|---|
| 1 | mock-llm（L1/L2 免真实 LLM API，快+免费+确定） | ✅ |
| 2 | 负证据 query_status（ok/empty/error） | ✅ 回写 DESIGN.md §六 |
| 3 | 止血后复发测试（只止血不根治，验证 PVC 不重新写满） | ✅ |
| 4 | 审批驳回路径 + 并发审批 | ✅ 回写 DESIGN.md §4.7 |
| 5 | 断点续跑测试用例（kill + resume + 验证幂等） | ✅ `assertions/test_resume.py` |
| 6 | --repeat N 通过率（≥80% 而非单次 pass/fail） | ✅ |
| 7 | 冷启动耗时实测（确认 startupProbe 阈值够） | ✅ |
| 8 | ES refresh_interval=1s 显式配置 | ✅ |
| 9 | tester 两层验证（sandbox_tests + integration_tests） | ✅ |
| 10 | 双层修复分别验证（止血立即 200 + 根治跑 10 次不复发） | ✅ |

### P2

| # | 补充项 | 状态 |
|---|---|---|
| 1 | 组件版本锁定（ES/Prometheus 具体版本） | ✅ |
| 2 | Langfuse 轻量部署 / 最低资源声明（依赖 PG+ClickHouse） | ✅ |
| 3 | 多节点 kind（L3 最终验收，≥2 worker） | ✅ |
| 4 | PVC 扩容局限（minikube hostpath 不可扩，云上验扩容） | ✅ |
| 5 | 时间戳对齐 NTP + fixture 显式 evidence_window | ✅ |
| 6 | 沙箱镜像 eclipse-temurin:21（含 git + ca-certificates + gradle 缓存） | ⚠️ 已定 eclipse-temurin:21 |

> **核心结论**：评审补齐了三类此前文档没覆盖、且会让闭环跑不通/跑不稳的东西——① 故障注入生命周期管理（teardown/幂等/时序等待）、② 修复侧真实性（Git 凭据分层/沙箱 egress/K8s RBAC）、③ 验证真实性（双层修复分别验/跨服务回归沙箱局限/负证据 query_status）。其中 Micrometer（§6.9）和 Feign 新配置 key（§6.4）是两处真正的技术选型修正，已回写正文。

---

## 九、场景带来的架构增量（回填 DESIGN.md）

1. **新增 3 个基础设施 agent**：`metrics-analyst` / `infra-locator` / `infra-remediator`（现有编队是「代码 bug 导向」，缺了会把基础设施故障误判为纯代码问题）。
2. **数据源 MCP 层扩展**：现有「日志/链路/CMDB」需补 **Prometheus 指标 + K8s 只读/写 + 服务拓扑（service_topology）** 三类 MCP。
3. **多仓库定位**：`inputs.repo` 从单值升级为映射 `{service: repo}`，`code-locator` 由 trace 的 service 名 + CMDB 映射跨仓库定位。
4. **负证据显性化**：`root-cause` schema 增加 `ruled_out` 字段（已排除假设及依据），而非只列正向假设。
5. **症状分类**：`BugReport` 增加 `symptom_type: crash | hang | slow | wrong_output`，驱动诊断侧重点（场景 1 是 crash/报错类，场景 2 是 hang/挂起类）。
6. **服务关联关系**：作为预计算的数据源 MCP（`service_topology`），支撑多仓库定位、故障传播分析、影响面评估（见第四节）。

---

## 十、实测踩坑记录（搭建测试床时实际遇到的问题与解决）

> 记录从零搭建测试床（minikube + podman + 3 服务 + ES/Prometheus/Grafana/Filebeat）过程中**实际踩到的坑**，供后续复现和排障参考。

### 集群与镜像

| # | 问题 | 现象 | 根因 | 解决 |
|---|---|---|---|---|
| 1 | minikube podman driver 起不来 | `Error downloading kic artifacts: not yet implemented (issue #8426)` | podman driver 的 kicbase 镜像加载未实现 | 改 `--driver=docker` 走 podman 的 docker 兼容 socket |
| 2 | docker driver 版本校验失败 | `docker version < minimum (5.3.2 < 18.09.0)` | podman 把自身版本 5.3.2 当 docker 版本 | `minikube start --force` 绕过校验 |
| 3 | 本机无 docker CLI | `exec: "docker": executable file not found` | 只装了 podman，无 docker 客户端 | `brew install docker`（仅客户端，daemon 走 podman socket） |
| 4 | openjdk:21 镜像拉不下来 | `manifest unknown` | Docker Hub 官方 openjdk 镜像已**下架**（非仅停更） | 改用 `eclipse-temurin:21-jre` |
| 5 | 改代码后重部署不生效 | pod 仍用旧镜像 | `minikube image load --overwrite` 对同 tag 不覆盖 | 换新 tag（如 v2），或 `minikube image rm` |

### 服务代码

| # | 问题 | 现象 | 根因 | 解决 |
|---|---|---|---|---|
| 6 | order-service 起不来 | `Failed to bind spring.cloud.openfeign.client.config.default` | application.yml 留了空 `default:` 块 | 删空块（场景2 的 bug 本就是「不配 timeout」，配置应完全缺省） |
| 7 | 本地调试报价单 500 | `IOException: No such file or directory` | 本地无 `/data`（K8s 里才是 PVC 挂载） | 本地 `--quotation.temp-dir=/tmp/quotation` 覆盖 |

### 数据源

| # | 问题 | 现象 | 根因 | 解决 |
|---|---|---|---|---|
| 8 | Prometheus cadvisor 抓不到 | `403 Forbidden` | RBAC 缺 `nodes/proxy` 子资源权限 | 补 `nodes/proxy` |

### 日志采集（Filebeat）

| # | 问题 | 现象 | 根因 | 解决 |
|---|---|---|---|---|
| 9 | 事件被 ES 丢弃 | Filebeat 刷屏 `Cannot index event (status=400)` | `decode_json_fields` 用 `target:""` 合并顶层，`@timestamp`(字符串) vs ECS(date)、`service`(字符串) vs ECS(object) 类型冲突 | 改用 `target:"app"` 命名空间 |
| 10 | 应用日志进不来/字段混乱 | ES 里只有系统日志 + Filebeat 自身日志 | 采集了所有容器日志，`app.service` 类型仍冲突 | 文件名 glob `*_order_order-service-*.log` 等只采 3 个应用容器 |

> 教训：① 本地容器工具链（podman）与 minikube 的 docker driver 兼容性差，需 `--force` + docker CLI 客户端；② 基础镜像要用持续维护的发行版（openjdk 已下架）；③ 配置文件里的空块是隐藏炸弹（Feign `default:`）；④ Filebeat 解析嵌套 JSON 要用 `target` 命名空间 + 按文件名过滤。

### 指标采集（Grafana 数据异常排查）

| # | 问题 | 现象 | 根因 | 解决 |
|---|---|---|---|---|
| 11 | CPU/内存指标 `container` 标签为空 | 按 `container` 过滤查不到数据 | minikube docker driver 的 cAdvisor 只暴露 pod 级指标（`container=""`），非容器级 | 改用 `pod=~"$service-.*"` 过滤（pod 名前缀） |
| 12 | `rate(...[5m])` 查不到数据 | Prometheus 重启后 5 分钟内 CPU 面板空 | `rate[5m]` 需 5 分钟窗口，重启后 TSDB 为空 | dashboard 用 `[2m]` 窗口；或等 5 分钟 |
| 13 | 磁盘 PVC 指标采集不到 | `kubelet_volume_stats_*` / `container_fs_*` 均无数据 | minikube hostPath PVC 不暴露 volume stats；cAdvisor fs 指标不覆盖 PVC | **场景1 的「磁盘 100%」无法靠 Prometheus 指标检测**，需依赖日志证据（`IOException: No space left on device`）+ infra-locator（`kubectl exec df -h /data`）；PVC 用量指标留到 L3 云环境（CSI PVC）验证 |

| 14 | hostPath PVC 不强制大小 | `df -h /data` 显示节点 46G，不是 512Mi | minikube hostPath provisioner 只把 PVC 大小当元数据，实际挂载节点文件系统 | 改用 `emptyDir` + `medium: Memory`（tmpfs）+ `sizeLimit: 512Mi`，有真实大小限制，填满报 `No space left on device` |
| 15 | 磁盘 gauge 值 NaN | `data_disk_*` 指标值是 NaN | `File.getTotalSpace()`/`getUsableSpace()` 用 `ToDoubleFunction` lambda 时返回 NaN | 改用 `Supplier<Number>`（`Gauge.builder(name, (Supplier<Number>) this::totalBytes)`），显式返回 `Number` |

> 关键结论（已解决）：**minikube 的 hostPath PVC 不强制大小**，场景 1 的「磁盘 100%」必须改用 **`emptyDir` tmpfs（512Mi）** 才能真实触发。磁盘用量用 **Micrometer 自定义 gauge**（`data_disk_total_bytes`/`data_disk_free_bytes`）暴露，CPU 百分比用 `container_spec_cpu_quota/period`（500m limit）计算。两者都能在 Grafana 直接看到，场景 1 的「CPU 100% + 磁盘 100%」可完整可视化判断。
