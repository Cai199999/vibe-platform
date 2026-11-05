# 2026-11-05 修复：标注浮层卸载竞态导致前端卡死

## 现象

用户点击发送后立即关闭标注浮层（X），偶发整个工作区卡死；展示模式的
预览区还会出现“执行失败”。根因是 route / dispatch / SSE 的结果在浮层
卸载后才返回，继续写入已删除标注或已切换的 Run 状态。

## 修复策略

### 1. 卸载即中止

- `AnnotationPopover` 在 `useEffect` cleanup 中通知父级 `onAnnotationUnmount`。
- 工作区按 `projectId:annotationId` 持有 `AbortController`：
  - route / dispatch 请求携带 signal；
  - 关闭时 abort controller，同时 abort 项目 SSE；
  - 若后端 Run 已启动，额外发一次静默 cancel；
- SSE 指数退避的 `setTimeout` 改为可中止，abort 时立即清理定时器。

### 2. 状态写入防呆

- `workspaceMountedRef`：页面卸载后 `patchRun` / `patchGate` 直接返回。
- `runEpochRef`：每次新提交 / 强制关闭递增 epoch；旧请求在 route、
  dispatch、SSE 事件、catch、finally 各阶段检查 epoch，过期即丢弃。
- `closedAnnotationsRef`：已关闭标注集合；终态事件、仓库同步、聊天记录
  均不再写回该 thread。
- SSE finally 只清理自己的 controller，避免旧流误删新连接的 controller。

### 3. 关闭交互

- 执行中点击 X 不再直接删除，弹出内联确认：
  “任务正在执行中，强行关闭可能丢失结果，是否继续？”
- “继续等待”保留任务；“仍然关闭”立即中止并移除标注。
- 普通状态下 X 仍直接删除。

### 4. 错误边界

- 新增 `ErrorBoundary`，顶层包裹 App；画布 / 预览每个面板单独包裹。
- 单个面板崩溃只显示该区域的降级 UI，其他面板与顶部工作区不受影响。

## 验证

`scripts/popover_close_race_e2e_test.py`：

1. 输入意图、点击发送、立即点 X -> 确认关闭；
2. 标注与浮层清空，页面主线程仍可响应；
3. 新开标注输入正常；
4. 展示模式等待 8s，无“执行失败”、无白屏、无未捕获异常；
5. 追加场景：dispatch 返回、SSE 执行中再关闭，迟到终态同样被丢弃。

结果：PASS（pageerror=0，预览区无报错）。

回归：`multi_annotation`、`annotation_delete_rollback`、`multi_turn_chat`、
`settings_manual_approval`、`ui_e2e` 全部 PASS。
