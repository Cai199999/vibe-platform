# vibe-platform · 2026-11-13 技术总结

> 范围：暂存组件自由拖拽落地的最后一公里——同容器连续插入的确定性问题、
> 沙盒镜像一致性、真实浏览器 E2E 验收。
> 证据目录：`docs/screenshots/`、`docs/memory/2026-11-13_*.md`。

---

## 一、今日任务清单

| # | 任务 | 结果 |
|---|------|------|
| 1 | 修复同一父容器连续插入两个新增组件时的 offset 漂移 / 顺序错乱 | ✅ 52 项 phase1 单测覆盖 |
| 2 | 重建沙盒基础镜像，确保 `inspect_sdk.js` 与 Resolver/Editor 脚本最新 | ✅ 镜像 `sha256:b63bda91…`，stage-hit 实测 exact |
| 3 | 真实浏览器 E2E：拖拽高亮、Esc 取消、同容器双插入保存、路由切换清幽灵 | ✅ `ALL STAGED DRAG E2E CHECKS PASSED` |
| 4 | 修复 HITL 澄清快捷选项被“画布取色”拦截 | ✅ 一键选项立即提交 |
| 5 | 修复拖拽中静态 ghost 卸载导致 pointer capture 丢失 | ✅ 静态节点保留、透明占位 |

---

## 二、技术栈

- **前端**：React 18 + Vite（`apps/platform-web`）
  - 原生 Pointer Events + `setPointerCapture` 实现跨域 iframe 上的拖拽；
  - 父页面 ↔ iframe 通过 `postMessage` 协议通信（`stage-hit` /
    `stage-hit-result` / `ready` / `enable` / `route-change`）；
  - Zustand + IndexedDB 做草稿隔离与恢复。
- **BFF**：Java 21 / Spring Boot 3.3.5（`apps/platform-bff`），只做编排与鉴权透传。
- **AI Core**：Python 3.12 / FastAPI / LangGraph（`services/orchestrator/ai_core`）
  - 确定性 Resolver（Node/Babel 脚本）先定位锚点；
  - Coder 只能调用 `apply_ast_edit`，没有整文件写权限。
- **沙盒**：Docker + Vite dev server（`vibe-platform/sandbox-react:dev`），
  SDK 在页面加载时注入并劫持 `history.pushState/replaceState`。
- **AST 编辑**：Babel parser/traverse，服务端计算插入点、长度与子节点序号。
- **模型**：DeepSeek 真实调用（Planner / Coder / Summary 分 task 路由）。
- **测试**：Python `unittest`（536 项）、前端 Vitest（13 项）、
  Playwright（Python，真实 Chromium + 真实容器）。

---

## 三、问题、方案与取舍

### 问题 1：连续插入后锚点 offset 漂移

**现象**：同一文件、同一父容器连续两次 `apply_ast_edit`，第二次直接报
`ANCHOR_RANGE_MISMATCH`。

**根因**：第二条编辑仍使用 Resolver 给出的“原始文件”offset，但第一条
插入已经改变文件长度。

**方案**（`graph.py` + `apply_ast_edit.mjs` + `ast_edit_tool.py`）：

1. Babel 脚本每次插入返回 `insert = {offset, length, index, position}`，
   其中 offset 是**编辑前内容**坐标系下的精确插入点；
2. Graph 维护 `insert_transforms`，用 `_project_anchor_range` 把后续
   锚点重投影到最新内容：
   - 插入点在锚点起点之前（含起点）→ start/end 同时右移；
   - 插入点落在锚点区间内部 → 只扩展 end；
   - 插入点在终点之后 → 区间不动；
3. 同一文件后续编辑通过服务端注入 `content_override` + 重投影后的
   `anchor_range`，模型不可见、也无法伪造。

**为什么不用“整文件重写”或“内容 diff 反推 offset”**：
- 整文件重写会绕开 AST 门禁（白名单、保护组件、范围一致性），是明确禁止的写路径；
- 内容 diff 在“相同前缀 / 相同标签”场景会误判插入点，offset 必须由
  Babel 在编辑前坐标系里精确给出。

### 问题 2：最终源码顺序与拖入顺序相反

**现象**：先拖「提交」再拖「重置」，保存后源码里「重置」在前。

**根因**：`ordered.sort(key=start, reverse=True)` 对相同锚点是稳定排序，
实际执行顺序 = 模型 `tool_calls` 的顺序。模型并不保证按拖入顺序输出。

**方案**：

1. `_layout_addition_ranks` 从 BATCH_LAYOUT 的 `payload.additions` 提取
   拖入顺序（annotationId → rank）；
2. `_edit_intended_rank` 用 `jsx_code` 里的 `data-vibe-created` 映射回
   rank（回退锚点 annotationId）；
3. `_edit_order_key`：锚点起点降序；同起点时 `inside_*` 按 rank 升序，
   `before/after` 按 rank 降序执行。

**为什么以 `payload.additions` 为准**：它是前端按用户操作顺序追加的
事实数据；LLM 的 tool_call 顺序只是生成结果的排列，不是用户意图。
把“视觉顺序”建立在模型自觉上不可靠，必须回到确定性输入。

**为什么 `before/after` 要降序**：兄弟插入每次紧贴锚点，
后执行的组件离锚点更近；要得到拖入顺序，执行顺序必须反过来。
`inside_index` 则是序号顺延，执行顺序即最终顺序。

### 问题 3：模型预补偿序号导致越界

**现象**：`INVALID_INSERTION_POINT: insertIndex 6 > childCount 5`。

**根因**：两次拖拽的 `placement.siblingIndex` 都是 4，模型为了让
第二个排在后面，自行把 `insert_index` 写成 5；Graph 又按
`parent_slots` 顺延一次 → 6。

**方案**：批次内 `inside_index` 的序号完全由服务端 `_batch_insert_index`
计算：`placement.siblingIndex + 同父容器前序 rank 数`，**不采信模型
的 insert_index**。

**为什么不在 `_normalize_insert_slot` 里“猜模型是否已补偿”**：
无法从单个数值判断模型意图，任何启发式都会在某个场景下双重补偿或
补偿不足。Resolver 的 placement 已是“拖拽当时的确切落位”，
服务端只需叠加拖入顺序，信息完整、无需猜测。

**兼容性**：只有存在 BATCH_LAYOUT 顺序信息时才接管；普通多标注 /
非批次流程保持原有“模型顺序 + 顺延”逻辑（`test_same_anchor_inserts_follow_drag_order_without_batch` 回归）。

### 问题 4：沙盒镜像与 SDK 版本不一致

**现象**：新容器不响应 `stage-hit`，拖拽永远没有父容器高亮。

**根因**：沙盒镜像构建时未打包最新的 `inspect_sdk.js` / 编辑脚本，
容器用的是旧模板。

**方案**：强制重建 `vibe-platform/sandbox-react:dev`，E2E 第 1 步校验
新容器的 `Image` 必须等于当前镜像 id，第 2 步用真实 `postMessage`
探针验证协议。

**为什么不用“运行时挂载脚本”**：沙盒是用户代码的隔离运行环境，
挂载宿主脚本会破坏镜像可复现性，也会让“本地能跑、重建后不能跑”
的问题再次出现。镜像即契约，构建时就该固化。

### 问题 5：拖动中路由切换残留幽灵组件

**现象**：drag 期间 iframe `pushState`，拖拽高亮消失，但暂存 ghost 仍在。

**排查过程**：父页面旁听 `postMessage` 确认 `route-change` 已经到达，
再对比 DOM 发现：
- ghost 没有 `is-capture-anchor` → CanvasStage 已清掉拖拽态；
- 标注浮层仍在、没有 Toast → 页面级清理函数根本没执行。

**根因**：`WorkspacePage` 的 `workspaceActionsRef.current` 稳定动作表
漏注册 `handleFrameRouteChange`，`projectActions` 里的
`call('handleFrameRouteChange', payload)` 落入可选链静默 no-op。
CanvasStage 只清自己的 drag 会话，标注 / 暂存草稿归页面级管理。

**方案**：把 `handleFrameRouteChange` 加入动作表，并补注释说明职责边界。

**为什么清理放在页面级而不是 CanvasStage**：
标注、草稿、Toast 都属于整个 Run 的页面状态，CanvasStage 只是画布视图；
让画布组件直接改 run 状态会破坏单向数据流，也会让非画布入口
（快捷键、外部刷新）漏清理。动作表是既有架构的统一入口，缺的只是注册。

### 问题 6：拖拽中 ghost 卸载导致 pointer capture 丢失

**现象**：拖到一半拖拽会话“卡死”，pointerup 落进 iframe，永远收不到结束事件。

**根因**：拖拽开始时把静态 ghost 从 DOM 卸载，改用独立拖拽 ghost；
而 pointer capture 绑定在被卸载节点的把手上，节点移除即释放 capture。

**方案**：静态 ghost 始终保留在 DOM，拖拽期间只设 `opacity: 0` +
`aria-hidden` + `is-capture-anchor`，内容与 chip 隐藏；拖拽视觉交给
`StagedDragGhost`。

**为什么不用 `display:none`**：`display:none` 同样会让元素失去事件
命中与布局，pointer capture 依然会失效。透明占位是唯一既保留 capture
又不干扰视觉的方式。

### 问题 7：HITL 快捷选项被“视觉参考”拦截

**现象**：点「和现有主按钮一致」后没有提交，反而进入画布取色模式。

**根因**：`REFERENCE_INTENT_RE` 命中了“和…一致”文案，把服务端预置的
结构化选项误判成自由文本的视觉参考请求。

**方案**：`applyQuickOption` 调用 `submitClarification` 时传
`skipReference: true`，结构化 quick option 直接提交。

**为什么加显式开关而不是改正则**：正则服务于自由文本输入，收紧会破坏
“和登录按钮一样”这类真实意图；快捷选项与自由文本是两条通道，
应该在调用点区分，而不是让一个正则同时承担两种语义。

### 问题 8：E2E 取证容易“自证清白”

**踩过的坑与处理**：

- 保存超时其实不是超时，而是 style `clarification.required` 暂停 →
  等待循环里自动选择推荐项；
- `git show HEAD` 可能为空（HEAD 的树与父提交相同）→ 用
  `git log -S 'data-vibe-created="ann_xxx"'` 精确定位引入提交；
- `frame_locator` 没有 `evaluate` → 先取 `ElementHandle.content_frame()`
  再执行真实 Frame 上下文里的 `pushState`；
- 断言位置要用真实 payload 结构（`payload.additions` /
  `annotation.payload.placement`），不能按想象的结构写。

**为什么坚持真实环境**：Mock 能验证协议形状，但验证不了 pointer
capture、跨域 iframe、HMR 重载、真实模型输出顺序这些边界；
本轮三个 bug 全部只在真实浏览器 + 真实容器下才暴露。

---

## 四、验证结果

```text
Python unittest      536 passed
前端 Vitest           13 passed
真实浏览器 E2E        ALL STAGED DRAG E2E CHECKS PASSED
```

关键证据：

- `docs/screenshots/staged-drag-diff.log`：两个按钮严格按拖入顺序落在
  `<section>` 内，仅 className + `data-vibe-created`，无 `absolute/left/top`；
- `docs/screenshots/staged-drag-esc-cancelled.png`：Esc 后无 ghost / 无落点；
- `docs/screenshots/staged-drag-route-cleared.png`：路由切换后 ghost、
  高亮、标注全部清空；
- `docs/screenshots/staged-drag-highlight-ann_*.png`：父容器高亮即时出现。

---

## 五、设计原则沉淀

1. **用户操作事实 > 模型输出顺序**：排序、落位、标识都以结构化 payload 为准。
2. **坐标变换只在服务端做**：模型的 `insert_index` 只作参考，批次内由
   placement + rank 计算，避免双重补偿。
3. **单一写路径不可绕开**：AST 工具是唯一写入口，整文件重写 / 裸 diff 一律拒绝。
4. **视觉占位与交互锚点分离**：拖拽 ghost 可以换，但 pointer capture 的锚点节点不能卸载。
5. **清理职责跟随状态归属**：画布管拖拽会话，页面管标注与草稿。
6. **镜像即契约**：SDK / Resolver / Editor 必须随镜像固化，E2E 先验镜像再验协议。
