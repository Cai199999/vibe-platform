# 2026-11-16 v0 基线、节点去层级与 Architect 解耦

## v0 自动基线

- 前端骨架 bootstrap 完成后，复用 `createSnapshot` 后台静默保存：
  - commit message：`v0: Architect Skeleton`
  - 返回的 commitHash 写回 run.result.commitHash / baselineCommit
- **只有 v0 快照成功** 才把 project.bootstrap 切到 `ready`、canvasMode
  切到 `live`；失败则 `bootstrap=failed / canvasMode=blank`，用户可重试。
- 后续 Direct Ops / AI Run 都基于 v0 产生 Diff，可随时回退。

## 消除架构师节点层级

- 检索确认：代码中不存在 `architect_generated` / 特殊保护标记。
- 限制只来自通用风险：shared / protected / map / 页面根。
- Resolver 增强：
  - SDK 未提供 callSite 时，用 `importersOf` 的引用文件兜底；
  - 架构师生成的独立组件可以定位到调用它的页面，而不是被当成
    不可编辑的定义根。
- 共享组件删除/移动继续使用 callSite，只改实例调用点。

## 澄清阶段与沙盒解耦

- 新增 `architect_source_timeout_seconds`（默认 5s）。
- `ArchitectService._collect_source_bounded()` 用 daemon 线程限时读取；
  超时立即回退到请求中的 files，澄清状态机不再被 Docker 导出 / 快照拖死。
- Architect 本身不保存快照；v0 保存属于前端 fullstack bootstrap 收尾。

## Architect 读取 v0 Diff 与 ChangeLog

- 启动 Architect 前前端读取当前 commit 的 baseline Diff：
  - `fetchSnapshotDiff(..., mode='baseline')`
  - 截断后作为 system 上下文注入 uiHistory；
  - 要求 Architect 主动识别用户删除/新增/修改的业务组件，并针对新实体
    询问数据源与建表。
- `projectSummary` / `latestChanges` 现在随 ArchitectRunRequest 传入。
- ChangeLog 仍由后端自动读取最近 10 条并注入 System Prompt。

## 验证

- Python 610 项 unittest 全绿；
- 前端 16 项 Vitest 全绿，生产构建通过；
- sandbox / python-ai 镜像已重建。

## 注意

- v0 基线失败时画布不会进入 live，这是有意设计；
- 旧沙盒仍需新建才能获得最新 SDK / Resolver。
