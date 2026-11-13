# Phase 0 草稿隔离（Worktree Draft）（2026-11-13）

## 目标与结论

模型 / Applier 在审批前不再直写主工作树。每次 Run 从干净 HEAD 创建
`git worktree` 影子目录 `/app-drafts/<draft_id>`，构建与预览都在草稿中完成；
只有用户点击“确认并合并”后，才以 `git merge --ff-only` 快进主分支。

E2E 19/19 通过：暂停时主树 HEAD / `src/App.jsx` SHA 不变且 `git status` 为空；
草稿预览可访问；批准后 HEAD 前进、工作树干净；拒绝后主树零变化；草稿目录与
5174 预览均被回收。

## 核心实现

- `SandboxProvider`：新增 `begin_draft` / `discard_draft` / `promote_draft`，
  默认返回 `None` 表示不支持（测试替身走旧直写路径）。
- `LocalDockerSandbox`：
  - 创建容器时同时发布 `5173`（主预览）与 `5174`（草稿预览）；
  - `begin_draft` 校验主树无未提交跟踪文件，`git worktree add --detach`，
    将 `node_modules` 软链回主树，启动第二个 Vite（`VITE_HMR_CLIENT_PORT`
    使用宿主端口），返回 `SandboxDraft(preview_url=?draft=<id>)`；
  - `promote_draft` 校验主 HEAD 仍等于 `base_sha`、草稿以 base 为祖先，
    提交草稿后 `git merge --ff-only`，再移除 worktree；
  - `discard_draft` 停预览 + `git worktree remove --force` + `prune`，幂等；
  - 同一沙盒已有未完成草稿时拒绝再开（5174 端口唯一）。
- `AgentState`：新增 `draft_id` / `draft_preview_url`。`applier`、`verifier`、
  `_missing_created_markers` 的写盘 / 构建 / 回读全部携带 `draft_id`。
- `RunExecutionService`：
  - `_prepare` 创建草稿并把 `VIBE_CONTEXT.md` 写入草稿；创建失败直接中止，
    **绝不回退直写主工作树**；
  - `_complete_finished`：`AWAITING_APPROVAL` / `SUCCEEDED` 先 promote 再
    `git_commit`；promote 失败收敛为 `PROMOTE_FAILED`；
  - 拒绝 / 失败 / 取消 / 审批过期统一 `discard_draft`，不做 `git reset`
    （主树本来就没变）；
  - 审批暂停响应与 `approval.required` 事件均返回草稿预览 URL + `isDraft`。
- 前端 `RuntimePanel`：审批期间 iframe 优先使用 `approval.previewUrl`；
  文案改为“等待确认 / 确认并合并 / 放弃草稿”。

## 安全不变量

1. 模型无 `draft_id` 时只能读主树；所有写操作必须命中已登记的 draft_id。
2. 草稿目录由服务端 ID 推导，调用方不能传任意路径。
3. promote 只由 Service 在用户审批事件后调用，Graph 节点无权调用。
4. 主树在审批前保持 `git status --porcelain` 为空、HEAD 不变。

## 验证

- `tests/test_draft_isolation.py`：11 项，覆盖 worktree 生命周期、dirty 主树、
  并发草稿拒绝、promote HEAD 漂移、审批 payload 与 Service 收尾。
- 全量 Python：469 passed（`/app` 容器内，排除依赖仓库目录结构的
  `test_eval_suite.py`）。
- `npm run build` 通过；`scripts/diagnostics/draft_isolation_e2e.py` 全绿。

## 后续

Phase 1：Resolver + `apply_ast_edit` 工具协议；Phase 2：`AWAITING_DECISION`
多轮分流。草稿隔离已为两者提供物理安全边界。
