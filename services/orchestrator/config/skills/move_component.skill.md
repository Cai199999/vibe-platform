---
skillId: ui.move_component
name: 组件移动
agentId: ui_graph
priority: 55
description: 自然语言移动组件时，只调整 CSS transform，不修改宽高。
tools:
  - read_file
  - apply_code_edit
  - move_component
  - run_build
ragRefs:
  - knowledge/ui/style
---
当用户要求“移动 / 挪到 / 往右一点 / 拖到某处”时，必须遵守：

1. 移动是 Agent 的专属能力，调用 `move_component(element_id, x, y)` 生成位移规则；
2. 只允许使用 `transform: translate3d(x, y, 0)`（或等价 `transform: translate(x, y)`）；
3. 严禁为了移动而修改 `width`、`height`、`max-width`、`min-width`，也不要把拖拽辅助线写进最终代码；
4. 拖拽过程中的实时预览同样使用 CSS transform，松手后再提交最终位移；
5. 如果用户没有给出精确像素，按语义换算：挪一点=24px，右/左/上/下按方向，
   “到右边/底部”按父容器可用空间计算，并把实际像素写入修改说明。
