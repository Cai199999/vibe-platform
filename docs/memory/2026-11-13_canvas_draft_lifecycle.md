# 画布草稿生命周期 / 自动 checkpoint / 预览降级

日期：2026-11-13

## 背景

`begin_draft` 要求主工作树干净；前端骨架 / 全栈生成直接写主工作树却没有
commit，导致下一次标注 Run 报 `主工作树存在未提交修改`。同时画布拖拽 /
新增组件只存在内存 ref，刷新、关 Tab、退出登录会丢草稿；预览断线时
`视口联动` 仍可能保持开启。

## 本次落地

### 1. 生成后自动 checkpoint

- 位置：`WorkspacePage.jsx` fullstack.completed effect。
- 复用 `snapshotClient.createSnapshot()`，不新增接口 / 按钮。
- `mode === 'frontend'` 与全栈生成成功后，先提交 Git checkpoint，再
  切换画布为 live / 聚焦预览。
- 成功：`dirty=false`，写入 `projectSummary` / `latestChanges`。
- 失败：`dirty=true` + Toast，提示手动点击【保存快照】，不阻断预览。
- `fullstackHandledRef` 保证同一 runId 只处理一次。

### 2. Draft Store（Zustand + IndexedDB）

文件：

- `src/workspace/draftStorage.js`：IndexedDB 持久层、串行写队列、墓碑。
- `src/workspace/draftStore.js`：Zustand store、revision barrier、Toast 纯函数。
- `src/workspace/__tests__/draftLifecycle.test.js`。

记录键：`<tenantId>:<userId>:<projectId>`。

记录字段：

```text
{ key, tenantId, userId, projectId, epoch, seq, status, ops[], savingBarrier, updatedAt }
```

每条操作稳定身份为 `layout:<selector>` / `addition:<annotationId>`：

- 内容不变：保留原 seq；
- 内容变化 / 新增：`seq + 1`；
- 撤销 / 已提交：从记录移除。

### 3. 保存快照 revision barrier

`submitSnapshotSave` 点击瞬间执行 `beginSave(scope)` 记录 `barrier`。
保存成功后只删除 `seq <= barrier` 的操作：

- 仍有新操作：Toast `已保存进度，保存期间的新修改已保留`；
- 无新操作：清空操作，`epoch + 1`、`seq = 0`，Toast
  `已保存到项目，本地草稿已清空`。

快照前若仍有画布草稿，先复用现有【保存布局】链路把草稿写进代码，再做
Git checkpoint，避免“快照成功但画布修改丢失”。

### 4. PreviewState 与视口联动降级

文件：`src/workspace/previewState.js`、`ViewportContext.jsx`、
`usePreviewConnection.js`。

状态：`LIVE / BOOTING / DEGRADED / SNAPSHOT / OFFLINE_EMPTY`。

```text
effectiveLinked = linkedPreference && previewState === 'LIVE'
```

- 只有 LIVE 允许联动；
- BOOTING / DEGRADED / SNAPSHOT / OFFLINE_EMPTY 仅禁用按钮并显示弱提示；
- 任何状态迁移都不写 `linkedPreference`，恢复 LIVE 后自动恢复用户偏好。

### 5. 离开守卫

- 关闭 Tab：`DraftLeaveGuard` 自定义 Modal；取消保留全部状态；确认先清
  IndexedDB 再销毁项目 / 沙盒。
- 退出登录：`AuthContext.logout` 先查内存 + IndexedDB；有草稿则弹同一
  Modal；确认后清空当前用户全部草稿再跳转登录页。
- 刷新：`beforeunload` 触发浏览器原生确认；真正 `pagehide`
  （`event.persisted === false`）同步写 localStorage 墓碑；下次启动
  hydrate 前清理对应项目草稿。
- 崩溃 / 进程被杀：没有 pagehide，不写墓碑，草稿保留。

## 验证

```text
npm test -- --reporter=verbose
2 files / 6 tests passed
npx vite build --outDir /tmp/vibe-dist --emptyOutDir
built successfully
```

关键测试日志：

```text
[并发保存] barrier = 1
[并发保存] 保存期间操作 = [{"id":"layout:.hero","seq":2},{"id":"layout:.footer","seq":3},{"id":"addition:ann_during_save","seq":4}]
[并发保存] commit 后保留 = [...同上...]
[并发保存] Toast = 已保存进度，保存期间的新修改已保留

[预览降级] LIVE -> effectiveLinked = true
[预览降级] offline -> previewState = SNAPSHOT effectiveLinked = false
[预览降级] linkedPreference 保持 = true
[预览降级] 恢复 LIVE -> effectiveLinked = true
```

## 遗留

- 孤儿 Draft 7 天回收（`updatedAt > 7d` 且 projectId 不在 workspace）尚未实现。
- 本轮未改 Python / BFF，无 black / 后端测试变更。
