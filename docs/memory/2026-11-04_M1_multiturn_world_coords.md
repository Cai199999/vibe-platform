# 2026-11-04 · 画布世界坐标、Z-index 与单圈多轮对话

## 目标

对 M1 画布交互做一次深度重构，解决三个体验痛点：

1. 激活的标注浮层 / 圈必须提升到最高层级，失焦后恢复；
2. 圈选屏幕坐标必须转换为画布世界坐标，平移 / 缩放时标注层与页面 DOM
   使用完全相同的 transform，不允许漂移；
3. 每个标注旁改为独立迷你 Chatbox，拥有 `threadId` / `chatHistory`，
   可在同一个圈上连续多轮增量修改，后端基于历史与上一轮源码执行。

## 前端改造

### 世界坐标与 transform 绑定

- `CanvasStage` 的 `.canvas-viewport` 是唯一 transform 容器：
  `translate(translateX, translateY) scale(scale)`，`transform-origin: top left`。
- Mock 页面与 `.annotation-layer` 都是该容器子节点，标注使用 1280×720
  设计视口百分比定位，因此页面、圈、浮层天然同矩阵。
- 画圈坐标转换：
  `worldX = (clientX - stageRect.left - translateX) / scale`，
  `worldY` 同理；再除以 1280/720 归一化。`translate` 已包含 cover 基准偏移
  与用户 pan，滚轮缩放的指针锚点算法也使用同一个世界坐标定义。
- `targetAtPoint` 用 `document.elementsFromPoint` 并跳过
  `.annotation-layer`，只读取页面上的 `data-vibe-target`。

### 层级管理

- `activeAnnotationId` 状态：激活圈 `z-index=100`，激活浮层 `z-index=120`，
  其余圈 `20+i`、浮层 `30+i`。
- 点击标注标签/浮层、输入框聚焦都会激活；点击画布空白或浮层失焦恢复。
- 浮层的中键 / 空格 + 左键指针事件继续冒泡给画布，保证全局平移手势不被
  浮层覆盖区域截断。

### 迷你 Chatbox

- 标注新增 `threadId`、`chatHistory`、`lastRunId`、`lastSandboxId`。
- `WorkspacePage` 新增 `handleAnnotationSend`：只提交当前圈，先乐观追加
  user 气泡，输入框清空；payload 携带完整 `chatHistory` 与当前 `sandboxId`。
- 结果返回后追加 `assistantMessage`，并更新 `lastRunId/lastSandboxId`。
- 浮层展示 user / assistant 气泡，`.popover-chat` 内部滚动，最大高度 218px；
  外层浮层 `max-height: 360px`，避免撑破画布。
- 会话快照兼容旧数据，恢复时自动补 `threadId` / `chatHistory`。

## 后端改造

### 请求 / 响应契约

- `AnnotationEnvelope` 新增 `threadId`、`chatHistory[]`。
- `ExecuteRunRequest` 新增 `sandboxId`。
- `ExecuteRunResponse` 新增 `reusedSandbox`、`assistantMessage`。
- Java BFF `CreateRunRequest` 增加 `sandboxId` 并原样透传。

### ThreadMemoryStore

- 进程内线程安全存储，`threadId -> ThreadRecord`：
  `chat_history`、最近 `runs`、最近源码 `files`、`sandbox_id`。
- `merge_context()` 合并前端历史与服务端记录（用户消息按 `id` 去重，
  助手消息按 `(role, runId, content)` 去重），输出最近 24 条历史与最近
  3 次 Run 记录。
- `record_run()` 写入用户指令、助手回复、变更文件、patch 摘要（截断 2000
  字符）、源码快照与沙盒 ID。
- 前端仍持有历史，因此 python-ai 重启后多轮上下文不丢失。

### 沙盒复用与增量修改

- `SandboxProvider.get(sandbox_id)` 新增；LocalDocker 通过容器标签恢复
  句柄、项目 ID、预览端口。
- `RunExecutionService._open_sandbox()` 优先复用请求 `sandboxId` / thread
  记录中的沙盒，并读取修改后的当前源码；跨项目 sandboxId 会被拒绝。
- 沙盒已销毁时使用 thread 源码快照 + `request.files` 重建，避免第二轮
  回滚第一轮成果。
- `_build_annotation_context()` 用当前沙盒源码覆盖前端旧 `target.snippet`，
  防止模型同时看到新旧两份源码。
- Planner / Coder 提示词明确：同圈历史中“改成红色” → “不，我要蓝色” 最终
  只能保留蓝色；历史修改已体现在当前源码，本轮只做增量，不回退未否定属性。

## 测试

### 新增：`scripts/multi_turn_chat_e2e_test.py`

真实 Chromium + Mock 模型：

```text
平移 + 缩放后 无漂移: ΔX=0.00000, ΔY=0.00000,
transform=matrix(1.7883, 0, 0, 1.7883, -285.312, -445.81)
Chatbox 屏幕宽度保持: 320 -> 320px
z-index: active=120 -> idle=30
第一轮: status=AWAITING_APPROVAL reused=False
第二轮: status=AWAITING_APPROVAL reused=True
threadId 一致 / chatHistory=['user', 'assistant', 'user']
最终 CTA background=rgb(0, 0, 255)
两轮对话 + 面板切换后 无漂移: ΔX=0.00000, ΔY=0.00000
刷新后两轮对话仍完整保留
MULTI-TURN CHAT E2E: PASS
```

### 回归

- `scripts/multi_annotation_e2e_test.py`：PASS（3 圈批量、上限、导出、
  `VIBE_CONTEXT.md`、Diff、清除）。
- `scripts/ui_e2e_test.py`：PASS（全流程；期间修复浮层拦截中键 / 空格平移）。
- `docker exec vibe-platform-web npm run build`：55 modules，构建通过。

## 已知边界

- ThreadMemoryStore 是进程内存储，M2 将迁移到 Redis / MySQL；跨进程重启的
  历史由前端 `chatHistory` 兜底，`previousRuns` 会丢失。
- Chatbox 锚点随世界坐标移动。**后续修订（同日）**：已移除
  `scale(1 / viewportScale)` 反向补偿，浮层改为固定 `320px` 世界宽度并
  完整继承画布矩阵，缩放时圆角 / 字号 / 输入框严格等比变化。验证见
  `scripts/popover_equal_scale_e2e_test.py`。
- DeepSeek 真实链路未在本次测试中调用（`.env` 的 Key 为空，health 显示
  `llm: mock`）；请求体、提示词和 threadContext 已通过 Mock / 契约验证。
