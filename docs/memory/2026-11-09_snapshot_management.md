# 2026-11-09 · 项目快照与版本历史

## 目标

为开发者提供 IDE 式存档 / 回滚：画完图或 Agent 生成后随时保存快照，
不满意时回滚到任一历史版本。完全复用沙盒内的 Git 仓库，不新增数据库。

## 后端

- `SandboxProvider` 新增 `git_log` / `git_diff`，`LocalDockerSandbox` 用
  `_git_lock` 串行化；log 采用 `%x1f`/`%x1e` 不可见分隔符解析，异常记录
  不会让整页历史 500。
- Diff 三种模式：
  - `baseline`（默认）：初始基线与目标快照的累积代码 Diff；
  - `commit`：该提交自身改动（`git show`）；
  - `current`：当前 HEAD 与目标快照差异。
- 新增内部 API（`/api/v1/internal/sandbox/{id}/snapshots`）：
  `POST` 保存、`GET` 历史、`POST .../{hash}/restore` 恢复、
  `GET .../{hash}/diff?mode=`。恢复为硬回滚语义，目标之后的 commit
  不再出现在历史中。
- Java BFF 新增 `/api/v1/sandboxes/{id}/snapshots` 系列透传，校验
  sandboxId 与 40 位十六进制 commit hash；AI Core 不可用统一 503。

## 前端

- 画布顶栏新增 💾「保存快照」Modal（备注必填、200 字上限）。
- Tab 栏右侧新增「版本历史」抽屉：倒序展示 hash / 备注 / 时间 / 作者，
  标记当前 HEAD；恢复前展示「当前未保存的修改将丢失，是否继续？」。
- 「查看」创建虚拟只读标签 `[快照] 备注`，展示该版本累积代码 Diff；
  快照标签不参与项目 Tab 拖拽与数量上限。
- 恢复成功后递增 `previewRefresh[projectId]`，`ScaledPreview` 以新 key
  重建 iframe，确保加载回滚后的构建；同时同步 `run.result.commitHash`。
- 关闭项目时联动清理其快照标签并中止 Diff 请求。

## 验证

- 单测 164 passed（新增 `test_sandbox_snapshots.py` 5 项）。
- 新增 `scripts/snapshot_management_e2e_test.py`：保存两个快照、抽屉排序、
  只读 Diff、二次确认回滚、历史刷新、iframe 重建，全部通过。
- 既有 blank-area / clarification / developer-panel 三条 E2E 回归通过；
  `npm run build` 通过；Black 已格式化。
