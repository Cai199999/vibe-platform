# 2026-11-05 M2.5 终局体验打磨：决策可视化与专属 Agent

## 1. 目标

把底层 Supervisor / Manifest / 强门控能力转化为面试官与用户一眼能看懂的
视觉呈现，并提供一个真实的 CustomAgent 秒级接入样例。

## 2. Supervisor 决策可视化

新增 `apps/platform-web/src/components/SupervisorTrace.jsx`：

| 阶段 | 展示 |
|---|---|
| `analyzing` | 🤖 正在分析意图…（请求发出后立即出现） |
| `decided` | 🎯 判定为局部 UI / 全局架构 / 全栈构建 / 我的专属 Agent |
| `both_waiting` | 两阶段时间轴：架构澄清（进行中）→ 前端修改（等待门控释放） |
| `ui_running` | 阶段 1 完成、阶段 2 执行中 |
| `custom` | 专属 Agent 卡片：名称、taskType、命中关键词、M3 运行时说明 |
| `rejected` | 未识别意图与缺失输入 |

数据来源：

- 前端在 route 前写入 `supervisorPhase='analyzing'`，并保证该状态至少展示
  480ms——本地规则路由通常几十毫秒返回，不设下限会导致用户只看到结果、
  看不到“思考过程”；浏览器 E2E 同步断言 analyzing 阶段确实出现；
- route 返回后保存 `supervisorDecision`（handoff / taskType / reason /
  strategy / signals / handoff displayName）；
- 时间轴状态由 `architectState`、`gate.status`、`run.result` 联合推导；
- 修复旧 Run 遗留 `result` 导致的误判：先看 `submitting` 与 `lastSeq`，
  只有本轮事件流真正到达终态才标记“已完成”。

## 3. 专属 Agent 目录与 Demo Agent

前端新增 `AgentCatalogMenu.jsx`：

- 读取 `GET /api/v1/supervisor/agents`；
- 只展示 `source=manifest` / `status=custom`；
- 展示名称、描述、taskTypes、关键词；
- 明确标注「仅路由 · M3 执行」，不把路由成功伪装成执行成功；
- 支持手动刷新，验证 Manifest 秒级接入。

新增 `services/orchestrator/config/custom_agents/excel_report_agent.json`：

```json
{
  "agentId": "excel_report_agent",
  "displayName": "Excel 报表专家",
  "taskTypes": ["excel_report_generation"],
  "keywords": ["数据报表", "Excel生成", "销售数据"],
  "systemPromptFile": "prompts/excel_report_agent.md",
  "ragKnowledgeDir": "knowledge/excel_report"
}
```

同时提供演示 Prompt 与 RAG 知识目录，证明路径安全校验与声明式能力。

## 4. CustomAgent 关键词路由

`RuleBasedIntentClassifier` 新增规则：

1. 显式 `targetAgent`；
2. 显式 `scopeHint`；
3. **CustomAgent 关键词命中（且未命中 UI / 架构词）** → `handoff_custom`；
4. UI + 架构词 → both；
5. 其余既有规则。

安全边界：UI 词优先级高于 CustomAgent，避免“把报表页面按钮改蓝”被专属
Agent 劫持；CustomAgent 仍 `dispatchable=false`，`missingInputs` 包含
`customAgentRuntime`。

## 5. 文档与演示

- `README.md` 新增 Mermaid 多 Agent 调度图、专属 Agent 秒级接入命令、
  Supervisor / CustomAgent E2E 验证入口；
- 新增 `docs/DEMO_3_MIN.md`：3 分钟面试演示脚本，按
  「精准修改 → 全局架构 + both 门控 → 专属 Agent 接入」推进，含兜底与
  量化指标；
- `docs/design/05.Supervisor_Multi_Agent.md` 增加 CustomAgent 关键词路由、
  前端决策可视化、验收项。

## 6. 验证

| 验证项 | 结果 |
|---|---|
| Python 单元测试 | 72 passed（新增 custom route / UI 防劫持） |
| Supervisor API E2E（直连 / BFF） | PASS，含 CustomAgent 路由 |
| both 强门控 API E2E | PASS |
| 浏览器混合 E2E | PASS，含决策条与时间轴状态推进 |
| 专属 Agent 浏览器 E2E | PASS，route=handoff_custom / dispatch=0 / sandbox=0 |
| 前端生产构建 | PASS |
