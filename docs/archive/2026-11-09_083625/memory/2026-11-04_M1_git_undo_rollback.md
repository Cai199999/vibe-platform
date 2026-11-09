# 2026-11-04 · 沙盒 Git Checkpoint 与单圈回滚

## 目标

为画布编辑器加入生产级 Undo / Rollback：

1. 每次 Agent 通过 Verifier、进入 `AWAITING_APPROVAL` 时，在沙盒内提交
   Git checkpoint；
2. 每条成功助手消息可以单独回滚到该轮代码 + 对话上下文；
3. 删除单个标注时，M1.5 直接回到该圈创建前的全局基线；
4. 在架构上记录 M2 的 Event Sourcing / 重放方案。

## Git Checkpoint（任务一）

### 沙盒初始化

- 沙盒容器启动后立即执行 `git init`，写入 `.gitignore`
  （`node_modules/`、`dist/`、`.vite/`、`*.log`），配置本地
  `user.name=Vibe Agent`、`user.email=agent@vibe.local`。
- 以当前模板源码创建 root commit：
  `baseline: sandbox init`，作为“创建任何标注之前”的全局基线。
- 兼容旧沙盒：首次 `git_commit` / `git_reset_hard` 时即时初始化，以当时
  代码作为 baseline。`git rev-list --max-parents=0 HEAD` 返回 root commit。

### 提交时机与内容

- 只有 `final_state.status == AWAITING_APPROVAL` 才提交：
  `git add -A && git commit --allow-empty -m "run: {runId} - turn: {turnCount}"`。
- `--allow-empty` 保证“模型没有实际 diff 但构建通过”的轮次也有稳定锚点。
- `VIBE_CONTEXT.md`、Agent 修改的源码、模板文件都进入同一次 commit。
- 提交失败时 Run 被标记为 `FAILED`（错误 `Git checkpoint 失败`），不允许
  前端出现“成功但无法 Undo”的轮次。
- 一个 Run 只有 **一个全局 commit**。批量 3 个标注共享同一个 commit hash，
  但每个 thread 有自己的 `turnCount`。

### 响应契约

```json
{
  "commitHash": "40 位 SHA-1",
  "turnCount": 2,
  "baselineCommit": "root commit",
  "commits": [
    {
      "threadId": "thread_x",
      "turnCount": 2,
      "commitHash": "...",
      "runId": "run_x"
    }
  ]
}
```

前端把 `commitHash` / `turnCount` 写进该圈的成功助手消息（`chatHistory`），
同时写入 `sessionStorage`。下一轮请求把它们带回后端，因此 python-ai 重启后
仍能推导下一轮 turn。

## Thread Checkpoint（任务二）

`ThreadMemoryStore` 中每个 `ThreadRecord` 增加 `checkpoints[]`。成功 Run
在 `record_run()` 末尾生成深拷贝快照：

```text
{runId, turnCount, commitHash, baselineCommit, sandboxId,
 chatHistory, runs, files, createdAt}
```

这等价于当前架构下 LangGraph Checkpointer 的 thread 级快照语义；现有
LangGraph Graph 没有持久化 checkpointer，因此 M1.5 由 ThreadMemoryStore
承担该职责（M2 可替换为 `BaseCheckpointSaver` + Redis/MySQL）。

回滚接口：

```http
POST /api/v1/runs/rollback
{
  "sandboxId": "sbx_...",
  "threadId": "thread_...",
  "commitHash": "...",
  "runId": "...",
  "turnCount": 2
}
```

执行顺序：

1. 按 runId / turnCount / commitHash 查找 checkpoint，校验 commit 一致；
2. `git reset --hard {commitHash}`（参数数组传入，40 位十六进制白名单）；
3. 恢复 thread 的 `chatHistory`、`runs`、`files`，截断之后的 checkpoint；
4. 返回带 `?v={timestamp}` 的 `previewUrl`，前端替换后 iframe 强制刷新。

前端 Chatbox 在每条带 `commitHash` 的助手消息旁展示
`turn N · <short sha>` 和「↩️ 回滚到此步」。点击后：

- 只保留目标消息及其之前的聊天记录；
- 更新 `lastRunId` / `lastSandboxId` / `lastCommitHash` / `lastTurnCount`；
- 用响应中的 `previewUrl` 刷新预览。

## 多轮提交顺序

单圈连续两轮：

```text
baseline: sandbox init
run: run_A - turn: 1     # HEAD~1
run: run_B - turn: 2     # HEAD
```

回滚到 turn 1 后，HEAD 指回 `run_A` 的 commit，turn 2 的 commit 不在当前
分支（仍在 reflog）。继续发送“改成绿色”时：

```text
run_A - turn: 1
run_C - turn: 2          # 新的 turn 2，父提交是 run_A
```

即回滚后 turn 从目标轮次重新递增，而不是继续 3；Git 历史始终线性，避免
在旧代码上叠加已撤销的修改。

## 删除标注的全局基线回滚（任务三 · M1.5）

- 圈选时记录 `baseCommitHash = 当前 HEAD`；第一轮成功后如果为空，则用
  `baselineCommit` 补齐。
- 删除有成功 Run 的圈：调用同一 rollback 接口 reset 到 `baseCommitHash`，
  然后从画布删除该圈。没有成功 Run 的圈只删除 UI，不碰代码。
- 「清除标注」以最早执行过的圈为锚点，reset 到它的创建前基线，再清空全部。
- 这是有意的最简策略：全局 Git 线性历史无法只撤销 B 而保留 B 之后的 A/C。
  删除 B 会丢弃 B 之后的所有全局提交。

### M2 Event Sourcing / 重演

目标：A/B/C 修改同一文件，撤销 B、保留 A/C。

1. 每个标注的每次成功 Run 保存不可变事件：
   `{annotationId, threadId, intent, baseCommit, commitHash, files, timestamp}`；
2. 删除 B 时不要直接以 `git reset --hard` 作为最终状态，而是从 B 的
   `baseCommit` 建立临时分支；
3. 按创建顺序重放仍未撤销的 A、C 的最新意图：把它们的
   `threadContext + 当前源码` 重新交给 Agent，生成新 commit；
4. 用一个 “replay Run” 串行执行，Verifier 通过后把结果作为新的 HEAD；
5. 重放失败时保留原状态并提示用户，不允许半完成状态成为最终代码。

M2 还需要把 thread checkpoint 与事件日志落到 Redis/MySQL，保证 python-ai
重启、多个副本之间仍可回滚。

## 测试

### `scripts/rollback_e2e_test.py`

```text
第一轮: turn=1 commit=7014a91
第二轮: turn=2 commit=646ad4f
git log: ['baseline: sandbox init', 'run: ... - turn: 1', 'run: ... - turn: 2']
回滚 turn 1: checkpointRestored=True，HEAD -> 7014a91，聊天记录 4 -> 2
预览恢复 rgb(255, 0, 0)
回滚后继续对话: turn=2 commit=e7618ae（新 hash）
预览变绿 rgb(0, 128, 0)
ROLLBACK E2E: PASS
```

### `scripts/annotation_delete_rollback_e2e_test.py`

```text
A 改红 -> commit A1
B 在 A 之后创建并改绿 -> commit B1
删除 B -> HEAD 回到 A1，A 的聊天记录保留，预览恢复红色
清除标注 -> HEAD 回到 baseline，预览恢复初始 rgb(37, 99, 235)
ANNOTATION DELETE ROLLBACK E2E: PASS
```

### 回归

- `multi_turn_chat_e2e_test.py`：PASS
- `multi_annotation_e2e_test.py`：PASS
- `ui_e2e_test.py`：PASS
- `agent_e2e_test.py`：PASS
- `platform_e2e_test.py`：PASS
- `docker exec vibe-platform-web npm run build`：55 modules，PASS
- `black --check services/orchestrator/ai_core`：PASS

## 已知边界

- Thread checkpoint 仍是进程内存储；python-ai 重启后 Git 回滚仍有效，但
  `checkpointRestored=false`，前端按本地消息坐标截断历史。
- 跨标注回滚会丢后续全局提交，M1.5 明确接受；M2 用事件重放修复。
- Git commit 保存完整源码快照，不保存 `node_modules` / `dist` / `.vite`。
- 导出包继续排除 `.git`，因此导出的项目是干净源码，不含回滚历史。
