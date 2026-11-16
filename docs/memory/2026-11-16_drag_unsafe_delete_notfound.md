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

## 验证

- 新增 callSite move 单测；
- 全量 Python 608 项通过；
- 前端 16 项 Vitest 与构建通过；
- sandbox 镜像已重建（包含 projectId 路由、自动确认、锚点保护）。

## 仍未完成

- CLONE_SIBLING / iterationGroup；组件下方复制 ➕ 尚未上线。
- 自定义组件根节点的 data-source 映射仍需单独增强。
