# 2026-11-10 · 防脑补与最小化骨架生成

## 问题

用户只输入“做个工作台”后，Architect 脑补出 LoginForm、ResumeUploader、
JDdetail 等无关组件，FrontendCoder 把全部卡片平铺在同一页面，导致重叠与乱码。

## 修复

### 1. 确定性抽象需求闸门

- 新增 `ai_core/architect/skeleton.py`：
  - `is_abstract_brief()`：剥离“帮我/做一个/简单的”等通用词后，只含
    工作台、仪表盘、后台、系统等容器名词，且没有任何业务信号时才判定抽象；
  - `should_use_skeleton()`：仅对“空白新项目（无源码/摘要/具体槽位）”生效；
  - `build_skeleton_draft()`：直接生成 Header + Sidebar + Main、一个
    `isPlaceholder=true` 的“待定区域”，不调用任何模型。
- Graph 新增 `skeleton_gate`，位于 RAG 之后、`architect_analyze` 之前；
  澄清恢复后也会再次经过，用户回答“没想好”仍停在骨架。
- `_draft_contract` 在 `skeleton_mode` 下绕过 LLM；契约校验修复也直接重建骨架。

### 2. 契约占位符

- `ContractDraft` / `ArchitectContract` 增加 `skeletonMode`；
- 组件支持 `isPlaceholder`（兼容 `is_placeholder`）；
- `validate_draft` 对骨架契约强制“只能一个占位组件”，禁止业务组件。

### 3. Prompt 铁律

`DeepSeekArchitectLLM` 的 analyze / draft / revise 均加入：

- 需求不明确禁止脑补 LoginForm / ResumeUploader / JDdetail；
- 抽象目标只能输出空白骨架；
- 每次追问最多 3 个核心问题；
- 未聊透区域只输出占位组件。

### 4. FrontendCoder 排版

- 模板改为真实 CSS：`Header + Sidebar + Main`；
- 组件区 `grid-template-columns: 1fr / repeat(2) / repeat(3)`（768 / 1200 断点）；
- 卡片 `min-height`、占位区 `min-height + overflow-y:auto`，主区可滚动；
- 骨架模式只渲染 “Welcome to your new app” 与一个 `<Placeholder>`；
- 即使历史契约混入业务组件，生成阶段也只保留第一个占位组件。

## 验证

- `python -m unittest discover -s tests`：238 通过；
- 真实 SSE：`做个工作台` → `architect.skeleton.detected` → 契约
  `skeletonMode=true`、`lightweight_web`、1 个占位组件；
- 前端 bootstrap：`mode=frontend` 生成 `src/App.jsx`，沙盒预览 200；
- BFF 透传后仍保留 `skeletonMode`。
