# 暂存组件自由拖拽 + 父容器高亮 + 源码锚点绑定

日期：2026-11-13

## 目标

多轮对话确认的新增组件以“暂存态”悬浮在画布上。用户可自由拖动，但
ghost 不得越出 iframe 视口；拖动过程中 SDK 实时命中父容器并高亮；
松手只记录结构化 placement，不写代码；【保存布局】时才由 Resolver +
apply_ast_edit 写入源码。

## 前端

### 纯函数层

`src/workspace/stagedPlacement.js`：

- `clampGhostRect`：ghost 完整限制在 `[0,1]` 视口；
- `clientToFramePoint`：父页面 client 坐标 → iframe CSS 像素；
- `buildPlacementPatch`：Drop 结果 → `{rect, documentRect, frameScroll,
  addTarget, source, relativeRect, placement}`；
- `placement` 字段：`targetParentId`（instanceId）、`parent`、
  `insertAnchor`、`insertPosition`、`siblingIndex`、`relativeX/Y`、
  `confidence`；
- `stageTargetLabel`：`即将放入：Header 容器`，低置信 / 多处复用会追加提示；
- `formatPlacementAcceptance`：写进 acceptance，明确禁止 left/top。

`relativeX/Y` 只是布局提示，源码写入永远依赖父容器 + 兄弟锚点。

### CanvasStage

- 原生 Pointer Events + `setPointerCapture`，未引入 dnd-kit：
  dnd-kit 的碰撞检测无法读取跨域 iframe DOM，在这里只能当指针追踪器。
- rAF 合并 `stage-hit` 请求；同一父容器内移动不更新 React state。
- 拖动期间只改 ghost 的 `left/top`（ref 直写），不触发整树重渲染。
- 指针移出 iframe：ghost 贴边但不发命中请求，不允许把“黑边”当父容器。
- Drop 时用最后一帧重新命中（不复用旧容器），回写 placement；
  Esc / pointercancel / iframe ready / route-change 全部取消并复位。

### 协议

```text
parent -> iframe : {type:"stage-hit", payload:{requestId,x,y}}
iframe -> parent : {type:"stage-hit-result", payload:{ok,parent,rect,
                    documentRect,insertAnchor,insertPosition,insertRect,
                    siblingIndex,siblingCount,childIndex,confidence,scroll}}
```

SDK 端 `stageHitAt` 只读：`elementFromPoint` → 最近 container →
直接子节点（自身带 data-source）→ 按主轴中点计算 before/after；
无子节点时按上下半区给 inside_start / inside_end。

## 后端

1. `_resolution_requests` 把 BATCH_LAYOUT 的 nested additions 拆成
   独立 Resolver 请求，逐项解析父容器；`placement` 原样透传。
2. `resolve_target.mjs` 回传 `allowedInsertPositions`：
   自闭合元素仅 `before/after`，成对元素 / Fragment 支持 `inside_*`。
3. `_coder_layout_batch` 在 Resolver 全量可用时改走 apply_ast_edit，
   模型只填 JSX；解析失败仍回退旧整文件生成路径。
4. `_parse_edit_result` 以 placement 作为默认插入位置 / 序号，
   模型漏填也不会退化成“插在盒子后面”。
5. `apply_ast_edit.mjs` 实现 inside 插入：子节点缩进、index 越界校验、
   自闭合拒绝。

## 验证日志

```text
npm test
3 files / 13 tests passed

npx vite build
built successfully

docker exec vibe-python-ai python -m unittest ...
Ran 38 tests ... OK   # phase1，含 inside 插入 / 自闭合拒绝 / placement 透传
Ran 55 tests ... OK   # phase1 + phase2
```

## 已知边界

- 同一父容器一次提交多个新增组件时，第二次编辑的锚点 offset 会因第一次
  inside 插入而漂移，当前会返回 ANCHOR_RANGE_MISMATCH；后续需要在
  Graph 侧按父容器合并或重算 offset。
- inspect_sdk.js 属于沙盒模板：已存在的沙盒容器不会自动获得新协议，
  需要新开沙盒 / 重建模板镜像。
- `black` 在 python-ai 容器内执行，格式化后已同步回宿主机。
