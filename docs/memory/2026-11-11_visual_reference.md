# 可视化选型与设计系统兜底（2026-11-11）

## 问题

澄清回答“和登录按钮一样”只是一段自然语言。大模型无法跨域读取沙盒 DOM，
只能猜颜色 / 圆角，生成结果与用户预期不符。

## 方案

1. **可视化选型**：`visualReference.js` 识别指代式回答（和/与/跟…一样、
   参考…、同款、照着…）。命中后不提交文本，画布 / 预览进入 Inspect
   取色模式，提示“请点击页面上您想参考的组件”。用户取消后对同一段文本
   放行一次，避免循环。
2. **绝对视觉签名**：`inspect_sdk.js` 的 pick / analyzeSelection 提取
   `tailwindClasses`（节点 + 最多 24 个后代）与 computed CSS
   （backgroundColor / borderRadius / fontSize / padding / boxShadow 等），
   剔除默认值后回传。
3. **强约束 Prompt**：`compact_annotations` 生成 `reference_styles` 与
   `current_styles`；Planner / Coder 被要求逐字复用参考类名，禁止近似值。
4. **设计系统兜底**：`design_system.py` 静态解析 `tailwind.config.*` 与
   `:root` / `@theme` CSS 变量，经 AgentState 传给 Coder；有令牌时新组件
   必须优先 `bg-primary` / `text-secondary` 等语义类名。

## 关键文件

- 前端：`workspace/visualReference.js`、`CanvasStage.jsx`（检测 + 取色提示）、
  `ProjectWorkspace.jsx`（取色状态 / 提交）、`RuntimePanel.jsx`（预览横幅）、
  `WorkspacePage.jsx`（reference 随澄清答案提交、挂回标注）。
- 后端：`domain/agent.py`（VisualReference）、`agent/service.py`（答案归一化 +
  读取设计系统）、`agent/graph.py`（reference 写回 payload）、
  `agent/ui_context.py`（reference_styles）、`agent/llm.py`（Prompt 铁律）、
  `agent/design_system.py`。
- SDK：`sandbox_template/inspect_sdk.js`（styleSignature）。
- 测试：`tests/test_visual_reference.py` 6 项。

## 验证

- 后端相关套件 58 项通过；全量 379 项仅容器缺 `eval_suite` 的历史导入错误。
- 前端 `npm run build` 通过；`node --check inspect_sdk.js` 通过。
- 沙盒镜像 `vibe-platform/sandbox-react:dev` 已重建；已有运行中沙盒需重建
  才能拿到新 SDK（Vite 插件的 SDK 有进程内缓存）。
