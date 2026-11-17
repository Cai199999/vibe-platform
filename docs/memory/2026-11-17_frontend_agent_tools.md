# 2026-11-17 源码级可视化：前端 Agent 语义工具

## 决策

- 不 fork Utopia；只学习其“源码唯一事实源 / 稳定节点身份 / 语义命令层 /
  命令可回放”的不变量。
- 工程模型采用 Onlook：iframe 运行应用 + 构建期注入节点 ID + DOM ↔ AST
  映射 + Babel 回写源码。
- 复用现有 Direct Ops，不新增 AST 执行器、不新增项目状态。

## 实现

新增 `ai_core/tools/frontend_visual_tools.py`：

- `update_text`
- `move_node`
- `insert_component`
- `delete_node`
- `get_frontend_revision`
- `undo_last_frontend_change`

所有工具都是 `DirectOperationService` 的门面：

- 目标解析、ScopePolicy、revision 锁、AST Gate、ChangeLog、Git checkpoint
  全部复用；
- `node_id` 自动转换为 `[data-node-id="..."]` 选择器；
- 未绑定项目上下文时 fail-closed；
- 写工具支持 `dry_run` 与显式 `base_revision`，缺省自动读取当前 HEAD；
- 失败统一返回 `ok/code/message/requiresHuman/retryable`。

## 接入

- `build_default_toolbox` 注册全部前端语义工具；
- `AgentGraphRunner.run` 按当前 Run 的 `project_id/sandbox_id` 绑定
  `FrontendToolContext`，`finally` 中恢复，避免串 Run；
- 工具仍受既有 Direct Ops 写锁与 ChangeLog 约束。

## 边界

- 暂不支持 `UPDATE_CLASS/STYLE/PROPS`、wrap/unwrap、跨文件移动；
- 这些能力应先扩展 Direct Ops，再由工具层暴露；
- 前端 Agent 不接触源码文本、anchorRange、文件写入。

## 画布选择与 API 代理修复

- 根因 1：Visual CodeGen 生成的 `vite.config.js` 只有 `react()`，
  没有注入 inspect SDK，iframe 内选择按钮无效。
- 根因 2：生成的 Vite 配置没有 `/api` 代理，`/api/v1/users/me`
  落到 Vite fallback 或平台 BFF，出现 404。
- 修复：
  - `compile_visual_document` 将 `sandbox_template/inspect_sdk.js`
    写入 `public/inspect_sdk.js`，并在 `index.html` 注入脚本；
  - 生成的 `vite.config.js` 内置 `/api -> 127.0.0.1:3000` 代理；
  - `inspect_sdk.js` 的节点选择、拖拽、直接编辑选择器增加
    `[data-node-id]` 兜底，AI 新增节点也能选中；
  - CodeGen 节点补充 `data-source-kind`，让 SDK 的容器识别与
    内联 ➕ 新增不依赖标签名猜测；
  - E2E 增加 `/inspect_sdk.js` HTTP 200 与 `/api` 代理配置断言。

## 验证

- 新增 12 项工具单测；
- Python 全量 696 项通过；
- 前端 65 项 Vitest 通过，生产构建通过；
- 真实沙盒 CodeGen E2E：inspect SDK 200、`/api` 代理配置、
  npm install / build / dev、多页面切换全部 PASS。
- 工具注册后的 schema 与 toolbox 列表已验证。
