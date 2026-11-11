# 2026-11-11 · 批量拖拽与统一提交

## 目标

用户会连续拖动多个组件调整整体布局，不能每次 `pointerup` 都触发一次模型调用；
必须把多次手势合并为一次确定性补丁。

## 前端

- SDK 仍只在 `pointermove` 写内联 `translate`，拖拽期间零 API。
- `WorkspacePage` 新增按项目隔离的 `pendingLayoutChangesRef: Map<projectId,
  Map<selector, {source, dx, dy, base, next, token, payload}>>`，以及渲染快照
  `pendingLayoutChanges`。
- 松手回调只更新草稿；同一组件再次拖动在 `previous.next` 上叠加，已提交基线
  `dragCommittedRef` 只在保存成功后更新。
- `.project-viewport` 右下角新增悬浮操作条：`已修改 X 个组件位置` +
  【撤销】+【保存布局】。撤销清空草稿并通过 `drag-rollback` 恢复 DOM；
  用户可继续重新拖拽。
- 双屏预览按 selector 保存所有未保存草稿的临时位移，拖第二个组件时
  第一个不会弹回；同一组件的新手势在旧位置上继续。
- 保存失败 / 超时**不回滚、不清空草稿**，Toast 提示草稿已保留；保存成功
  清空对应快照并 Toast“布局已同步”。

## 后端

- `AnnotationEnvelope.action` 新增 `BATCH_MOVE`；`payload.moves[]` 每项含
  `source / file / selector / dx / dy`。
- `_explicit_batch_moves` 严格校验非空、选择器、有限且 ±2000px 的偏移；
  任何非法记录整体回退 LLM，避免只移动一部分组件。
- `_is_explicit_move_batch` 识别单个 BATCH_MOVE，使 Planner / RAG 跳过模型。
- `_try_move_tool` 在一次循环中调用 `move_component` 生成所有 CSS 规则，
  最终只做一次 `append_css_rule` 合并与一次 `_combined_diff`，避免并行写文件。
- 保留旧协议：单条 `action=MOVE` 与自然语言移动路径不变。

## 验证

- 新增 BATCH_MOVE 契约、非法记录拒绝、单次合并补丁测试；331 个 Python 单测通过。
- `npm --prefix apps/platform-web run build` 通过。
- Docker `python-ai` 重建并健康检查通过。
