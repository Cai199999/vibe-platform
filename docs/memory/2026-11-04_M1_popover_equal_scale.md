# 2026-11-04 · 标注浮层改为随画布等比缩放

## 决策

旧实现让浮层锚点跟随世界坐标，但用 `transform: scale(1 / viewportScale)`
反向补偿父级缩放，使屏幕字号恒定。用户确认改为 Figma 式行为：

> 悬浮对话框是画布的子元素，完全跟随画布的 transform 矩阵一起放大、
> 缩小、平移，就像画在画布上的一个带文字图形。

## 实现

- `CanvasStage.jsx`：删除 `popoverStyleFor(rect, index, active, scale)` 的
  `inverseScale`、`transformOrigin` 与所有 `1 / scale` 尺寸换算；浮层定位
  直接使用 1280×720 世界坐标（`left/top/right` 均为世界 px）。
- 浮层宽度固定 `320px`（`.canvas-popover { width: 320px }`），不再使用
  `min(320px, calc(100% - 24px))` 百分比宽度，避免缩放时布局比例变化。
- 浮层仍是 `.canvas-viewport` → `.annotation-layer` 的普通子元素，不脱离
  画布坐标系，也没有独立 transform。
- 层级：圈线 `z-index` 20（激活 100），浮层默认 `300 + index`、激活 320，
  保证浮层永远在圈线之上，同时激活浮层高于其他浮层。
- 世界坐标中的边界 clamp（10px）与错开间距（12px）不再除以 scale。

## 交互影响

- 50% 缩放时浮层屏幕宽度为 160px、文字同步变小；150% 时按比例放大。
- 平移 / 缩放后浮层相对画布偏移保持不变，与圈、页面 DOM 完全同步。
- 在浮层上按中键 / 空格 + 左键仍会冒泡给画布，不产生拖动死角。
- 小比例下输入框可点击区域变小，这是“像图片一样缩放”的预期结果；
  需要精确输入时可先点击「重置视图」回到 100%。

## 测试

`scripts/popover_equal_scale_e2e_test.py`：

```text
100%: scale=1.1750 popover=376.00×135.12 input=269.08×43.48 transform=none
 50%: scale=0.5875 popover=188.00×67.56  input=134.54×21.74 transform=none
150%: scale=1.7625 popover=564.00×202.69 input=403.61×65.21 transform=none

归一化比例（input 高 / 浮层宽、浮层高 / 宽、头部高 / 宽）三档完全一致
浮层 transform=none；圆角世界值 14px；z-index 320 > 圈线 100
中键平移后浮层相对画布偏移变化 0
POPOVER EQUAL-SCALE E2E: PASS
```

回归：

- `multi_turn_chat_e2e_test.py`：PASS（已改为断言浮层世界宽度恒为 320）
- `rollback_e2e_test.py`：PASS
- `multi_annotation_e2e_test.py`：PASS
- `annotation_delete_rollback_e2e_test.py`：PASS
- `ui_e2e_test.py`：PASS（平移 / 缩放 / 对齐 / 账户全流程）
- `docker exec vibe-platform-web npm run build`：PASS

截图：`docs/screenshots/m1-popover-scale-{50,100,150}.png`。
