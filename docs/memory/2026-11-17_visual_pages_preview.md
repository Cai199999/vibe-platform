# 2026-11-17 多页面 VisualDocument 与高保真预览

## 目标

解决画布“单薄、不像网页、不能切页”的问题。

## Schema

- 新增 `VisualPage`：
  - `pageId`（pg_ 开头）、`path`、`title`、`root`；
  - root 必须是 `page` 节点；
- `VisualDocument.pages` / `activePageId`：
  - pages 为权威列表，root 镜像当前页；
  - 节点 ID 全局唯一；页面路径唯一；
- `VisualInteraction.targetPageId`：navigate 跳转目标页；
- 旧单页文档自动归一为一页，保持兼容；
- 后端校验 / 冻结 hash / 只读投影 / delta 全部 pages-aware。

## 前端

- `visualDocument.js`：
  - `ensureVisualPages` / `activePageIdOf` / `withActivePageRoot`；
  - `switchVisualPage` / `addVisualPage`；
  - 所有编辑命令经 touchDocument 同步回当前页；
  - 删除页面根节点 → 移除页面或回到合法空白画布。
- Zustand：
  - `activePageId`；
  - `switchPage` / `addPage`；
  - undo/redo / replace / freeze 保持页面一致性。
- 画布：
  - 顶部页面 Tab + “＋ 页面”；
  - “👁 预览”打开 `VisualDocumentPreview`。
- `VisualDocumentPreview`：
  - 真实 HTML/CSS 渲染（designTokens CSS 变量）；
  - 页面 Tab、navigate 按钮切换页面；
  - 覆盖 page/section/container/grid/stack/text/button/input/image/
    list/table/form/modal。

## 网页模拟预览

- `visualPreview.js`：
  - 按 path/title 选择 dashboard / table / form / cards 模板；
  - 提供统计卡、工具栏、表格、卡片、表单、页脚与示例数据；
  - `shouldSimulatePage`：页面已有 >=7 个有效业务节点时不叠加模板。
- `VisualDocumentPreview` 在真实节点后追加模拟页面，仅预览、不写回；
- 解决 V0 JSON 稀疏时预览像白纸的问题。

## 冻结与业务审查顺序

- FROZEN 的 repeat.source 未绑定改为 warning，不再阻止进入业务审查；
- 只有 COMPILED / CodeGen 前才强制 binding 闭环；
- CodeGen 新增 `VISUAL_UNBOUND_REPEAT` 错误码；
- 第二次 Architect 审查范围扩大：
  - 全部 UNRESOLVED interactions；
  - 全部未绑定 repeat.source；
  - 全部 button/input/form/list/table；
  - 再处理 visualDelta；
  - 不假设 V0 了解业务语义。
- semantic review packet 新增 `interactiveComponents` /
  `repeatsWithoutBinding`。

## Architect V0

- `frontendRoutes` 多路由时，确定性 builder 生成多页：
  - 首页承载完整组件；
  - 其余页面 header + main 占位；
- Prompt 要求：
  - 多路由输出 pages（pageId/path/title/root）；
  - 首页 header + main + 至少 3 个业务区块；
  - 真实文案与示例数据，禁止全占位；
  - navigate interaction 使用 targetPageId。

## CodeGen

- 多页编译：
  - 每页生成 `PageX` 函数；
  - `VISUAL_PAGES` 列表 + `useState` 当前页；
  - 页面导航按钮；
  - navigate 交互生成 `onNavigate(targetPageId)`；
- data-node-id / data-source 门禁覆盖所有页面；
- fallback 占位页覆盖所有页面节点。

## 验证

- 前端 59 项 Vitest（新增多页面、预览交互测试）。
- Python 675 项全量（7 项既有环境失败）。
- 浏览器 E2E：
  - 新建 → V0 生成 2 个页面；
  - 画布页面 Tab=2；
  - 高保真预览 Tab=2，第二页显示真实 DOM；
  - 添加节点 → 冻结 → BACKEND_BINDING；
  - `visualDelta.added=1`，`visualDocument.status=FROZEN`；
  - 0 浏览器错误。

## 已知限制

- 多页导航目前是客户端 state 切换，不是 React Router；
- V0 额外页面只给占位，业务内容依赖增量审查；
- 画布线框编辑与高保真预览是两套渲染，但共用同一 VisualDocument；
- 跨文件组件/复杂布局仍属后续阶段。
