# 2026-11-09 · 架构师对话前置：新建项目状态机

## 目标

新建项目不再先渲染 Mock 页面，也不再让用户在「全栈 / 轻量 / 脚本」三选一
卡片里做技术决策。用户先描述目标，Architect 通过多轮自然语言对话识别应用
形态、补齐槽位，产出包含前端路由 / 页面布局 / 组件清单的《架构实施契约》；
契约确认后自动触发 FrontendCoder 在沙盒生成真实 React 骨架，画布切换为
真实页面，用户再开始圈选 / Inspect 微调。

## 状态机

```
新建项目
  │ createProject(blank=true)：files={}, bootstrap=pending, canvasMode=blank
  ▼
BLANK ──【✨ 开始创建】──▶ ARCHITECTING
                              │  ArchitectGraph：自然语言 ui_history
                              │  形态推断 → 动态槽位 → 置信度门控
                              ▼
                    AWAITING_CONFIRMATION
                              │  用户 CONFIRM
                              ▼
                          CONFIRMED
                              │  自动触发 FrontendCoder（fullstack mode=frontend）
                              │  FrontendCoder → BuildVerifier → SandboxRestart
                              ▼
                    FRONTEND_READY（bootstrap=ready, canvasMode=live）
                              │  画布 iframe 展示真实页面
                              ▼
                          ITERATING（Inspect / 圈选微调）
```

关键规则：

- 应用形态由 `slots.infer_app_shape()` 从自然语言目标主动推断，Architect
  不再输出 `app.shape` 三选一；推断不出时用自由文本询问用户群体与核心功能。
- 契约新增 `frontendRoutes`、`pageLayout`、`components` 三个一级字段，
  作为 FrontendCoder 的唯一输入，不允许再靠猜测生成页面。
- FrontendCoder 的 `mode=frontend` 只执行三个节点：生成 `src/App.jsx` 骨架、
  `npm run build`、以 `/` 为就绪探针重启沙盒；不生成后端 / DDL / IaC。
- 初始「演示项目」与导入项目保持 `bootstrap=ready, canvasMode=mock/live`
  的旧行为，避免破坏既有画布圈选与 E2E；只有新建项目进入 blank 流程。
- 生成失败时 `bootstrap=failed`，画布提供「重试初始化前端」，不会永久转圈。

## 契约扩展

- `frontendRoutes: [{path,name,description}]`
- `pageLayout: [{region,title,description}]`
- `components: [{name,role,section,description}]`

Mock 与 DeepSeek 都必须输出这三项；`FrontendCoder` 据此生成导航、页面标题、
区域与组件占位卡片，并写入 `data-source` 供 Inspect 精确定位。

## 验证

- Python：198 个模块用例 + 9 个 eval suite 用例，共 207 项 PASS。
- `scripts/conversation_first_e2e_test.py`：新建 → 开始创建 → 纯对话契约 →
  自动三节点 → 画布实时页面 PASS。
- 回归：`architect_m2_5`、`architect_ui`、`fullstack_disabled_hint_ui`、
  `fullstack_gate_ui`、`fullstack_generation_ui`、`supervisor_mixed_ui`、
  `supervisor_both`、`supervisor_e2e`、`fullstack_generation_e2e`、
  `fullstack_e2e_check` 全部 PASS。
- 前端 `npm run build` PASS（78 modules，427.63 kB）。
