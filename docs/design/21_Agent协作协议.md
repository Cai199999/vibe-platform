# 21 · Agent 协作协议（Architect 中心制）

> 状态：设计冻结（2026-11-25）
> 适用范围：平台内所有生成/校验/改造类 Agent，以及生成应用内部的 Agent 编排边界

## 1. 目标

把 Agent 协作从当前偏网状的 A2A，收敛为 **Architect 中心制的层级制**：

- 用户只和 Architect 对话；
- Architect 负责需求理解、任务拆分、分派与裁决；
- 子 Agent 只执行、只汇报，不直接面向用户、不互相直连；
- 所有子 Agent 行为对 Architect 可见；
- 不清楚就澄清，不猜测；
- 高危操作明确拒绝或要求重新生成。

这不是提示词约定，而是**协议 + 状态机 + 台账**三层强制。

## 2. 非目标

- 不追求“全自主多 Agent 自由协作”。
- 不允许子 Agent 绕过 Architect 直接改用户可见状态。
- 不把安全、策略、迁移执行交给 LLM 判断。
- 不要求一次对话解决所有问题；允许多轮澄清。

## 3. 角色与职责

| 角色 | 职责 | 不做什么 |
|---|---|---|
| **Architect** | 唯一用户对话入口；需求翻译；任务拆分与分配；摘要/全文裁决；高危操作决策；场景追问 | 不直接写代码；不跳过门禁 |
| **Supervisor** | 运行时传输与执行调度；转发消息；维护任务状态 | 不替 Architect 做路由或风险决策 |
| **专业 Agent** | 在授权任务内执行；输出结构化摘要；遇到不确定主动澄清 | 不直接问用户；不调用其他子 Agent；不越权写 |
| **工具层** | 模板、AST、构建、迁移、打包、门禁等确定性能力 | 不做语义判断；不跳过记录 |
| **澄清桥** | 挂起当前 Agent、收集用户选项、恢复原进程 | 不重跑整个任务 |
| **风险门禁** | 一致性/安全/范围/成本/数据破坏判定 | 不给出“勉强通过” |

## 4. 拓扑与路由规则

```
用户 ↔ Architect
          ↕ 任务 / 摘要 / 澄清 / 裁决
      Supervisor（传输）
          ↕
  专业 Agent（Frontend / Backend / DBA / DevOps / Test / Brownfield ...）
          ↕
       工具层
```

强制规则：

1. 消息地址只允许 `architect` 或 `supervisor`；子 Agent 互相寻址直接拒绝。
2. 子 Agent 不得直接调用用户界面；需要用户输入时发 `clarification.requested` 给 Architect。
3. 每次写操作必须携带 `taskId + actor`，并写入动作台账；缺记录的工具调用被拒绝。
4. Architect 默认只读摘要；需要细节时按 `fullPayloadRef` 回取，受 token 预算约束。
5. Architect 可暂停、终止、重派任一子 Agent。

### 4.1 快速通道（标准链路直连）

在“标准路径 + 全程无异常”时，允许子 Agent 直连，减少不必要的往返；但直连不是失联。

授权条件：

- 由 Architect 预先发放 `FastPathGrant`，包含 `runId / taskId / 白名单边 / TTL / maxSteps`；
- 只允许标准链路白名单，例如 `DBACoder → BackendCoder` 的 `schema_ready`；
- 当前步骤零错误、零重试、零澄清、未被风险门禁拦截；
- 修复类、升级类、澄清类边一律不允许直连。

直连期间的可见性：

- 每条消息仍写入 Architect 收件箱，标记 `deliveryMode = fast_path`；
- 发送 `agent.report` 事件，Architect 能实时看到“做了什么、一切正常”，但不阻塞执行；
- 每一步计入 `maxSteps`，防止无限直连。

立即收回：

- 出现错误、重试、澄清、风险拦截、超时或超步数，立即收回授权；
- 发 `agent.fastpath.revoked` 事件，后续消息自动回落到 Architect 中转；
- 收回后不自动恢复，必须由 Architect 重新授权。

审计事件：`agent.fastpath.granted` / `agent.fastpath.revoked` / `agent.report(direct=true)`。

## 5. 消息协议

### 5.1 AgentReport（子 Agent → Architect）

```json
{
  "type": "agent.report",
  "agent": "BackendCoder",
  "taskId": "task_01",
  "runId": "run_01",
  "status": "done | blocked | needs_clarification | failed",
  "summary": "≤200 字，业务语言，给 Architect 看",
  "decisions": ["关键判断及依据"],
  "risks": [
    { "category": "consistency|security|scope|cost|data", "level": "low|high", "evidence": "..." }
  ],
  "needs": [
    { "type": "clarification|approval|resource", "question": "...", "options": [] }
  ],
  "artifacts": [{ "path": "...", "hash": "..." }],
  "fullPayloadRef": "artifact://...",
  "parentSpan": "trace/span"
}
```

- `summary` 必填；缺失视为非法输出，直接打回。
- 摘要使用业务语言，不出现函数名、文件名、堆栈。
- 完整结果存对象存储，保留 TTL 与访问审计。

### 5.2 ClarificationRequest（任意 Agent → Architect → 用户）

```json
{
  "type": "clarification.requested",
  "id": "clr_01",
  "askedBy": "DBACoder",
  "taskId": "task_01",
  "contextSummary": "为什么会卡住（业务语言）",
  "question": "需要用户决定什么",
  "options": [
    { "id": "opt_a", "label": "选项 A", "impact": "选择后的影响" }
  ],
  "allowFreeText": true,
  "timeoutSeconds": 300,
  "resumeToken": "checkpoint_..."
}
```

### 5.3 RiskDecision（Architect 裁决）

```json
{
  "type": "risk.decision",
  "taskId": "task_01",
  "category": "consistency|security|scope|cost|data",
  "level": "high",
  "decision": "reject | regenerate | ask_user | allow",
  "reason": "业务语言说明",
  "alternatives": ["可选路径"],
  "evidence": ["引用审计/门禁结果"]
}
```

## 6. 澄清桥状态机

```
IDLE
  → REQUESTED       子 Agent 发起
  → RELAYED         Architect 改写为用户语言
  → AWAITING_USER   等待用户选择/输入
  → ANSWERED        用户已答
  → RESUMED         原子 Agent 从 checkpoint 继续
  → CANCELLED / TIMEOUT
```

要求：

- 支持多轮，不限于一次问答。
- 必须提供 2–4 个选项，同时允许自由输入。
- 超时、取消、回退都有明确状态与事件。
- 与现有 HITL 暂停/恢复复用同一 checkpoint 机制，不重跑整任务。
- 澄清记录进入证据链，包含问题、选项、回答、影响。

## 7. 用户语言边界

- 用户界面只呈现：场景、数据、流程、影响、风险、下一步。
- 禁止出现：函数名、文件名、字段名、堆栈、框架术语、内部 ID。
- 子 Agent 输出不直接渲染；Architect 负责翻译。
- 技术详情默认折叠；工程师可显式打开。
- 依赖后置过滤器兜底，不能只靠提示词。

## 8. 高危操作策略

| 类别 | 默认阈值（可配置） | 处理 |
|---|---|---|
| 一致性 | 契约/代码/拓扑矛盾 | 直接拒绝，要求重新生成 |
| 安全 | 越权、注入、密钥泄露 | 硬拒绝 + 替代方案 |
| 需求爆炸 | 页面 > 12、实体 > 10、范围增长 > 30% | 暂停，要求用户确认范围 |
| 成本 | 超出单任务预算 | 终止并报告，不静默继续 |
| 数据破坏 | 删表、覆盖、不可逆迁移 | 拒绝自动执行，必须人工确认 |

原则：

- 拒绝必须给出原因、影响、替代路径。
- “重新生成”指新 revision，保留旧产物，不在原产物上强行修补。
- 决策进入审计，用户可见摘要。

## 9. 动作台账与可见性不变式

不变式：

1. 没有台账记录的写操作，视为未发生，并被路由器拒绝。
2. 没有 `summary` 的子 Agent 报告，视为失败。
3. 没有 Architect 知晓的任务，不得进入执行队列。

事件字段：`actor / taskId / tool / target / beforeHash / afterHash / risk / traceId`。

Architect 消费台账摘要；用户看到业务化时间线，而不是原始日志。

## 10. 分层与场景追问

### 10.1 平台分层

- Architect：编排与决策。
- 专业 Agent：能力实现。
- 工具层：确定性执行。
- 门禁与台账：横切治理。

### 10.2 生成代码分层

接口层 / 应用层 / 领域层 / 基础设施层；依赖方向由架构测试守住。

### 10.3 场景追问

Architect 在冻结契约前必须确认：

- 谁用、什么角色、多少并发；
- 部署在哪、是否私有化、是否断网；
- 接哪些外部系统；
- 数据敏感度与保留要求；
- 预期改动频率与扩展点。

结果写入契约 `usageScenarios`；缺失则不允许冻结契约。

## 11. 契约扩展

```yaml
usageScenarios:
  roles: []
  scale: {}
  deployment: {}
  integrations: []
  dataSensitivity: {}
  changeFrequency: ""
layers:
  modules: []
  dependencyRules: []
extensionPoints: []
agentPolicy:
  clarification: required | optional
  riskTolerance: strict | normal
  userLanguage: business | technical
```

## 12. 事件类型

- `agent.task.assigned` / `agent.report`
- `agent.fastpath.granted` / `agent.fastpath.revoked`
- `agent.action`（写操作台账）
- `clarification.requested` / `clarification.resolved`
- `risk.decision`
- `architect.relay`（摘要/全文选择）

## 13. 失败与降级

| 场景 | 处理 |
|---|---|
| 子 Agent 超时 | Architect 重派或降级模板 |
| 摘要缺失 | 判失败，要求重报 |
| 澄清无响应 | 超时后按保守选项或终止 |
| Architect 不可用 | 任务暂停，不自动改由子 Agent 接管 |
| 台账写入失败 | 阻止后续写操作（fail-closed） |
| 风险门禁不可用 | 拒绝高危操作 |

## 14. 指标与验收

- 未登记写操作 = **0**
- 缺少摘要的子 Agent 报告 = **0**
- 用户可见代码标识符泄漏 = **0**
- 高危操作拒绝附带替代方案 = **100%**
- 澄清命中率、平均解决时长、放弃率
- Architect 中继 token 开销占单任务比例
- 场景字段完整率（冻结契约前）

## 15. 迁移路线

| 阶段 | 内容 | 退出标准 |
|---|---|---|
| P0 | 消息协议、Architect 收件箱、A2A 改道、动作台账 | 所有子 Agent 输出带摘要；无直连 |
| P1 | 通用澄清桥、用户语言过滤器、多轮对话 | 任一 Agent 可原地暂停/恢复 |
| P2 | 风险门禁、场景追问、分层模板与架构测试 | 高危操作全部可解释拒绝 |
| P3 | 全 Agent 接入、评测与回归 | 指标达标并纳入 CI |

## 16. 与现有实现的映射

| 现有 | 改造 |
|---|---|
| `ai_core/architect/graph.py` | 增加收件箱/中继节点、场景追问、风险裁决 |
| `ai_core/fullstack/graph.py` | 子 Agent 输出改为 AgentReport；经 Supervisor 转发 |
| `ai_core/a2a.py` / `a2a_contract.py` | 允许矩阵改为“仅 Architect 地址”；A2A 直连废弃 |
| `ai_core/supervisor/*` | 保留传输与状态，移除自主路由决策 |
| `ai_core/agent/events.py` | 新增 `agent.action`、`clarification.*`、`risk.decision` |
| HITL / 离线缓冲 | 复用为澄清桥的暂停/恢复底座 |
| `domain/architect.py` | 契约新增 `usageScenarios/layers/extensionPoints/agentPolicy` |

## 17. 未决问题

1. 摘要长度与 token 预算是否需要按 Agent 类型分档。
2. Architect 与 Supervisor 的边界：Supervisor 是否保留任何降级决策权。
3. 子 Agent 之间确有高频协作场景时，是否允许“经 Architect 批准的直连通道”。
4. 澄清桥的超时默认值是否需要按风险等级区分。
