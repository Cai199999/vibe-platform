# 2026-11-09 · CREATE_NEW 拖拽定位与保存门控

## 背景

旧方案在生成新组件时把绝对坐标写死进代码，用户无法调整位置，只能重新
圈选 / 继续对话。同时缺少“未保存”状态，用户可在改动未落盘时直接进入
Architect，导致全栈方案与当前界面不一致。

## 改动

### 1. 动作语义

- 新增 `ai_core/domain/actions.py` 与前端 `workspace/annotationActions.js`：
  `CREATE_NEW` / `MODIFY_EXISTING` 为唯一语义，兼容旧 `ADD` / `MODIFY`。
- Planner / Coder Prompt 明确分流；`AnnotationEnvelope.intent_action`
  归一化后供上下文文档、Mock Coder 使用。

### 2. CREATE_NEW 只生成主体

- Coder 不再生成绝对定位，新增组件带 `data-vibe-created="<annotationId>"`。
- 成功 Run 后 `finalizeFromResult` 生成 `run.placement`：
  `{ annotationId, selector, parentSelector }`，并置 `run.dirty=true`。
- `ScaledPreview` 通过 inspect SDK 新增的 `measure` / `measured` 消息拿到
  iframe 内组件与父容器 rect；HMR 未完成时 500ms × 16 次轮询。
- 预览上绘制可拖拽浮层，记录相对父容器的像素偏移；确认时提交一次
  合成 `MODIFY_EXISTING` 标注，`payload.position={x,y,width,height,selector,parentSelector}`。
- Coder 收到 position Patch：父容器补 `position:relative !important`，
  目标节点写 `position:absolute !important; left/top`，HMR 后即为用户拖拽位置。

### 3. 保存门控

- `run.dirty` 在任意成功生成 / 拖拽后为 true。
- 【下一步：构建全栈方案】增加 `aria-disabled` + `is-locked` 样式；点击仅
  Toast「请先保存当前修改」，不改变视图。
- 保存快照成功后：`dirty=false`，缓存 `projectSummary` 与 `latestChanges`。
- 快照保存刷新 `global_summary.md` 与 `user_profile.md`；Architect 请求
  携带 `projectSummary` / `latestChanges`，Prompt 要求复用已确认技术选型。

### 4. 上下文记忆落盘修复

- `CreateSnapshotRequest` 改用 `CamelRequest`：BFF 透传的 `projectId` /
  `tenantId` / `latestChanges` 之前被 Pydantic 静默忽略，首次快照无法
  生成全局摘要。
- `python-ai` 新增 entrypoint：启动时以 root 修正 bind mount 的 `/app/data`
  属主，再用 `setpriv` 降权到 app（保留 docker 附加组）。不修的话
  `global_summary.md` 因 `PermissionError` 被 try/except 静默吞掉。

## 验证

- Python：`189 passed, 4 subtests passed`。
- Java：`SandboxControllerSnapshotTest` PASS。
- 前端：`vite build` 通过。
- E2E `scripts/drag_placement_e2e_test.py`：
  画圈 → CREATE_NEW → 拖拽 +180/+90 → 确认 → computed `position:absolute`
  left/top > 0 → 未保存时按钮 `aria-disabled=true` + Toast → 保存快照 →
  `projectSummary` 453 字、`latestChanges` 含拖拽说明 → Architect 澄清可见。
- 回归：`add_component` / `blank_area_add` / `architect_ui` /
  `snapshot_management` E2E 全 PASS。

## 注意

- 修改 `sandbox_template/inspect_sdk.js` 后必须重建
  `vibe-platform/sandbox-react:dev` 镜像；该文件在构建期 COPY，旧镜像会
  因插件 `sdkCache` 一直返回旧脚本，表现为只有 `measure` 没有 `measured`。
- 拖拽确认复用 Supervisor → UI Graph → Coder，不新增专用接口。
- 取消拖拽只清浮层并标记 `placementDone`，不写代码，也不改变 `dirty`。
