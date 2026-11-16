# 2026-11-16 Direct Ops 视觉隐藏 HITL

## 目标

把 ScopePolicy 返回的 `strategy=hitl` / `options` 真正渲染到前端，
让用户在“删除 map 列表卡片”时可以选择“仅隐藏视觉”，画布立即变化
且不报错、不修改代码。

## 后端

- `DirectDecisionRequest`：`decisionId` / `optionId` / `comment`。
- `analyze_delete` 在 `strategy=hitl` 时生成一次性 `decisionId`，内存暂存
  payload、anchor 摘要、scope、options，TTL 600s，上限 200 条。
- 新增 `POST /api/v1/internal/direct-ops/decisions`；BFF 对应
  `POST /api/v1/direct-ops/decisions`。
- `DirectOperationService.decide`：
  - `VISUAL_ONLY` → `status=VISUAL_ONLY`，写 `applied=false`、
    `visual_only=true` 的 ChangeLog，不写 Git；
  - `CANCEL` → `status=CANCELLED`；
  - `AI_DATA_EDIT` / `DETACH` / `DEFINITION` / `WRAP` →
    `status=HANDOFF_TO_AI`，显式交给后续 AI 流程，绝不静默失败。
- 决策一次性消费；重复提交返回 409 `DIRECT_ACTION_INVALID`。

## 前端

- 新增 `DirectOpsDecisionDialog`，复用 RuntimePanel 的 decision 样式
  （`decision-options` / `decision-option` / `dialog-backdrop`）。
- WorkspacePage 收到 `direct-delete-result` 且带 `decisionId+options` 时：
  - 保存来源 iframe 的 `event.source`；
  - 弹出决策面板；
  - 回给 SDK `handled=true`，避免 SDK 再弹“已拦截”提示。
- 选择后调用 `submitDirectDecision`：
  - `VISUAL_ONLY` 向 SDK 发送 `direct-visual-hide`；
  - 关闭面板并显示成功 Toast。
- SDK 用 `directDeleteNode` 记住被点击的真实 DOM；selector 只作为
  fallback，并按 `instance` 取第 N 个匹配，修复 map 循环永远命中第一项。

## 验证

- Python 621 项 unittest、前端 16 项 Vitest、BFF 单测全绿。
- 真实 Chromium + 真实沙盒 E2E：
  1. 渲染 3 张 map 卡片；
  2. 点击第 3 张卡片的 ❌；
  3. 决策面板出现 3 个选项；
  4. 点击“仅隐藏视觉上的当前项”；
  5. 第 3 张卡 `display:none` + `data-vibe-visual-hidden=true`；
  6. 弹窗关闭、无错误 Toast，SDK 显示成功提示；
  7. `data/changelog.jsonl` 新增 `visualOnly=true, applied=false` 记录。

## 遗留

- `AI_DATA_EDIT` 目前只返回 `HANDOFF_TO_AI`，尚未自动把目标交给
  Architect/Agent 执行数据源修改。
- MOVE/ADD/UPDATE 的 HITL options 尚未统一走同一决策面板。
- OperationExecutor 拆分与 WorkspacePage/SDK 编排拆分仍待完成。
