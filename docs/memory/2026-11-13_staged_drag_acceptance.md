# 暂存拖拽三边界验收：offset 漂移 / 沙盒镜像 / 真实浏览器 E2E

日期：2026-11-13

## 背景

上一轮完成暂存组件自由拖拽后，留下三个边界问题：同一父容器连续
插入会因 offset 漂移报 `ANCHOR_RANGE_MISMATCH`；沙盒镜像可能不含
最新 `inspect_sdk.js`；缺少真实浏览器端到端验收证据。

## 1. 同容器多插入：确定性定序 + 序号服务端计算

问题分两层，且互相放大：

1. **锚点漂移**：同一文件后续编辑仍用原始 offset。已由
   `insert_transforms` + `_project_anchor_range` 重投影，插入点在锚点
   之前右移整个区间，区间内部只扩展终点。
2. **顺序不确定**：`ordered.sort(key=start, reverse=True)` 对同锚点
   稳定排序，实际顺序 = 模型 `tool_calls` 顺序。模型可能先写第二个
   拖入的组件，最终源码顺序与画布视觉顺序相反。
3. **序号重复补偿**：模型看到两次 `placement.siblingIndex=4`，可能
   自行把第二个写成 5；Graph 再按 `parent_slots` 顺延一次 → 6 >
   childCount 5，工具报 `INVALID_INSERTION_POINT`。

修复（`agent/graph.py`）：

- `_layout_addition_ranks`：从 `annotation_context` 的 BATCH_LAYOUT
  `payload.additions` 提取拖入顺序（annotationId → rank）。
- `_edit_intended_rank`：优先用 `jsx_code` 里的 `data-vibe-created`，
  回退锚点 annotationId，把每条编辑映射回拖入序号。
- `_edit_order_key`：起点降序；同起点时 `inside_*` 按 rank 升序，
  `before/after` 按 rank 降序（紧贴锚点插入，后执行者更靠近锚点）。
- `_batch_insert_index`：批次内 `inside_index` 的序号完全由
  `placement.siblingIndex + 同父容器前序 rank 数` 计算，**不采信模型
  的 insert_index**，从根上消除双重补偿越界。
- 缺少 BATCH_LAYOUT 顺序信息时保持旧行为（模型顺序 + 顺延）。
- `_parse_edit_result` 保持原校验；无新增白名单。

单测：phase1 52 项，覆盖反向 tool_calls、模型预补偿序号、无批次
回归、兄弟插入排序键。

## 2. 沙盒镜像

`vibe-platform/sandbox-react:dev` 重新构建，镜像 id
`sha256:b63bda91…`，新容器实测 `stage-hit` 返回
`ok:true / parent=card / insertPosition=after / siblingIndex=4 /
confidence=exact`，证明 SDK 与 resolver/editor 脚本均已打包。

## 3. 真实浏览器 E2E（`scripts/staged_drag_e2e_test.py`）

通过 DeepSeek 真实调用与真实 Docker 沙盒，验证：

- 拖动中 Esc：幽灵与高亮立即清空（`staged-drag-esc-cancelled.png`）。
- 同一卡片容器连续拖入「提交」「重置」两个暂存组件：高亮框、插入
  线、落位 chip 正常；保存后 Diff 严格按拖入顺序插入 `<section>` 内，
  仅使用 className + `data-vibe-created`，无 absolute/left/top
  （`staged-drag-diff.log`）。
- 拖动中 iframe `pushState` 切路由：父页面收到 `route-change`，标注、
  幽灵、高亮、暂存草稿全部清空，Toast 提示“页面已切换”
  （`staged-drag-route-cleared.png`）。

### 路由切换幽灵残留的真实根因

不是 SDK 或 CanvasStage 的问题：`WorkspacePage` 的
`workspaceActionsRef.current` 稳定动作表漏注册了
`handleFrameRouteChange`，`projectActions` 里的
`call('handleFrameRouteChange', payload)` 落入可选链静默 no-op。
CanvasStage 自己清了拖拽态，页面级标注 / 暂存草稿从未被清。
修复为把 `handleFrameRouteChange` 加入动作表。

## 验证结果

- Python：536 项 unittest 全部通过。
- 前端：13 项 Vitest 全部通过。
- E2E：`ALL STAGED DRAG E2E CHECKS PASSED`。
- 证据：`docs/screenshots/staged-drag-*.png` + `staged-drag-diff.log`。
