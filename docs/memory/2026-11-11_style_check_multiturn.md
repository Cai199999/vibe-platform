# 样式澄清（style_check）与迷你多轮聊天框

> 2026-11-11 · 解决 UI 新增组件的“风格割裂”：用户只说“加个按钮”时，
> Agent 不再自由发挥颜色 / 尺寸，而是先对齐现有设计系统。

## 任务边界

本轮实现：

1. 悬浮框从“单次表单”升级为迷你多轮 Chatbox；
2. `FrontendCoder` 前新增 `style_check` 节点，对新增组件强制追问样式。

暂缓：Design System Token 提取（任务二）与 Coder Prompt 强约束（任务四）。
当前依赖用户回答（如“和登录按钮一样”）进入 `annotation_context`，Coder
再据此复用现有类名。

## 前端：多轮对话

- `AnnotationPopover` 新增 `reply` 状态；澄清期间主输入栏切换为
  “回复 Agent”，不再覆盖最初的 `annotation.intent`；
- `clarification.required` 到达时，问题 Prompt 以 assistant 气泡写入对应
  标注的 `chatHistory`（按 `clarificationId` 去重，重连不重复）；
- 提交答案时先把用户回答写入 user 气泡，再 POST 到
  `/runs/{runId}/clarifications/{id}/answers`；
- 主输入可直接回答必填问题：提交时把回复注入第一个缺失的 required
  question，若已通过选项回答则作为 `comment` 提交；
- 澄清按钮文案变为“回复”，输入框 placeholder 引导
  “和登录按钮一样”。

## 后端：style_check 节点

拓扑变为：

    clarify_gate -> style_check -> coder

- 仅对 `CREATE_NEW` / `BATCH_LAYOUT.additions` 生效，纯位移与修改直接跳过；
- 样式线索检测（颜色、大小、圆角、主次、Tailwind 类名、“和…一样”等）；
  用户已说明样式时绝不打断；
- 未说明样式则 `interrupt()`，payload：
  - `reason=style_detail_missing`
  - `decisionImpact=MAJOR`
  - 问题 ID `style.appearance`
  - Prompt：“请问您希望这个按钮是主色还是次要色？大小与现有的提交按钮一致吗？”
  - 选项含“和现有主按钮一致 / 次要按钮一致 / 和登录按钮一样”；
- BATCH_LAYOUT 的追问投递到**嵌套新增项**的 `annotationId + threadId`，
  前端能定位到对应圈；
- 用户回答经 `_augment_style_answers` 写回：
  - 普通标注：`payload.text` 与 `intent.ui.summary`；
  - BATCH_LAYOUT：嵌套信封 + `addition.intent` 同时更新；
  - 同时追加到 `plan` 的“视觉样式约束”段，Coder 直接可见；
- `clarification_mode=off` 或达到 `max_clarification_rounds` 时停止追问。

## 布局草稿的跨澄清收尾

保存布局可能被样式澄清暂停。前端用 `layoutFlushRef` 保存本次提交快照：

- `clarification.required` 返回时**不清空** `pendingAdditions` / 位移草稿；
- 恢复后的 Run 只有 `run.succeeded` 才调用 `commitLayoutSnapshot` 清空；
- 失败 / 取消 / 拒绝时保留草稿供重试，并删除过期快照；
- `startRunStream` 在无终态时返回 `clarification.required` 事件，调用方
  不再依赖 React 状态刷新时序判断“暂停还是失败”。

## 验证

- `tests/test_style_check.py`：10 项（追问 / 跳过 / 嵌套布局 / 模式关闭 /
  轮次耗尽 / 路由 / 只增强目标标注 / E2E 暂停恢复）；
- E2E 用内存沙盒 + Mock LLM 跑通验收链路：画圈加按钮 -> `style_check`
  `interrupt()` -> 回答“和登录按钮一样” -> Coder 的标注文本与 Plan 均
  包含该约束 -> 按钮真实落盘且带 `data-vibe-created` -> `SUCCEEDED`；
- 相关套件（layout_batch / tiered_decision / clarification_comment）共
  39 项全绿；
- 前端 `vite build` 通过。
