# 2026-11-16 拖拽 UNSAFE_OPERATION 与删除 TARGET_NOT_FOUND

## MOVE_NODE：共享组件走 callSite

- 新增 `DirectOperationService._move_source_anchor()`：
  - source 为共享组件且 Resolver 提供 `callSiteAnchor` 时，
    实际移动目标使用 callSite 的 file/range；
  - 不修改公共组件定义文件。
- `_risk()` 对 MOVE_NODE：
  - 有 callSiteAnchor 且 parent file == callSite file → L0；
  - parent 仍是公共容器 → L2；
  - 无 callSite / 跨文件 → 维持 L3。
- `_build_new_content` / ChangeLog 使用 effective move anchor 的
  file/range，避免把定义文件写入。
- 新增单测：共享 Card 在 `src/Page.jsx` 的调用点移动成功，
  AST 编辑 file 为 callSite 文件。

## DELETE_NODE：项目路由与锚点保护

- 根因之一是多 Tab / 双屏下全局 message 监听器用 `activeTabId`：
  - iframe 可能属于另一个项目；
  - `handle.project_id != payload.project_id` 会被误报为
    `DIRECT_TARGET_NOT_FOUND`。
- 修复：
  - ProjectWorkspace 将 `projectId` 传给 CanvasStage / RuntimePanel /
    ScaledPreview；
  - `direct-mode` 消息携带 projectId，SDK 在 direct-add /
    direct-delete-request / direct-delete-confirmed / direct-text
    中回传；
  - WorkspacePage 优先使用 `payload.projectId`，不再盲用 activeTabId。
- SDK 删除前若 `readSource` 为空，直接提示“目标已失效，请重新选择”，
  不再发送空 target 触发 TARGET_NOT_FOUND。

## 新建组件删除确认

- SDK 检测 `data-vibe-created="direct"`：
  - analyze-delete safe 后直接确认删除，不再弹确认框；
  - 原有骨架 / 公共组件仍保留确认与安全分析。

## 共享组件 callSite 删除

- 真实 E2E 复现：Card 在 App.jsx 中渲染 2 次，删除被
  `isShared + isRouteRoot` 拦截。
- 修复：
  - `_delete_source_anchor()` 优先使用 `callSiteAnchor`，实际删除
    `src/App.jsx` 中的 `<Card />` 实例，不动 `Card.jsx` 定义；
  - `_risk()` 检测 callSite 后允许 `L0_DIRECT`；
  - `analyze_delete_anchor()` 存在 callSite 时，不再把定义文件里的
    “顶层 JSX 根节点”当作删除阻断；
  - 仍保留 map 内删除拦截。
- E2E：`analyze-delete` 返回 `safe=true`；执行后 App.jsx 从两个
  `<Card />` 变为一个，Card.jsx 未被修改。

## 诊断增强

- MOVE_NODE 的 UNSAFE_OPERATION 现在携带：
  - sourceFile / parentFile / callSiteFile
  - parentShared / sharedReason / risk
  - Toast 直接显示这些字段，便于判断是 callSite 缺失还是父容器受保护。
- Resolver 偶发失败自动重试一次（80ms）；仍失败时返回
  `DIRECT_RESOLVER_UNAVAILABLE` 并携带：
  - resolverError
  - file / line / column
  不再把底层 SandboxError 吞成泛化“服务不可用”。

## AST stage 竞态修复（P0）

- 根因日志：
  `resolver exception: ENOENT ... /tmp/vibe-ast-tool-stage/spec.json`
- `_run_ast_tool` 每次调用使用唯一目录：
  `AST_TOOL_STAGE_DIR-<uuid12>`；
  并发 analyze-delete / resolve / apply 不再互相 `rm -rf`。
- 20 路并发 analyze-delete（同一沙盒、同一有效锚点）全部 200，
  不再出现 503 / ENOENT。
- SDK 删除请求增加 pending 防重（3s 超时），避免快速点击打爆解析器。
- 删除前若目标已断开，先按 selector 重新查找；仍找不到直接提示
  “目标已失效”。

## 错误诊断

- Resolver 失败自动重试一次，仍失败返回 `resolverError` +
  file/line/column。
- TOOL_FAILED 现在返回异常类型、底层 error、action，
  前端 `directOps` 会解析 detail 并拼进 Toast。
- 移动 UNSAFE 返回 source/parent/callSite/parentShared/risk。

## 拖拽索引与自定义组件

- 根因：SDK 的 siblingIndex 只统计“带 data-source 的 children”，
  而 Babel `move_node` 统计全部 JSX children。
- Architect 骨架包含无 data-source 的自定义组件时，序号偏小/偏大，
  Babel 报 `INVALID_MOVE_TARGET: insertIndex 超出子节点范围`，
  前端只看到 `DIRECT_TOOL_FAILED`。
- 修复：拖拽落点改用 `parentNode.children` 全部元素计算序号，
  与 Babel 的 JSX children 对齐；同父容器仍按移除 source 后修正。

## 错误诊断透传

- `translate_exception` 现在把 HTTPException.detail 中的附加字段
  （toolCode / toolMessage / resolverError / file / line 等）保留到
  `ErrorEnvelope.diagnostic`。
- 前端 `directOps.js` 合并 `detail + diagnostic` 到 Toast。
- 实测：非法移动返回
  `DIRECT_TOOL_FAILED`，诊断包含
  `toolCode=INVALID_MOVE_TARGET, toolMessage=insertIndex 超出子节点范围`。

## 根节点删除

- 分析结果新增 isShared / protected / inMap / isRouteRoot /
  deleteSafe / callSiteFile / hasCallSite。
- 页面顶层根节点（hasCallSite=false）仍禁止删除；
  共享组件实例有 callSite 时允许删除调用点。

## 验证

- 新增 callSite move 单测；
- 全量 Python 608 项通过；
- 前端 16 项 Vitest 与构建通过；
- sandbox 镜像已重建（包含 projectId 路由、自动确认、锚点保护）。
- 并发冒烟：20 次 analyze-delete 并发，全部 200；修复前混合 200/404/503。

## 仍未完成

- CLONE_SIBLING / iterationGroup；组件下方复制 ➕ 尚未上线。
- 自定义组件根节点的 data-source 映射仍需单独增强。
