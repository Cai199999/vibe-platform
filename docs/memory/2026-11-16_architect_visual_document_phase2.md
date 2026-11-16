# 2026-11-16 Architect VisualDocument 接入 · 阶段二（任务一/二）

## 已完成

### 1. Architect 输出升级

- `ContractDraft` / `ArchitectContract` 新增 `visual_document`。
- DeepSeek 契约 Prompt 新增 `visual_document` 输出字段与【离线优先】铁律：
  - 必须输出完整 VisualDocument JSON；
  - 严禁 JSX / React / HTML / TS 源码；
  - 只有一个 `page` 根；
  - 节点 id 必须 `nd_` 开头；
  - 禁止 x/y 像素坐标；
  - skeleton_mode 只允许一个“待定区域”占位容器；
  - 不合法会被确定性校验打回 repair。
- `validate_draft` 对 Web 形态校验 visualDocument；
  - 模型已输出：严格 Schema 校验，失败进入 repair；
  - 旧契约 / Mock 缺失：`architect/visual.py` 确定性生成无 JSX 兜底树。
- 契约 Markdown / placeholder 文案不再包含 JSX。
- `assemble_contract` 将 visualDocument 固化为 `VisualDocument` 模型后随
  `architect.contract.ready` 下发。

### 2. 前端接入

- `WorkspacePage` 监听 active 项目的 `contract.visualDocument`：
  - 用 `visualDocumentFromContract` 补齐 projectId/title；
  - JS Schema 校验通过后写入 `visualDocumentStore`；
  - 设置项目 `canvasMode=visual`，不触发 FrontendCoder / 沙盒；
  - 首次 CONFIRMED 自动跳转 `/visual-canvas`，用 sessionStorage 防重复；
  - 同一 contract + revision + contentHash 只同步一次，避免覆盖用户编辑。
- 旧契约没有 visualDocument 时保留原 FrontendCoder 链路。
- 工作台画布新增 `visual` 模式：显示“虚拟骨架已就绪”入口，不启动 iframe。
- ArchitectPanel 确认后展示“打开虚拟画布”，不再自动初始化 React 骨架。

## 验证

- 真实 DeepSeek 契约调用：
  - `validate_draft` valid；
  - visualDocument：16 节点、DRAFT repeat 未绑定仅 warning；
  - 无 `<div` / `<Button` / `function` / `jsx` 标记。
- 真实浏览器 E2E：
  - CONFIRMED 契约 → 自动进入 `/visual-canvas`；
  - 14 个节点渲染、Schema 通过；
  - 页面 2 个 section、form/list/button 均正确；
  - 拖拽换父容器、删除、撤销成功；
  - 0 个 `/api/v1/runs`、`/sandbox`、`/direct-ops` 请求。
- Python 633 项 unittest 全绿。
- 前端 34 项 Vitest 全绿，生产构建通过。

## 尚未完成

- 局部 JSON Patch Agent（“✨ AI 魔法生成”）未开始。
- 业务审查 / 冻结 / CodeGen 未开始。
- 旧 FrontendCoder 仍在代码库中，等待阶段三替换。
- 多项目同时维护多份 VisualDocument 的本地存储尚未拆分。
