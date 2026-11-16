# 2026-11-16 离线优先可视化引擎 · 阶段一

## 重构目标

把“用户自由编辑”和“React 源码合法性”解耦：

- 生成前：虚拟组件树是唯一事实源，不写源码、不启动沙盒、不 commit；
- 生成后：CodeGen 一次性编译，Direct Ops 降级为微调模式；
- 虚拟树只读层级/语义，不读像素坐标；
- 代码生成后不双向同步，避免双事实源。

## 已完成（阶段一）

### 1. VisualDocument Schema

新增 `services/orchestrator/ai_core/domain/visual_document.py`：

- `VisualDocument`：schemaVersion、docId、projectId、revision、status、
  designTokens、root、interactions、bindings、review、metadata。
- `VisualNode`：id、type、name、componentRef、props、layout、style、
  children、repeat、semantics、a11y、state、codegen、provenance、locked。
- 节点类型：page / section / container / grid / stack / text / button /
  input / image / list / table / form / modal / component。
- 校验：
  - nodeId 唯一、最大节点数 5000、最大深度 20；
  - 禁止 `x / y / absoluteX / absoluteY` 等像素坐标；
  - 非容器不能有 children；只有 list/table 支持 repeat；
  - interaction / binding 必须指向存在的 nodeId；
  - repeat.source 必须有匹配 binding；
  - 禁止 `dangerouslySetInnerHTML` / `__html`；
  - 空白画布 `root=None` 合法。
- `freeze_visual_document`：status=FROZEN、revision 自增、写
  `metadata.contentHash`（排除 contentHash 自身，稳定 JSON 排序）。
- `validate_visual_document` 返回稳定报告，不向上抛 Pydantic 异常。

### 2. 前端可视化树

新增 `apps/platform-web/src/visual/visualDocument.js`：

- 与 Python 对齐的 JS 校验器；
- `createEmptyVisualDocument` / `createVisualNode`；
- 纯函数命令：
  - `insertVisualNodeIntoDocument`
  - `deleteVisualNodeFromDocument`（允许删除根节点，得到合法空画布）
  - `moveVisualNodeInDocument`（换父容器 / 同父排序，禁止拖进子树）
  - `updateVisualNodeInDocument`
  - `duplicateVisualNodeInDocument`

### 3. Zustand 离线命令栈

新增 `apps/platform-web/src/visual/visualDocumentStore.js`：

- 单一 `doc` 状态；
- `addNode / deleteNode / moveNode / updateNode / duplicateNode /
  toggleLock / setTitle / resetDocument`；
- 80 步 undo/redo 快照；
- 250ms 防抖写入 localStorage（`vibe.visualDocument.v1`）；
- 所有命令只改本地 JSON，不调用 API。

### 4. OfflineVisualCanvas

新增 `apps/platform-web/src/visual/OfflineVisualCanvas.jsx`：

- 左侧组件库；中间页面画布；右侧属性面板；
- 渲染 page / section / container / grid / stack / text / button / input /
  image / list / table / form / modal / component；
- 拖拽 60fps：
  - pointerdown 创建 `position:fixed` ghost；
  - pointermove 只通过 rAF 直接写 `transform` 和 drop 高亮 class；
  - 不 setState / 不写 Zustand / 不请求后端；
  - pointerup 同步再解析一次落点，然后提交一次 `moveNode`。
- 支持：
  - 任意节点删除，包括页面根；
  - 拖拽换父容器 / 同父排序；
  - 节点文本、名称、布局、间距、列数、最大宽度编辑；
  - 复制、锁定、撤销、重做、清空；
  - Schema 实时校验状态、revision、自动保存。
- 入口：`/visual-canvas`，工作台工具栏“✨ 虚拟画布”。

## 验证

- 真实 Chromium + Vite：
  - 空白画布加载；
  - 添加 2 个区块 + 文本；
  - 文本拖入第二个区块，DOM 父子关系变化；
  - 删除节点 → 撤销恢复；
  - localStorage 文档 revision 4；
  - 全程 0 个 `/api/` 请求。
- Python 630 项 unittest 全绿（新增 9 项 VisualDocument）。
- 前端 30 项 Vitest 全绿（新增 12 项）。
- 前端生产构建通过。

## 尚未完成

- Architect 尚未输出 `VisualDocument`（阶段二）。
- 局部 JSON Patch Agent 尚未实现（阶段二）。
- 业务审查 / 冻结 / CodeGen / data-node-id（阶段三）。
- Direct Ops 微调模式与“退回虚拟编辑模式”（阶段四）。
- IndexedDB 多版本文档存储；当前 localStorage 仅保存最新文档。
- 虚拟树的响应式断点、设计令牌编辑、JSON 导入导出尚未做。
