# 2026-11-16 拖拽直连 Direct Ops

## 目标

物理拖拽不再经过前端 Agent / LLM，松手后直接 AST 重排写码；
Agent 通过 ChangeLog 事后感知，而不是实时参与。

## 实现

- `inspect_sdk.js` 拖拽松手时：
  - 用 `elementFromPoint` + `stageParentOf` 计算目标父容器；
  - 若命中被拖节点自身 / 子树，回退到原父容器；
  - `stageSlotFor` 计算最终子节点序号；
  - 同一父容器内移动时，按“移除 source 后”的序号修正；
  - `drag-end` 增加 `placement`：
    `parentSelector / parentFile / parentLine / parentColumn /
    parentInstance / parentInstanceCount / insertPosition / siblingIndex`。
- `WorkspacePage.handleComponentDragEnd`：
  - 不再写入 `pendingLayoutChanges`，不再等待【保存布局】；
  - 直接调用 `MOVE_NODE` Direct Op；
  - 成功后发送 `drag-commit` 清理 SDK inline translate，并刷新预览；
  - 失败 / 无父容器时 `drag-rollback` 还原位置。
- 后端 `MOVE_NODE` 复用现有链路：
  project write lock → baseRevision → source/parent 双锚点 →
  Babel move_node → 节点数量守恒 → Git commit → ChangeLog。
- 新增组件幽灵节点的拖拽仍走 `ADD_COMPONENT` Direct Op；
  旧“保存布局”仍可用于批量暂存新增，但已有组件移动不再经过 Supervisor。

## 验证

- SDK `node --check` 通过；
- 前端 16 项 Vitest 全绿，生产构建通过；
- sandbox 镜像已重建；
- 后端 MOVE_NODE 既有单测保持通过（Python 606 项）。

## 未完成

- 浏览器点击/拖拽级 E2E 尚未自动化；本轮验证为 SDK 语法 + 前端构建 +
  后端 MOVE_NODE 单测。
- 批量混合“多个移动 + 新增”尚未统一到单次 Direct 事务；当前逐次提交。
